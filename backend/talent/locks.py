"""Named locks in the database, for jobs that must not run twice at once.

A threading.Lock only covers one process; the back office runs two uvicorn
workers and the weekly scrape also runs from cron. A row in job_locks covers
all of them. A lock has an expiry so a holder that crashed without letting go
blocks the next run for at most ``ttl``, not forever.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from talent import db
from talent.models import JobLock


def acquire(name: str, ttl: dt.timedelta) -> str | None:
    """Take the lock; return a holder token, or None when someone else has it."""
    holder = uuid.uuid4().hex
    now = dt.datetime.now(dt.timezone.utc)
    try:
        with db.session() as s:
            s.add(JobLock(name=name, holder=holder, acquired_at=now, expires_at=now + ttl))
        return holder
    except IntegrityError:
        pass
    # Taken. Steal it only if it has expired, in one conditional update so two
    # processes racing for a stale lock cannot both win.
    with db.session() as s:
        res = s.execute(
            update(JobLock)
            .where(JobLock.name == name, JobLock.expires_at < now)
            .values(holder=holder, acquired_at=now, expires_at=now + ttl)
        )
        return holder if res.rowcount == 1 else None


def release(name: str, holder: str) -> None:
    with db.session() as s:
        s.execute(delete(JobLock).where(JobLock.name == name, JobLock.holder == holder))
