"""What running Alister and Miton Talent costs, provider by provider.

Two kinds of numbers end up in provider_costs:

  from an API   Anthropic and OpenAI report what they bill per day. A sync
                (scripts/sync_costs.py from cron, or the button on the page)
                reads the last days and replaces those days' rows, so a day
                the provider revises later ends up right. These are the
                organisation's whole bills: Alister and Talent, dev and prod.
  by hand       Bright Data and Hetzner have no cost API we can read, so their
                monthly invoice is typed in on the page.

Each API needs its own admin key, in .env:

  ANTHROPIC_ADMIN_KEY   sk-ant-admin...  (Console, Settings, Admin keys)
  OPENAI_ADMIN_KEY      sk-admin-...     (platform.openai.com, Admin keys)

Without a key that provider is simply not synced and the page says so.
Neither key can send messages; both can read the organisation's billing.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal

import httpx
from sqlalchemy import delete, select

from talent import db
from talent.models import ProviderCost

log = logging.getLogger("miton-talent.costs")

PROVIDERS = {
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "brightdata": "Bright Data",
    "hetzner": "Hetzner",
    "other": "Ostatní",
}
MANUAL_PROVIDERS = ("brightdata", "hetzner", "other")
CURRENCIES = ("USD", "EUR")

ANTHROPIC_URL = "https://api.anthropic.com/v1/organizations"
OPENAI_URL = "https://api.openai.com/v1/organization"
TIMEOUT = 30.0
# Anthropic's cost analytics spans at most 31 days per request.
ANTHROPIC_MAX_SPAN = 31


class SyncError(Exception):
    pass


def eur_usd() -> float:
    """EUR -> USD for the totals. Approximate on purpose: the invoices in EUR
    are paid in EUR, this only puts them on one scale next to the USD ones."""
    try:
        return float(os.environ.get("COST_EUR_USD", "1.10"))
    except ValueError:
        return 1.10


def to_usd(amount_micro: int, currency: str) -> float:
    usd = amount_micro / 1_000_000
    return usd * eur_usd() if currency == "EUR" else usd


def configured() -> dict:
    return {
        "anthropic": bool(os.environ.get("ANTHROPIC_ADMIN_KEY")),
        "openai": bool(os.environ.get("OPENAI_ADMIN_KEY")),
    }


# --- Anthropic ------------------------------------------------------------------


def _anthropic_headers(key: str) -> dict:
    return {
        "x-api-key": key,
        "anthropic-version": "2026-10-21",
        "anthropic-beta": "analytics-usage-cost-2026-08-24",
    }


def _anthropic_workspaces(http: httpx.Client, key: str) -> dict[str, str]:
    """Workspace id -> name. Best effort: a failure leaves ids on the page."""
    names: dict[str, str] = {}
    try:
        params = {"include_default": "true", "limit": 100}
        while True:
            r = http.get(f"{ANTHROPIC_URL}/workspaces", headers=_anthropic_headers(key), params=params)
            r.raise_for_status()
            body = r.json()
            for w in body.get("data") or []:
                if w.get("id"):
                    names[w["id"]] = w.get("name") or w["id"]
            if not body.get("has_more") or not body.get("last_id"):
                break
            params["after_id"] = body["last_id"]
    except Exception as exc:
        log.warning("anthropic workspaces not listed: %s", exc)
    return names


def fetch_anthropic(start: dt.date, end: dt.date, *, http: httpx.Client | None = None) -> list[dict]:
    """[{day, scope, usd}] for start <= day < end, per workspace."""
    key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if not key:
        raise SyncError("ANTHROPIC_ADMIN_KEY chybí")
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT)
    try:
        names = _anthropic_workspaces(http, key)
        out: list[dict] = []
        chunk = start
        while chunk < end:
            chunk_end = min(end, chunk + dt.timedelta(days=ANTHROPIC_MAX_SPAN))
            params: dict = {
                "starting_at": f"{chunk.isoformat()}T00:00:00Z",
                "ending_at": f"{chunk_end.isoformat()}T00:00:00Z",
                "time_bucket_width": "1d",
                "group_by[]": "workspace_id",
                "order_by": "time",
                "order": "asc",
                "limit": 1000,
            }
            while True:
                r = http.get(f"{ANTHROPIC_URL}/analytics/cost", headers=_anthropic_headers(key), params=params)
                if r.status_code >= 400:
                    raise SyncError(f"Anthropic {r.status_code}: {r.text[:200]}")
                body = r.json()
                for row in body.get("data") or []:
                    cost = row.get("cost") or {}
                    # decimal string in CENTS with six places
                    cents = Decimal(str(cost.get("amount") or "0"))
                    ws = (row.get("group") or {}).get("workspace_id") or ""
                    out.append({
                        "day": dt.date.fromisoformat(row["starting_at"][:10]),
                        "scope": names.get(ws, ws or "Default"),
                        "usd": float(cents / 100),
                    })
                if not body.get("has_more") or not body.get("next_page"):
                    break
                params["page"] = body["next_page"]
            chunk = chunk_end
        return out
    finally:
        if own:
            http.close()


# --- OpenAI ---------------------------------------------------------------------------


def _openai_projects(http: httpx.Client, key: str) -> dict[str, str]:
    names: dict[str, str] = {}
    try:
        params: dict = {"limit": 100, "include_archived": "true"}
        while True:
            r = http.get(f"{OPENAI_URL}/projects", headers={"Authorization": f"Bearer {key}"}, params=params)
            r.raise_for_status()
            body = r.json()
            for p in body.get("data") or []:
                if p.get("id"):
                    names[p["id"]] = p.get("name") or p["id"]
            if not body.get("has_more") or not body.get("last_id"):
                break
            params["after"] = body["last_id"]
    except Exception as exc:
        log.warning("openai projects not listed: %s", exc)
    return names


def fetch_openai(start: dt.date, end: dt.date, *, http: httpx.Client | None = None) -> list[dict]:
    """[{day, scope, usd}] for start <= day < end, per project."""
    key = os.environ.get("OPENAI_ADMIN_KEY", "")
    if not key:
        raise SyncError("OPENAI_ADMIN_KEY chybí")
    own = http is None
    http = http or httpx.Client(timeout=TIMEOUT)
    try:
        names = _openai_projects(http, key)
        epoch = lambda d: int(dt.datetime.combine(d, dt.time(), tzinfo=dt.timezone.utc).timestamp())  # noqa: E731
        params: dict = {"start_time": epoch(start), "end_time": epoch(end), "bucket_width": "1d",
                        "group_by": "project_id", "limit": 180}
        out: list[dict] = []
        while True:
            r = http.get(f"{OPENAI_URL}/costs", headers={"Authorization": f"Bearer {key}"}, params=params)
            if r.status_code >= 400:
                raise SyncError(f"OpenAI {r.status_code}: {r.text[:200]}")
            body = r.json()
            for bucket in body.get("data") or []:
                day = dt.datetime.fromtimestamp(bucket["start_time"], tz=dt.timezone.utc).date()
                for res in bucket.get("results") or []:
                    amount = res.get("amount") or {}
                    pid = res.get("project_id") or ""
                    out.append({"day": day, "scope": names.get(pid, pid or "bez projektu"),
                                "usd": float(amount.get("value") or 0)})
            if not body.get("has_more") or not body.get("next_page"):
                break
            params["page"] = body["next_page"]
        return out
    finally:
        if own:
            http.close()


FETCHERS = {"anthropic": fetch_anthropic, "openai": fetch_openai}


# --- writing ----------------------------------------------------------------------------


def store_api_rows(provider: str, start: dt.date, end: dt.date, rows: list[dict]) -> int:
    """Replace the provider's API rows for start <= day < end with ``rows``.
    Rows for the same day and scope are added up (a provider may split a day
    by line item even when we only group by project)."""
    merged: dict[tuple, float] = {}
    for r in rows:
        if start <= r["day"] < end:
            k = (r["day"], (r["scope"] or "")[:128])
            merged[k] = merged.get(k, 0.0) + r["usd"]
    with db.session() as s:
        s.execute(delete(ProviderCost).where(
            ProviderCost.provider == provider, ProviderCost.source == "api",
            ProviderCost.day >= start, ProviderCost.day < end))
        for (day, scope), usd in merged.items():
            s.add(ProviderCost(provider=provider, source="api", day=day, scope=scope,
                               amount_micro=int(round(usd * 1_000_000)), currency="USD", updated_by="sync"))
    return len(merged)


def sync(days: int = 35, *, since: dt.date | None = None, today: dt.date | None = None,
         providers: tuple[str, ...] | None = None) -> dict:
    """Read the last ``days`` days (or everything from ``since``) from every
    provider with a key. One provider failing does not stop the other.
    Returns {provider: {"ok", "rows" | "error"}}."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    end = today + dt.timedelta(days=1)          # today is partial, still worth showing
    start = since or (today - dt.timedelta(days=days - 1))
    result = {}
    for name, fetch in FETCHERS.items():
        if providers and name not in providers:
            continue
        if not configured()[name]:
            result[name] = {"ok": False, "error": "chybí klíč"}
            continue
        try:
            rows = fetch(start, end)
            result[name] = {"ok": True, "rows": store_api_rows(name, start, end, rows)}
        except Exception as exc:
            log.warning("cost sync %s failed: %s", name, exc)
            result[name] = {"ok": False, "error": str(exc)[:300]}
    # The page shows when each provider last synced from the newest
    # updated_at of its API rows, so nothing else needs storing here.
    log.info("cost sync %s", result)
    return result


