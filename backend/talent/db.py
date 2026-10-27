"""Database wiring for Miton Talent.

One engine per process. The URL comes from DATABASE_URL:

  postgresql://...   (the Hetzner .env sets this; "postgres://" is normalised too)
  sqlite:///./miton_talent.db   (default for local development and tests)

In production (APP_BASE_URL on https) a missing DATABASE_URL is an error, not
a quiet SQLite file next to the code: candidates written there would be
invisible to the back office and lost on the next deploy.

The schema is managed by Alembic (backend/alembic). Nothing here creates tables;
run `alembic upgrade head` (the systemd unit does it before uvicorn starts).
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker


def is_production() -> bool:
    """The public https deployment, as opposed to a laptop or the tests."""
    return os.environ.get("APP_BASE_URL", "").strip().lower().startswith("https://")


def database_url() -> str:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        if is_production():
            raise RuntimeError("DATABASE_URL is not set; refusing to fall back to SQLite in production")
        url = "sqlite:///./miton_talent.db"
    # Some hosts hand out the legacy scheme; SQLAlchemy 2 wants the driver named.
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def make_engine(url: str | None = None):
    url = url or database_url()
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


_engine = None
_SessionLocal = None


def engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine()
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine


def session_factory() -> sessionmaker:
    engine()
    return _SessionLocal  # type: ignore[return-value]


@contextmanager
def session() -> Iterator[Session]:
    """Commit on success, roll back on any exception, always close."""
    s = session_factory()()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def reset_for_tests(url: str) -> None:
    """Point the process at a fresh engine (tests use an in-memory SQLite)."""
    global _engine, _SessionLocal
    _engine = make_engine(url)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
