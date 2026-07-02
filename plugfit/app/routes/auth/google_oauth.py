import logging
import secrets

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from plugfit.app.config import settings
from plugfit.app.db.db import get_db
from plugfit.app.models.models import OAuthAccount, User
from plugfit.app.schema.auth import Token
from plugfit.app.utils.db.get_users import get_user_by_email

from .security import issue_token_pair
from plugfit.app.utils.auth import make_slug

logger = logging.getLogger(__name__)

router = APIRouter()

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_SCOPES = "openid email profile"
OAUTH_STATE_COOKIE = "oauth_state"
OAUTH_STATE_MAX_AGE = 60 * 10


@router.get("/google")
async def google_login() -> RedirectResponse:
    logger.info("Google OAuth login initiated")
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
    logger.debug("Google OAuth state cookie set")
    return redirect


@router.get("/google/callback", response_model=Token)
async def google_callback(
    code: str,
    state: str,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> Token:
    logger.debug("Google OAuth callback initiated")
    stored_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not stored_state or stored_state != state:
        logger.warning(
            "Google OAuth callback failed - invalid state (CSRF attempt suspected)"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state — possible CSRF attempt",
        )
    response.delete_cookie(key=OAUTH_STATE_COOKIE, path="/auth/google")
    google_tokens = await _exchange_code_for_tokens(code)
    google_access_token = google_tokens.get("access_token")
    if not google_access_token:
        logger.warning("Google OAuth callback failed - failed to obtain access token")
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
        logger.warning("Google OAuth callback failed - missing required user info")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return required user info",
        )

    if not email_verified:
        logger.warning(
            f"Google OAuth callback failed - email not verified: {google_email}"
        )
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
            logger.warning(
                f"Google OAuth callback failed - account is inactive: {google_email}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Account is inactive",
            )
        logger.debug(f"Existing Google OAuth account linked: {google_email}")
    else:
        user = await get_user_by_email(db, google_email)

        if user:
            if not user.is_active:
                logger.warning(
                    f"Google OAuth callback failed - account is inactive: {google_email}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Account is inactive",
                )
            if not user.is_email_verified:
                user.is_email_verified = True
            logger.info(f"Existing user linked with Google OAuth: {google_email}")
        else:
            slug = make_slug(google_name)
            slug_taken = await db.execute(select(User).where(User.slug == slug))
            if slug_taken.scalar_one_or_none():
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
            logger.info(
                f"New user created via Google OAuth: {google_email} (ID: {user.id})"
            )
        db.add(oauth_row)
    token, _ = await issue_token_pair(db, user, response)
    logger.info(f"Google OAuth login successful: {user.email} (ID: {user.id})")
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
