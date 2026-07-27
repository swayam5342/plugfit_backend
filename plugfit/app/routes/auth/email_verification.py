from datetime import timedelta
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.schema.auth import ResendVerificationRequest
from plugfit.app.utils.auth.token import (
    TokenError,
    create_verification_token,
    decode_verification_token,
)
from plugfit.app.utils.db.get_users import get_user_by_email
from plugfit.app.utils.email import send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter()


def send_verification_email_for(user_name, email: str) -> None:
    token = create_verification_token(
        email=email,
        expires_delta=timedelta(minutes=settings.VERIFICATION_TOKEN_EXPIRE_MINUTES),
    )
    magic_link = f"{settings.FRONTEND_URL}/verify-email?token={token}"
    send_verification_email(user_name, email, magic_link)


@router.get("/verify-email")
async def verify_email(
    token: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    logger.debug("Email verification token received")
    try:
        payload = decode_verification_token(token)
    except TokenError as e:
        logger.warning(f"Email verification failed - invalid token: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )

    email = payload.get("sub")
    if not email:
        logger.warning("Email verification failed - no email in token")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid token",
        )

    user = await get_user_by_email(db, email)
    if not user:
        logger.warning(f"Email verification failed - user not found: {email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User not found",
        )

    if user.is_email_verified:
        logger.info(f"Email already verified: {email}")
        return {"detail": "Email already verified."}

    user.is_email_verified = True
    db.add(user)
    await db.commit()
    logger.info(f"Email verified successfully: {email} (User ID: {user.id})")
    return {"detail": "Email verified successfully."}


@router.post("/resend-verification")
async def resend_verification(
    payload: ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    logger.info(f"Resend verification email request: {payload.email}")
    user = await get_user_by_email(db, payload.email)
    if not user:
        logger.warning(f"Resend verification failed - user not found: {payload.email}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email not registered.",
        )

    if user.is_email_verified:
        logger.debug(
            f"Resend verification skipped - email already verified: {payload.email}"
        )
        return {"detail": "Email is already verified."}

    send_verification_email_for(user_name=user.name, email=user.email)
    logger.info(f"Verification email resent: {payload.email}")
    return {"detail": "Verification email sent."}
