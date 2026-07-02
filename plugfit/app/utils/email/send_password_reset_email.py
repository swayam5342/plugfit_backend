from ...config import settings
from .email import send_email


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
