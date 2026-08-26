# app/api/routes/auth.py
from datetime import datetime, timedelta, timezone
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...db.db import get_db
from ...models.models import User
from ...models.password_reset import PasswordResetToken
from ...schema.auth import ForgotPasswordRequest, ResetPasswordRequest
from ...utils.auth.token_reset import generate_reset_token, hash_token
from ...utils.auth.token import hash_password
from ...utils.email import send_password_reset_email
from ...config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    logger.info(f"Forgot password request for email: {payload.email}")
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()

    if user:
        raw_token, token_hash = generate_reset_token()
        reset_entry = PasswordResetToken(
            user_id=user.id,
            token_hash=token_hash,
            expires_at=datetime.now(timezone.utc)
            + timedelta(minutes=settings.PASSWORD_RESET_TIME),
        )
        db.add(reset_entry)
        await db.commit()
        reset_link = f"{settings.FRONTEND_URL}/reset-password?token={raw_token}"
        send_password_reset_email(
            username=user.name, email=user.email, reset_link=reset_link
        )
        logger.info(f"Password reset email sent for user: {payload.email}")
    else:
        logger.debug(f"Forgot password request for non-existent email: {payload.email}")


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    logger.debug("Password reset attempt with provided token")
    token_hash = hash_token(payload.token)

    result = await db.execute(
        select(PasswordResetToken).where(PasswordResetToken.token_hash == token_hash)
    )
    reset_entry = result.scalar_one_or_none()

    if (
        not reset_entry
        or reset_entry.used
        or reset_entry.expires_at < datetime.now(timezone.utc)
    ):
        logger.warning("Password reset failed - invalid or expired token")
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    user_result = await db.execute(select(User).where(User.id == reset_entry.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        logger.warning("Password reset failed - user not found")
        raise HTTPException(status_code=400, detail="Invalid token")

    user.password_hash = hash_password(payload.new_password)
    reset_entry.used = True

    await db.commit()
    logger.info(f"Password reset successfully for user: {user.email} (ID: {user.id})")
    return {"message": "Password reset successful"}
