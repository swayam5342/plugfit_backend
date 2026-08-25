import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from plugfit.app.config import settings

LOGS_DIR = Path("logs")
APP_LOG_MAX_BYTES = 5 * 1024 * 1024
APP_LOG_BACKUP_COUNT = 5
_JOB_LOGGER_NAMES = {
    "plugfit.tasks",
    "plugfit.pipeline",
    "plugfit.scorer",
    "plugfit.app.job.cleaner",
}
_JOB_LOGGER_PREFIXES = ("plugfit.eval.",)


class _ExcludeJobLogs(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name in _JOB_LOGGER_NAMES:
            return False
        return not record.name.startswith(_JOB_LOGGER_PREFIXES)


def setup_logging(level: str = "INFO") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    if settings.ENABLE_FILE_LOGGING:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                LOGS_DIR / "app.log",
                maxBytes=APP_LOG_MAX_BYTES,
                backupCount=APP_LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.addFilter(_ExcludeJobLogs())
            handlers.append(file_handler)
        except Exception:
            # File logging is best-effort — never block app startup on it.
            logging.getLogger(__name__).warning(
                "Could not set up app.log file handler — continuing with "
                "stdout logging only",
                exc_info=True,
            )

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,  # Reconfigure existing loggers
    )
