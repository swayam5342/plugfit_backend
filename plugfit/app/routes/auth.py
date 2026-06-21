import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.models.models import RefreshToken, User
from plugfit.app.schema.auth import Token, UserCreate, UserOut
from plugfit.app.utils.auth import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)
from plugfit.app.utils.db import utcnow

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
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


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


@router.get("/me", response_model=UserOut)
async def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    return current_user
