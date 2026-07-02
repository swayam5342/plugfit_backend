from fastapi import APIRouter

from .dependencies import get_current_user, oauth2_scheme
from . import email_verification, google_oauth, password, profile, forget_password

router = APIRouter(tags=["auth"])
router.include_router(password.router)
router.include_router(email_verification.router)
router.include_router(google_oauth.router)
router.include_router(profile.router)
router.include_router(forget_password.router)

__all__ = ["router", "get_current_user", "oauth2_scheme"]
