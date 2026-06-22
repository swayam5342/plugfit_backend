import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.models.models import User
from plugfit.app.schema.auth import (
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
    create_verification_token,
    decode_verification_token,
)

from plugfit.app.utils.db import utcnow
from ...app.utils.email import send_verification_email


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


def _send_verification_email(email: str):
    token = create_verification_token(
        email=email,
        expires_delta=timedelta(minutes=settings.VERIFICATION_TOKEN_EXPIRE_MINUTES),
    )
    magic_link = f"{settings.FRONTEND_URL}/auth/verify-email?token={token}"
    send_verification_email(
        email, magic_link, settings.VERIFICATION_TOKEN_EXPIRE_MINUTES
    )


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
