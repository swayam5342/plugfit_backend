import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.db.db import get_db
from plugfit.app.models.models import RefreshToken, User
from plugfit.app.schema.auth import Token, UserCreate, UserOut
from plugfit.app.utils.auth.token import (
    hash_password,
    hash_refresh_token,
    verify_password,
)
from plugfit.app.utils.db.funcs import utcnow
from plugfit.app.utils.db.get_users import get_user_by_email, get_user_by_id

from .email_verification import send_verification_email_for
from .security import REFRESH_COOKIE_NAME, clear_refresh_cookie, issue_token_pair
from plugfit.app.utils.auth.make_slug import make_slug

logger = logging.getLogger(__name__)

router = APIRouter()


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email)
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    logger.info(f"User registration attempt: {user_in.email}")
    existing = await get_user_by_email(db, user_in.email)
    if existing:
        logger.info(f"Registration failed - email already exists: {user_in.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    slug = make_slug(user_in.name)
    result = await db.execute(select(User).where(User.slug == slug))
    if result.scalar_one_or_none():
        slug = f"{slug}-{secrets.token_hex(4)}"

    user = User(
        name=user_in.name,
        email=user_in.email,
        slug=slug,
        password_hash=hash_password(user_in.password),
    )
    send_verification_email_for(user_name=user_in.name, email=user_in.email)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    logger.info(f"User registered successfully: {user_in.email} (ID: {user.id})")
    return user


@router.post("/login", response_model=Token)
async def login(
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    logger.info(f"Login attempt: {form_data.username}")
    user = await authenticate_user(db, form_data.username, form_data.password)
    if not user:
        logger.warning(f"Login failed - invalid credentials: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_email_verified:
        logger.warning(f"Login failed - email not verified: {form_data.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email address is not verified. Please verify your email before logging in.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, _ = await issue_token_pair(db, user, response)
    logger.info(f"User logged in successfully: {user.email} (ID: {user.id})")
    return token


@router.post("/refresh", response_model=Token)
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    logger.debug("Token refresh attempt")
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired refresh token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    raw_token = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_token:
        logger.warning("Token refresh failed - no refresh token found")
        raise invalid

    token_hash = hash_refresh_token(raw_token)
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == token_hash)
    )
    stored = result.scalar_one_or_none()

    if not stored:
        logger.warning("Token refresh failed - invalid token hash")
        raise invalid

    if stored.revoked_at is not None:
        logger.warning("Token refresh failed - token already revoked")
        clear_refresh_cookie(response)
        raise invalid

    if stored.expires_at <= utcnow():
        logger.warning("Token refresh failed - token expired")
        raise invalid

    user = await get_user_by_id(db, stored.user_id)
    if not user or not user.is_active:
        logger.warning("Token refresh failed - user not found or inactive")
        raise invalid

    new_token, new_row = await issue_token_pair(db, user, response)

    stored.revoked_at = utcnow()
    stored.replaced_by = new_row.id
    await db.commit()
    logger.debug(f"Token refreshed successfully for user ID: {user.id}")
    return new_token


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> None:
    logger.info("User logout attempt")
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
            logger.info("User logged out successfully")

    clear_refresh_cookie(response)