# --- by hand --------------------------------------------------------------------------


def set_manual(provider: str, month: str, amount: float, currency: str, note: str | None,
               who: str | None) -> dict:
    """Upsert one month's invoice. ``month`` is YYYY-MM; amount 0 deletes it."""
    if provider not in MANUAL_PROVIDERS:
        raise ValueError("unknown provider")
    if currency not in CURRENCIES:
        raise ValueError("unknown currency")
    try:
        day = dt.date.fromisoformat(f"{month}-01")
    except ValueError:
        raise ValueError("month must be YYYY-MM")
    if amount < 0 or amount > 1_000_000:
        raise ValueError("amount out of range")
    with db.session() as s:
        row = s.scalars(select(ProviderCost).where(
            ProviderCost.provider == provider, ProviderCost.source == "manual",
            ProviderCost.day == day, ProviderCost.scope == "")).first()
        if amount == 0:
            if row is not None:
                s.delete(row)
            return {"deleted": True}
        if row is None:
            row = ProviderCost(provider=provider, source="manual", day=day, scope="")
            s.add(row)
        row.amount_micro = int(round(amount * 1_000_000))
        row.currency = currency
        row.note = (note or "").strip()[:500] or None
        row.updated_by = who
    return {"deleted": False}


# --- reading ----------------------------------------------------------------------------


