# app/api/routes/auth.py
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...app.db.db import get_db
from ...app.models.models import User
from ...app.models.password_reset import PasswordResetToken
from ...app.schema.auth import ForgotPasswordRequest, ResetPasswordRequest
from ..utils.auth.token_reset import generate_reset_token, hash_token
from ..utils.auth.token import hash_password
from ..utils.email.email import send_password_reset_email
from ...app.config import settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/forgot-password")
async def forgot_password(
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
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
        send_password_reset_email(
            username=user.email, email=user.email, reset_link=raw_token
        )


@router.post("/reset-password")
async def reset_password(
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
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
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    user_result = await db.execute(select(User).where(User.id == reset_entry.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="Invalid token")

    user.password_hash = hash_password(payload.new_password)
    reset_entry.used = True

    await db.commit()
    return {"message": "Password reset successful"}
