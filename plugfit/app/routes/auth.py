import json
import re
import secrets
import urllib.error
import urllib.request
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.models.models import User
from plugfit.app.schema.auth import (
    EmailVerificationRequest,
    ResendVerificationRequest,
    Token,
    UserCreate,
    UserOut,
)
from plugfit.app.utils.auth import (
    TokenError,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from plugfit.app.utils.db import utcnow

router = APIRouter()
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


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


def _generate_email_verification_otp() -> str:
    return f"{secrets.randbelow(1000000):06d}"


def _send_verification_email(email: str, otp: str) -> None:
    if not settings.RESEND_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Email delivery is not configured. Set RESEND_API_KEY.",
        )

    payload = {
        "from": settings.RESEND_FROM_EMAIL,
        "to": [email],
        "subject": "Verify your email",
        "html": (
            f"<p>Use the code below to verify your email address:</p>"
            f"<p><strong>{otp}</strong></p>"
            f"<p>If you did not request this, please ignore this email.</p>"
        ),
    }
    
    print(f"DEBUG: RESEND_FROM_EMAIL={settings.RESEND_FROM_EMAIL}")
    print(f"DEBUG: RESEND_API_KEY_PREFIX={settings.RESEND_API_KEY[:10]}")
    print(f"DEBUG: Sending email from {settings.RESEND_FROM_EMAIL} to {email}")
    print(f"DEBUG: API Key set: {bool(settings.RESEND_API_KEY)}")
    print(f"DEBUG: API Key length: {len(settings.RESEND_API_KEY)}")
    
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            response_body = resp.read().decode("utf-8", errors="ignore")
            try:
                resp_headers = dict(resp.getheaders())
            except Exception:
                resp_headers = {}
            print(f"DEBUG: Response status: {resp.status}")
            print(f"DEBUG: Response headers: {resp_headers}")
            print(f"DEBUG: Response body (full): {response_body}")
            if resp.status >= 400:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"Resend API error (HTTP {resp.status}): {response_body}",
                )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
        try:
            exc_headers = dict(exc.headers) if exc.headers is not None else {}
        except Exception:
            exc_headers = {}
        print(f"DEBUG: HTTPError status: {getattr(exc, 'code', 'N/A')}")
        print(f"DEBUG: HTTPError headers: {exc_headers}")
        print(f"DEBUG: HTTPError body (full): {error_body}")
        error_detail = (
            f"Resend API error (HTTP {getattr(exc, 'code', 'N/A')}): {error_body}"
            if error_body
            else f"Resend API error (HTTP {getattr(exc, 'code', 'N/A')})"
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=error_detail,
        ) from exc
    except urllib.error.URLError as exc:
        print(f"DEBUG: URLError: {str(exc)}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Email delivery service is unavailable: {str(exc)}",
        ) from exc


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

    otp = _generate_email_verification_otp()
    expires_at = utcnow() + timedelta(minutes=15)

    user = User(
        name=user_in.name,
        email=user_in.email,
        slug=slug,
        password_hash=hash_password(user_in.password),
        email_verification_otp=otp,
        email_verification_otp_expires_at=expires_at,
    )

    _send_verification_email(user_in.email, otp)

    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/verify-email")
async def verify_email(
    payload: EmailVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    user = await _get_user_by_email(db, payload.email)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or OTP",
        )

    if user.is_email_verified:
        return {"detail": "Email is already verified."}

    if not user.email_verification_otp or not user.email_verification_otp_expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No active verification OTP found.",
        )

    if user.email_verification_otp != payload.otp:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid email or OTP",
        )

    if utcnow() > user.email_verification_otp_expires_at:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Verification code has expired.",
        )

    user.is_email_verified = True
    user.email_verification_otp = None
    user.email_verification_otp_expires_at = None
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

    otp = _generate_email_verification_otp()
    expires_at = utcnow() + timedelta(minutes=15)

    user.email_verification_otp = otp
    user.email_verification_otp_expires_at = expires_at
    db.add(user)
    await db.commit()

    _send_verification_email(user.email, otp)

    return {"detail": "Verification email resent."}


@router.post("/login", response_model=Token)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    user = await _authenticate_user(
        db,
        form_data.username,
        form_data.password,
    )

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

    access_token = create_access_token(
        subject=user.id,
        expires_delta=timedelta(
            minutes=settings.JWT_EXPIRE_MINUTES,
        ),
    )

    return Token(access_token=access_token)


@router.get("/me", response_model=UserOut)
async def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user
