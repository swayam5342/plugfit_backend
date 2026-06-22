import logging
import resend

from ...app.config import settings

logger = logging.getLogger(__name__)

resend.api_key = settings.RESEND_API_KEY


def send_email(payload: dict):
    try:
        return resend.Emails.send(payload)  # type:ignore
    except Exception:
        logger.exception("Failed to send email")
        raise


def send_verification_email(email: str, magic_link: str):
    payload = {
        "from": f"Plugfit <{settings.RESEND_FROM_EMAIL}>",
        "to": [email],
        "subject": "Verify your email",
        "template": {
            "id": "magic-link-sign-in",
            "variables": {
                "first_name": email,
                "company_name": "PlugFit",
                "magic_link_url": magic_link,
                "VERIFICATION_EXPIRE": str(settings.VERIFICATION_TOKEN_EXPIRE_MINUTES),
            },
        },
    }
    send_email(payload)


def send_password_reset_email(username: str, email: str, reset_link: str):
    payload = {
        "from": f"Plugfit <{settings.RESEND_FROM_EMAIL}>",
        "to": [email],
        "subject": "Verify your email",
        "template": {
            "id": "password-reset",
            "variables": {
                "first_name": username,
                "company_name": "PlugFit",
                "reset_password_url": reset_link,
                "Expires": str(settings.PASSWORD_RESET_TIME),
            },
        },
    }
    send_email(payload)
