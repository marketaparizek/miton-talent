"""Token spend: every model call becomes an llm_usage row, and /admin/usage adds them up.

No test talks to Anthropic.
"""
import datetime as dt
import os
import sys
from types import SimpleNamespace

import jwt
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app as chat  # noqa: E402
from talent import db, limits, usage  # noqa: E402
from talent.models import Base, LlmUsage  # noqa: E402

SECRET = "test-handoff-secret-shared-with-alister-32plus"


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    db.reset_for_tests(f"sqlite+pysqlite:///{tmp_path / 'u.db'}")
    Base.metadata.create_all(db.engine())
    monkeypatch.delenv("LLM_DAILY_BUDGET", raising=False)
    monkeypatch.delenv("USAGE_DB", raising=False)


def _resp(i=1000, o=200, cw=0, cr=0):
    return SimpleNamespace(usage=SimpleNamespace(input_tokens=i, output_tokens=o,
                                                 cache_creation_input_tokens=cw, cache_read_input_tokens=cr))


def rows():
    with db.session() as s:
        return s.query(LlmUsage).order_by(LlmUsage.id).all()


# --- prices -----------------------------------------------------------------------


def test_cost_counts_input_output_and_both_cache_kinds():
    # Sonnet 4.6: 3 / 15 $ per million, write 1.25x input, read 0.30
    cost, priced = usage.cost_micro_usd("claude-sonnet-4-6", 1_000_000, 100_000, 10_000, 1_000_000)
    assert priced
    assert cost == round(3_000_000 + 1_500_000 + 37_500 + 300_000)


def test_the_longest_prefix_wins_and_dated_ids_find_their_model():
    assert usage.price_for("claude-sonnet-5-5") == usage.PRICES["claude-sonnet-5-5"]
    assert usage.price_for("claude-sonnet-5") == usage.PRICES["claude-sonnet-5"]
    assert usage.price_for("claude-haiku-4-5-20251001") == usage.PRICES["claude-haiku-4-5"]


def test_an_unknown_model_is_recorded_unpriced(fresh_db):
    usage.log_usage("chat", _resp(), model="claude-unknown-9")
    (r,) = rows()
    assert r.priced is False and r.cost_micro_usd == 0 and r.input_tokens == 1000


# --- recording ----------------------------------------------------------------------


def test_a_chat_reply_writes_one_row_with_counts_only(fresh_db, monkeypatch):
    block = SimpleNamespace(type="tool_use", name="record_reply",
                            input={"reply": "Ahoj!", "profile": {}, "summary": "", "stage": "exploring"})
    fake = SimpleNamespace(create=lambda **kw: SimpleNamespace(
        content=[block], usage=_resp(40, 60, 0, 1700).usage, model=kw.get("model")))
    monkeypatch.setattr(chat, "client", SimpleNamespace(messages=fake))
    r = TestClient(chat.app).post("/chat", json={"lang": "cs", "message": "tajná zpráva"},
                                  headers={"X-Forwarded-For": "203.0.113.20"})
    assert r.status_code == 200
    (row,) = rows()
    assert (row.feature, row.model) == ("chat", chat.MODEL)
    assert (row.input_tokens, row.output_tokens, row.cache_read_tokens) == (40, 60, 1700)
    assert row.cost_micro_usd > 0
    assert "tajná" not in repr(vars(row))


def test_a_broken_database_never_breaks_the_call(monkeypatch, caplog):
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "session", boom)
    usage.log_usage("chat", _resp(), model="claude-sonnet-4-6")  # no exception
    assert any("could not record usage" in r.getMessage() for r in caplog.records)


# --- summary --------------------------------------------------------------------------


def _add(when, feature, model="claude-sonnet-4-6", i=1_000_000, o=0, cr=0):
    cost, priced = usage.cost_micro_usd(model, i, o, 0, cr)
    with db.session() as s:
        s.add(LlmUsage(created_at=when, feature=feature, model=model, input_tokens=i, output_tokens=o,
                       cache_write_tokens=0, cache_read_tokens=cr, cost_micro_usd=cost, priced=priced))


def test_summary_splits_today_month_last_month_and_features(fresh_db):
    now = dt.datetime(2026, 11, 22, 15, 0, tzinfo=dt.timezone.utc)
    _add(now - dt.timedelta(hours=1), "chat")                      # 3 $ today
    _add(dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc), "score")  # 3 $ this month
    _add(dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc), "chat")  # 3 $ last month
    _add(now - dt.timedelta(hours=2), "chat", i=0, cr=1_000_000)   # 0.30 $ cached today

    out = usage.summary(now=now)
    assert out["today"]["usd"] == pytest.approx(3.30)
    assert out["this_month"]["usd"] == pytest.approx(6.30)
    assert out["this_month"]["days_elapsed"] == 22 and out["this_month"]["days_in_month"] == 30
    assert out["this_month"]["projected_usd"] == pytest.approx(round(6.30 / 22 * 30, 2))
    assert out["last_month"]["usd"] == pytest.approx(3.0)
    assert [f["feature"] for f in out["features"]] == ["chat", "score"]
    chat_row = out["features"][0]
    assert chat_row["calls"] == 3 and chat_row["cache_share"] == pytest.approx(1 / 3, abs=1e-3)
    assert len(out["daily"]) == 30 and out["daily"][-1]["date"] == "2026-11-22"
    assert out["daily"][-1]["by_feature"]["chat"] == pytest.approx(3.30)
    assert out["unpriced_calls"] == 0


def test_summary_reads_todays_budget_counter(fresh_db):
    now = dt.datetime(2026, 11, 22, 15, 0, tzinfo=dt.timezone.utc)
    for _ in range(3):
        limits.take_llm_call(now=now.timestamp())
    assert usage.summary(now=now)["budget"] == {"used_today": 3, "limit": limits.daily_budget()}


def test_summary_of_an_empty_table(fresh_db):
    out = usage.summary()
    assert out["tracking_since"] is None and out["features"] == [] and out["window"]["usd"] == 0


# --- the page ---------------------------------------------------------------------------


@pytest.fixture()
def http(fresh_db, monkeypatch):
    monkeypatch.setenv("TALENT_HANDOFF_SECRET", SECRET)
    monkeypatch.setenv("ALISTER_BASE_URL", "https://alister.example.test")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    return TestClient(chat.app)


def sign_in(client, role):
    now = dt.datetime.now(dt.timezone.utc)
    token = jwt.encode({"sub": "1", "email": "marketa.parizek@miton.cz", "role": role, "purpose": "talent-handoff",
                        "aud": "miton-talent", "jti": os.urandom(8).hex(), "iat": now,
                        "exp": now + dt.timedelta(seconds=60)}, SECRET, algorithm="HS256")
    assert client.get(f"/auth/callback?token={token}", follow_redirects=False).status_code == 302


def test_the_page_is_for_the_admin_role_only(http):
    assert http.get("/admin/usage.json").status_code == 401
    sign_in(http, "miton")
    assert http.get("/admin/usage").status_code == 403
    assert "útrata za modely" not in http.get("/admin").text


def test_the_admin_sees_the_page_with_numbers_inside(http):
    _add(dt.datetime.now(dt.timezone.utc), "chat", model="claude-sonnet-4-6")
    sign_in(http, "admin")
    page = http.get("/admin/usage")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "__DATA__" not in page.text and '"feature": "chat"' in page.text
    assert http.get("/admin/usage.json").json()["window"]["calls"] == 1
    assert "/admin/usage" in http.get("/admin").text
