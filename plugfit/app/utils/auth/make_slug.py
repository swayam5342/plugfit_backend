import re
import secrets


def make_slug(value: str) -> str:
    candidate = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return candidate or secrets.token_hex(8)
