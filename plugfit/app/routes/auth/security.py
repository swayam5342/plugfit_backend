from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.models.models import RefreshToken, User
from plugfit.app.schema.auth import Token
from plugfit.app.utils.auth.token import (
    create_access_token,
    create_refresh_token,
    hash_refresh_token,
)
from plugfit.app.utils.db.funcs import utcnow


async def issue_token_pair(
    db: AsyncSession, user: User
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
    return Token(access_token=access_token, refresh_token=raw_refresh), refresh_row
