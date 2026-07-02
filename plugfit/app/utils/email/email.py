import logging
import resend

from ...config import settings

logger = logging.getLogger(__name__)

resend.api_key = settings.RESEND_API_KEY


def send_email(payload: dict):
    try:
        return resend.Emails.send(payload)  # type:ignore
    except Exception:
        logger.exception("Failed to send email")
        raise
