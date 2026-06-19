import os
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_raw_url = os.getenv("DATABASE_URL", "sqlite:///./plugfit_dev.db")
_sync_url = _raw_url.replace("sqlite+aiosqlite", "sqlite").replace(
    "postgresql+asyncpg", "postgresql+psycopg2"
)

sync_engine = create_engine(
    _sync_url,
    connect_args={"check_same_thread": False} if "sqlite" in _sync_url else {},
    pool_pre_ping=True,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autoflush=True,
    autocommit=False,
    expire_on_commit=False,
)


@contextmanager
def sync_db_session() -> Generator[Session, None, None]:
    """Context manager for a sync DB session inside a Celery task."""
    session = SyncSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