def _month(d: dt.date) -> dt.date:
    return d.replace(day=1)


def _add_months(d: dt.date, n: int) -> dt.date:
    y, m = divmod(d.month - 1 + n, 12)
    return dt.date(d.year + y, m + 1, 1)


def summary(today: dt.date | None = None, months: int = 6) -> dict:
    """Month by month, provider by provider, in USD, plus where each number
    comes from. The current month's projection scales the API providers by
    days elapsed and takes a hand-entered provider's last known month when
    this month's invoice is not in yet."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    this = _month(today)
    first = _add_months(this, -(months - 1))
    month_keys = [_add_months(first, i) for i in range(months)]

    with db.session() as s:
        rows = s.scalars(select(ProviderCost).where(ProviderCost.day >= _add_months(first, -3))).all()
        rows = [(r.provider, r.source, r.day, r.scope, r.amount_micro, r.currency, r.note, r.updated_at, r.updated_by)
                for r in rows]

    table: dict[str, dict[str, float]] = {}
    scopes: dict[str, dict[str, dict[str, float]]] = {}
    last_api: dict[str, dt.datetime] = {}
    last_api_day: dict[str, dt.date] = {}
    manual = []
    for provider, source, day, scope, micro, currency, note, updated_at, by in rows:
        usd = to_usd(micro, currency)
        mk = _month(day).isoformat()
        table.setdefault(provider, {})
        table[provider][mk] = table[provider].get(mk, 0.0) + usd
        if source == "api":
            sc = scopes.setdefault(provider, {}).setdefault(mk, {})
            sc[scope or "–"] = sc.get(scope or "–", 0.0) + usd
            if provider not in last_api or updated_at > last_api[provider]:
                last_api[provider] = updated_at
            if usd > 0 and (provider not in last_api_day or day > last_api_day[provider]):
                last_api_day[provider] = day
        else:
            manual.append({"provider": provider, "label": PROVIDERS.get(provider, provider),
                           "month": day.strftime("%Y-%m"), "amount": micro / 1_000_000, "currency": currency,
                           "usd": round(usd, 2), "note": note, "updated_by": by})

    days_in = (today - this).days + 1
    month_len = (_add_months(this, 1) - this).days
    providers = []
    for p in PROVIDERS:
        per_month = {m.isoformat(): round(table.get(p, {}).get(m.isoformat(), 0.0), 2) for m in month_keys}
        has_any = p in table
        is_api = p in FETCHERS
        if not has_any and not (is_api and configured().get(p)) and p == "other":
            continue
        now_usd = table.get(p, {}).get(this.isoformat(), 0.0)
        if is_api:
            projected, basis = now_usd / days_in * month_len, "api"
        elif this.isoformat() in table.get(p, {}):
            projected, basis = now_usd, "manual"
        else:
            prev = [m for m in sorted(table.get(p, {})) if m < this.isoformat()]
            projected = table[p][prev[-1]] if prev else 0.0
            basis = "last_month" if prev else "none"
        providers.append({
            "provider": p, "label": PROVIDERS[p], "kind": "api" if is_api else "manual",
            "configured": configured().get(p, True) if is_api else True,
            "months": per_month, "projected_usd": round(projected, 2), "projection_basis": basis,
            "last_sync": last_api[p].isoformat() if p in last_api else None,
            "last_day": last_api_day[p].isoformat() if p in last_api_day else None,
            "scopes_this_month": sorted(({"scope": k, "usd": round(v, 2)} for k, v in
                                         scopes.get(p, {}).get(this.isoformat(), {}).items()),
                                        key=lambda r: r["usd"], reverse=True),
            "scopes_last_month": sorted(({"scope": k, "usd": round(v, 2)} for k, v in
                                         scopes.get(p, {}).get(_add_months(this, -1).isoformat(), {}).items()),
                                        key=lambda r: r["usd"], reverse=True),
        })

    totals = {m.isoformat(): round(sum(pr["months"][m.isoformat()] for pr in providers), 2) for m in month_keys}
    manual.sort(key=lambda r: (r["month"], r["provider"]), reverse=True)
    return {
        "today": today.isoformat(),
        "this_month": this.isoformat(),
        "last_month": _add_months(this, -1).isoformat(),
        "months": [m.isoformat() for m in month_keys],
        "providers": providers,
        "totals": totals,
        "projected_usd": round(sum(p["projected_usd"] for p in providers), 2),
        "days_elapsed": days_in,
        "days_in_month": month_len,
        "manual": manual[:24],
        "manual_providers": [{"id": p, "label": PROVIDERS[p]} for p in MANUAL_PROVIDERS],
        "eur_usd": eur_usd(),
    }
