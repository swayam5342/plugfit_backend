import re
import secrets
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.schema.auth import (
    ResendVerificationRequest,
    Token,
    UserCreate,
    UserOut,
    UserUpdate,
)
from plugfit.app.models.models import RefreshToken, User, OAuthAccount
from plugfit.app.utils.auth import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    verify_password,
    create_verification_token,
    decode_verification_token,
)
from plugfit.app.utils.db import utcnow
from ...app.utils.email import send_verification_email
import httpx


router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")

REFRESH_COOKIE_NAME = "refresh_token"
REFRESH_COOKIE_PATH = "/auth"


def _make_slug(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or secrets.token_hex(8)


async def _get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email))
    return result.scalar_one_or_none()


async def _get_user_by_id(db: AsyncSession, user_id: str) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def _authenticate_user(
    db: AsyncSession, email: str, password: str
) -> User | None:
    user = await _get_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def _send_verification_email(email: str):
    token = create_verification_token(
        email=email,
        expires_delta=timedelta(minutes=settings.VERIFICATION_TOKEN_EXPIRE_MINUTES),
    )
    magic_link = f"{settings.FRONTEND_URL}/auth/verify-email?token={token}"
    send_verification_email(email, magic_link)


async def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(token)
        user_id = payload.get("sub")
        if not user_id:
            raise TokenError("Missing subject")
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = await _get_user_by_id(db, user_id)
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def _set_refresh_cookie(response: Response, raw_token: str) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=raw_token,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=settings.JWT_REFRESH_EXPIRE_DAYS * 24 * 60 * 60,
        path=REFRESH_COOKIE_PATH,
    )


def _clear_refresh_cookie(response: Response) -> None:
    response.delete_cookie(key=REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH)


async def _issue_token_pair(
    db: AsyncSession, user: User, response: Response
) -> tuple[Token, RefreshToken]:
    access_token = create_access_token(
        subject=user.id,
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_EXPIRE_MINUTES),
    )

    raw_refresh = create_refresh_token()
    refresh_row = RefreshToken(
        user_id=user.id,
        token_hash=hash_refresh_token(raw_refresh),
        expires_at=utcnow() + timedelta(days=settings.JWT_REFRESH_EXPIRE_DAYS),
    )
    db.add(refresh_row)
    await db.commit()
    await db.refresh(refresh_row)
    _set_refresh_cookie(response, raw_refresh)
    return Token(access_token=access_token), refresh_row


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    existing = await _get_user_by_email(db, user_in.email)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    slug = _make_slug(user_in.name)
    result = await db.execute(select(User).where(User.slug == slug))
    if result.scalar_one_or_none():
        slug = f"{slug}-{secrets.token_hex(4)}"

    user = User(
        name=user_in.name,
        email=user_in.email,
        slug=slug,
        password_hash=hash_password(user_in.password),
    )
    _send_verification_email(user_in.email)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return user


@router.get("/verify-email")
async def verify_email(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        payload = decode_verification_token(token)
    except TokenError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    email = payload.get("sub")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token",
        )

    user = await _get_user_by_email(db, email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found",
        )

    if user.is_email_verified:
        return {"detail": "Email already verified."}

    user.is_email_verified = True
    db.add(user)
    await db.commit()

    return {"detail": "Email verified successfully."}


