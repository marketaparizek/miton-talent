"""Token spend: one log line and one llm_usage row per Anthropic call.

    from talent.usage import log_usage
    log_usage("chat", resp, model=MODEL)

Only counts: never message contents, never anything about the candidate.
Recording never breaks the call it describes: if the database is down, the
log line is still written and the chat answers as usual.

The price list is per million tokens, Anthropic first-party rates (checked
22 Nov 2026). A cache write (5 minute TTL) costs 1.25x input; a cache read
costs what the model's own row says. Add a row when a new model goes in;
until then its calls are recorded with priced=False and the page says so.
"""

from __future__ import annotations

import datetime as dt
import logging
import os

from sqlalchemy import func, select

log = logging.getLogger("miton-talent.usage")

# model id prefix -> (input, output, cache read) in USD per million tokens
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
}
CACHE_WRITE_MULTIPLIER = 1.25

# What each feature is, in the words the admin page uses.
FEATURES = {
    "chat": "Chat na webu",
    "score": "Hodnocení přihlášek",
    "portfolio_extract": "Portfolio: čtení kariérních stránek",
    "portfolio_classify": "Portfolio: třídění rolí",
    "portfolio_check": "Portfolio: kontrola modelu",
}


def price_for(model: str) -> tuple[float, float, float] | None:
    """Longest matching prefix, so 'claude-sonnet-5-5' never takes the
    'claude-sonnet-5' row and a dated id still finds its model."""
    model = (model or "").lower()
    best = None
    for prefix, row in PRICES.items():
        if model.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, row)
    return best[1] if best else None


def cost_micro_usd(model: str, input_tokens: int, output_tokens: int,
                   cache_write: int, cache_read: int) -> tuple[int, bool]:
    """(cost in millionths of a dollar, whether the model had a price).
    Per million tokens times tokens / 1e6 * 1e6 micro = price * tokens."""
    row = price_for(model)
    if row is None:
        return 0, False
    inp, out, read = row
    micro = (inp * input_tokens + out * output_tokens
             + inp * CACHE_WRITE_MULTIPLIER * cache_write + read * cache_read)
    return int(round(micro)), True


def log_usage(feature: str, response, *, model: str | None = None) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    def n(name: str) -> int:
        value = getattr(usage, name, None)
        return value if isinstance(value, int) else 0

    model = model or getattr(response, "model", None) or "?"
    counts = (n("input_tokens"), n("output_tokens"),
              n("cache_creation_input_tokens"), n("cache_read_input_tokens"))
    log.info(
        "anthropic usage feature=%s model=%s input=%d output=%d cache_write=%d cache_read=%d",
        feature, model, *counts,
    )
    record(feature, model, *counts)


def record(feature: str, model: str, input_tokens: int, output_tokens: int,
           cache_write: int = 0, cache_read: int = 0) -> None:
    if os.environ.get("USAGE_DB", "1") == "0":
        return
    try:
        from talent import db
        from talent.models import LlmUsage

        cost, priced = cost_micro_usd(model, input_tokens, output_tokens, cache_write, cache_read)
        with db.session() as s:
            s.add(LlmUsage(feature=feature[:32], model=model[:64], input_tokens=input_tokens,
                           output_tokens=output_tokens, cache_write_tokens=cache_write,
                           cache_read_tokens=cache_read, cost_micro_usd=cost, priced=priced))
    except Exception:
        log.warning("could not record usage for %s", feature, exc_info=True)


# --- reading it back (/admin/usage) -------------------------------------------


def _usd(micro) -> float:
    return round((micro or 0) / 1_000_000, 4)


