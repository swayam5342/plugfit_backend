import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from plugfit.app.config import settings
from plugfit.app.logging import LOGS_DIR

log = logging.getLogger("plugfit.tasks")


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _server_dir(server_id: str) -> Path:
    return LOGS_DIR / "servers" / server_id


def _append_jsonl(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_happening(server_id: str, job_id: str, stage: str, message: str) -> None:
    if not settings.ENABLE_FILE_LOGGING:
        return
    try:
        _append_jsonl(
            _server_dir(server_id) / "happening.jsonl",
            {
                "ts": _ts(),
                "server_id": server_id,
                "job_id": job_id,
                "stage": stage,
                "message": message,
            },
        )
    except Exception:
        log.warning(
            "Failed to write happening log for server=%s job=%s",
            server_id[:8],
            job_id[:8],
            exc_info=True,
        )


def log_change(server_id: str, job_id: str, field: str, **fields) -> None:
    if not settings.ENABLE_FILE_LOGGING:
        return
    try:
        _append_jsonl(
            _server_dir(server_id) / "changes.jsonl",
            {
                "ts": _ts(),
                "server_id": server_id,
                "job_id": job_id,
                "field": field,
                **fields,
            },
        )
    except Exception:
        log.warning(
            "Failed to write change log for server=%s job=%s field=%s",
            server_id[:8],
            job_id[:8],
            field,
            exc_info=True,
        )