@router.post("/resend-verification")
async def resend_verification(
    payload: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = await _get_user_by_email(db, payload.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email not registered.",
        )

    if user.is_email_verified:
        return {"detail": "Email is already verified."}

    _send_verification_email(user.email)

    return {"detail": "Verification email sent."}


@router.post("/login", response_model=Token)
async def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    user = await _authenticate_user(db, form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email address is not verified. Please verify your email before logging in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, _ = await _issue_token_pair(db, user, response)
    return token


@router.post("/refresh", response_model=Token)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_token:
        raise invalid

    token_hash = hash_refresh_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()

    if not stored:
        raise invalid

    if stored.revoked_at is not None:
        _clear_refresh_cookie(response)
        raise invalid

    if stored.expires_at <= utcnow():
        raise invalid

    user = await _get_user_by_id(db, stored.user_id)
    if not user or not user.is_active:
        raise invalid

    new_token, new_row = await _issue_token_pair(db, user, response)

    stored.revoked_at = utcnow()
    stored.replaced_by = new_row.id
    await db.commit()

    return new_token


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_token:
        token_hash = hash_refresh_token(raw_token)
        result = await db.execute(
            select(RefreshToken).where(RefreshToken.token_hash == token_hash)
        )
        stored = result.scalar_one_or_none()
        if stored and stored.revoked_at is None:
            stored.revoked_at = utcnow()
            await db.commit()

    _clear_refresh_cookie(response)


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_SCOPES = "openid email profile"
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_MAX_AGE = 60 * 10


@router.get("/google")
async def google_login() -> RedirectResponse:
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "select_account",
    }
    query = "&".join(f"{k}={v}" for k, v in params.items())
    redirect = RedirectResponse(url=f"{GOOGLE_AUTH_URL}?{query}")
    state = secrets.token_urlsafe(32)
    redirect.set_cookie(
        key=OAUTH_STATE_COOKIE,
        value=state,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        max_age=OAUTH_STATE_MAX_AGE,
        path="/auth/google",
    )
    return redirect


@router.get("/google/callback", response_model=Token)
async def google_callback(
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    stored_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not stored_state or stored_state != state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state — possible CSRF attempt",
        )
    response.delete_cookie(key=OAUTH_STATE_COOKIE, path="/auth/google")
    google_tokens = await _exchange_code_for_tokens(code)
    google_access_token = google_tokens.get("access_token")
    if not google_access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to obtain access token from Google",
        )
    google_user = await _fetch_google_userinfo(google_access_token)

    google_sub = google_user.get("sub")
    google_email = google_user.get("email")
    google_name = google_user.get("name") or google_email.split("@")[0]  # type:ignore
    email_verified = google_user.get("email_verified", False)

    if not google_sub or not google_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return required user info",
        )

    if not email_verified:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account email is not verified",
        )
    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == "google",
            OAuthAccount.provider_user_id == google_sub,
        )
    )
    oauth_row = result.scalar_one_or_none()

    if oauth_row:
        # already linked — just load the user
        result = await db.execute(select(User).where(User.id == oauth_row.user_id))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is inactive",
            )
    else:
        user = await _get_user_by_email(db, google_email)

        if user:
            if not user.is_active:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is inactive",
                )
            if not user.is_email_verified:
                user.is_email_verified = True
        else:
            slug = _make_slug(google_name)
            slug_taken = await db.execute(select(User).where(User.slug == slug))
            if slug_taken.scalar_one_or_none():
                import secrets

                slug = f"{slug}-{secrets.token_hex(4)}"

            user = User(
                name=google_name,
                email=google_email,
                slug=slug,
                password_hash=None,
                is_email_verified=True,
            )
            db.add(user)
            await db.flush()
            oauth_row = OAuthAccount(
                user_id=user.id,
                provider="google",
                provider_user_id=google_sub,
                provider_email=google_email,
            )
        db.add(oauth_row)
    token, _ = await _issue_token_pair(db, user, response)
    return token


async def _exchange_code_for_tokens(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GOOGLE_CLIENT_ID,
                "client_secret": settings.GOOGLE_CLIENT_SECRET,
                "redirect_uri": settings.GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Google token exchange failed: {resp.text}",
        )
    return resp.json()


async def _fetch_google_userinfo(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to fetch user info from Google",
        )
    return resp.json()


@router.get("/me", response_model=UserOut)
async def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_current_user(
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not any([payload.name, payload.email, payload.password]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No update fields provided.",
        )

    if payload.email and payload.email != current_user.email:
        existing = await _get_user_by_email(db, payload.email)
        if existing and existing.id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        current_user.email = payload.email
        current_user.is_email_verified = False
        _send_verification_email(payload.email)

    if payload.name and payload.name != current_user.name:
        slug = _make_slug(payload.name)
        result = await db.execute(select(User).where(User.slug == slug))
        if result.scalar_one_or_none():
            slug = f"{slug}-{secrets.token_hex(4)}"
        current_user.name = payload.name
        current_user.slug = slug

    if payload.password:
        current_user.password_hash = hash_password(payload.password)

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user
