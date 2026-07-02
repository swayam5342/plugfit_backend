import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.db.db import get_db
from plugfit.app.models.models import User
from plugfit.app.schema.auth import UserOut, UserUpdate
from plugfit.app.utils.db.get_users import get_user_by_email

from .dependencies import get_current_user
from .email_verification import send_verification_email_for
from plugfit.app.utils.auth import make_slug

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/me", response_model=UserOut)
async def read_current_user(current_user: User = Depends(get_current_user)) -> User:
    logger.debug(f"Fetching current user profile: {current_user.email}")
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_current_user(
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    logger.info(f"User profile update request: {current_user.email}")
    if not any((payload.name, payload.email)):
        logger.warning(
            f"Profile update failed - no update fields provided: {current_user.email}"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No update fields provided.",
        )

    if payload.email and payload.email != current_user.email:
        existing = await get_user_by_email(db, payload.email)
        if existing and existing.id != current_user.id:
            logger.warning(
                f"Profile update failed - email already exists: {payload.email}"
            )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email already registered",
            )
        current_user.email = payload.email
        current_user.is_email_verified = False
        send_verification_email_for(payload.email)
        logger.info(f"Email updated for user: {current_user.email} -> {payload.email}")
    if payload.name:
        slug = make_slug(payload.name)
        result = await db.execute(select(User).where(User.slug == slug))
        if result.scalar_one_or_none():
            slug = f"{slug}-{secrets.token_hex(4)}"
        current_user.name = payload.name
        current_user.slug = slug
        logger.info(f"Name updated for user: {current_user.email} -> {payload.name}")

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    logger.info(
        f"User profile updated successfully: {current_user.email} (ID: {current_user.id})"
    )
    return current_user