def summary(now: dt.datetime | None = None, days: int = 30) -> dict:
    """Everything the admin page shows, from the llm_usage table.

    Days are UTC days, like the daily budget in talent/limits.py.
    """
    from talent import db, limits
    from talent.models import LlmUsage, RateCounter

    now = now or dt.datetime.now(dt.timezone.utc)
    today = now.date()
    start = dt.datetime.combine(today - dt.timedelta(days=days - 1), dt.time(), tzinfo=dt.timezone.utc)
    month_start = dt.datetime(today.year, today.month, 1, tzinfo=dt.timezone.utc)
    prev_month_start = (month_start - dt.timedelta(days=1)).replace(day=1)

    U = LlmUsage
    sums = (func.count(U.id), func.coalesce(func.sum(U.cost_micro_usd), 0),
            func.coalesce(func.sum(U.input_tokens), 0), func.coalesce(func.sum(U.output_tokens), 0),
            func.coalesce(func.sum(U.cache_write_tokens), 0), func.coalesce(func.sum(U.cache_read_tokens), 0))

    def totals(s, since, until=None):
        q = select(*sums).where(U.created_at >= since)
        if until is not None:
            q = q.where(U.created_at < until)
        calls, cost, inp, out, cw, cr = s.execute(q).one()
        prompt = inp + cw + cr
        return {"calls": calls, "usd": _usd(cost), "input": inp, "output": out,
                "cache_write": cw, "cache_read": cr,
                "cache_share": round(cr / prompt, 3) if prompt else None}

    with db.session() as s:
        day_rows = s.execute(
            select(U.created_at, U.feature, U.cost_micro_usd).where(U.created_at >= start)
        ).all()
        by_feature = s.execute(
            select(U.feature, *sums).where(U.created_at >= start).group_by(U.feature)
        ).all()
        by_model = s.execute(
            select(U.model, func.count(U.id), func.coalesce(func.sum(U.cost_micro_usd), 0))
            .where(U.created_at >= start).group_by(U.model)
        ).all()
        unpriced = s.execute(
            select(func.count(U.id)).where(U.created_at >= start, U.priced.is_(False))
        ).scalar_one()
        first = s.execute(select(func.min(U.created_at))).scalar_one()
        budget_used = s.execute(
            select(RateCounter.count).where(
                RateCounter.key == limits.BUDGET_KEY,
                RateCounter.window_start == int(now.timestamp() // limits.DAY * limits.DAY))
        ).scalar_one_or_none() or 0

        this_month = totals(s, month_start)
        last_month = totals(s, prev_month_start, month_start)
        today_t = totals(s, dt.datetime.combine(today, dt.time(), tzinfo=dt.timezone.utc))
        window = totals(s, start)

    # per day, per feature, every day in the window present (zero days too)
    daily = {}
    for i in range(days):
        d = (today - dt.timedelta(days=days - 1 - i)).isoformat()
        daily[d] = {"date": d, "usd": 0.0, "by_feature": {}}
    for created, feature, cost in day_rows:
        created = created if created.tzinfo else created.replace(tzinfo=dt.timezone.utc)
        d = created.astimezone(dt.timezone.utc).date().isoformat()
        if d in daily:
            daily[d]["usd"] += (cost or 0) / 1_000_000
            daily[d]["by_feature"][feature] = daily[d]["by_feature"].get(feature, 0) + (cost or 0) / 1_000_000
    for row in daily.values():
        row["usd"] = round(row["usd"], 4)
        row["by_feature"] = {k: round(v, 4) for k, v in row["by_feature"].items()}

    # month projection: what this month has cost so far, per day elapsed
    days_in = (today - month_start.date()).days + 1
    next_month = (month_start + dt.timedelta(days=32)).replace(day=1)
    month_len = (next_month - month_start).days

    features = []
    for feature, calls, cost, inp, out, cw, cr in by_feature:
        prompt = inp + cw + cr
        features.append({
            "feature": feature, "label": FEATURES.get(feature, feature), "calls": calls,
            "usd": _usd(cost), "per_call_usd": _usd(cost / calls) if calls else 0,
            "input": inp, "output": out, "cache_write": cw, "cache_read": cr,
            "cache_share": round(cr / prompt, 3) if prompt else None,
        })
    features.sort(key=lambda f: f["usd"], reverse=True)

    return {
        "generated_at": now.isoformat(),
        "days": days,
        "tracking_since": first.isoformat() if first else None,
        "today": today_t,
        "this_month": dict(this_month, projected_usd=round(this_month["usd"] / days_in * month_len, 2),
                           days_elapsed=days_in, days_in_month=month_len),
        "last_month": last_month,
        "window": window,
        "daily": list(daily.values()),
        "features": features,
        "models": sorted(({"model": m, "calls": c, "usd": _usd(cost), "priced": price_for(m) is not None}
                          for m, c, cost in by_model), key=lambda r: r["usd"], reverse=True),
        "unpriced_calls": unpriced,
        "budget": {"used_today": int(budget_used), "limit": limits.daily_budget()},
        "feature_labels": FEATURES,
    }
