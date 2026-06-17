import uuid
from datetime import datetime, timezone
import secrets


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_uuid() -> str:
    return str(uuid.uuid4())


def new_api_key() -> str:
    return f"pf_{secrets.token_hex(16)}"
