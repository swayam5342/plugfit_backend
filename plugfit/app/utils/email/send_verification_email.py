from ...config import settings
from .email import send_email


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
