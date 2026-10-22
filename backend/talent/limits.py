"""Limits on the public chat that hold across workers and across addresses.

Two counters, both in the database (rate_counters) so the two uvicorn workers
and any later ones share them:

  per network   CHAT_RATE_LIMIT model calls per CHAT_RATE_WINDOW seconds from one
                visitor network. IPv6 is counted per /64, the block one home
                or phone gets, so rotating through addresses inside it does
                not reset the count. IPv4 is counted per address.
  per day       LLM_DAILY_BUDGET model calls per UTC day for the whole service,
                chat and scoring together. This is the cost cap: whatever the
                per-network limit misses, the bill cannot pass it.

Counters are fixed windows (a burst across a window edge can reach twice the
limit for a moment); that is fine for a cost cap and needs one row per key.
"""

from __future__ import annotations

import ipaddress
import os
import random
import time

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from talent import db
from talent.models import RateCounter

DAY = 86400
BUDGET_KEY = "llm:day"


def daily_budget() -> int:
    return int(os.environ.get("LLM_DAILY_BUDGET", "1000"))


def network_key(ip: str) -> str:
    """The unit a visitor is counted by: an IPv4 address, or an IPv6 /64."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return (ip or "unknown")[:64]
    if addr.version == 6:
        if addr.ipv4_mapped:
            return str(addr.ipv4_mapped)
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(addr)


def hit(key: str, window: int, now: float | None = None) -> int:
    """Count one event under ``key`` in the current window; return the new total."""
    now = time.time() if now is None else now
    start = int(now // window * window)
    stmt = _increment(db.engine().dialect.name, key[:128], start)
    with db.session() as s:
        count = s.execute(stmt).scalar_one()
        if random.random() < 0.02:
            # Housekeeping: nothing older than two days is ever read again.
            s.execute(delete(RateCounter).where(RateCounter.window_start < int(now) - 2 * DAY))
    return int(count)


def _increment(dialect: str, key: str, start: int):
    """One atomic statement: insert the counter at 1 or add 1, and return it.
    Postgres and SQLite (3.35+) both speak ON CONFLICT ... RETURNING."""
    insert = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert(RateCounter).values(key=key, window_start=start, count=1)
    return stmt.on_conflict_do_update(
        index_elements=[RateCounter.key, RateCounter.window_start],
        set_={"count": RateCounter.count + 1},
    ).returning(RateCounter.count)


def over_limit(key: str, limit: int, window: int, now: float | None = None) -> bool:
    return hit(key, window, now) > limit


def take_llm_call(now: float | None = None) -> bool:
    """Spend one model call from today's budget. False when it is used up."""
    return hit(BUDGET_KEY, DAY, now) <= daily_budget()
