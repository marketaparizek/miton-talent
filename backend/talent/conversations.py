"""The public chat as the server saw it (chat_conversations).

The widget sends a conversation id and its newest message. The model gets
the turns stored here plus that one message, and only a reply the model
really produced is stored back. So the history the model reads, the turn cap
and the transcript a submission keeps are all the server's, not the browser's.

A conversation that ends in a submission is moved into the candidate row and
deleted here. One that does not is deleted after CHAT_RETENTION_DAYS: chat
text is personal data and an abandoned chat has no purpose to keep it for.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.orm import Session

from talent.models import ChatConversation, utcnow

_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_TURN_CHARS = 4000


def retention_days() -> int:
    return int(os.environ.get("CHAT_RETENTION_DAYS", "7"))


def get(session: Session, conversation_id: Optional[str]) -> Optional[ChatConversation]:
    if not conversation_id or not _ID_RE.match(conversation_id):
        return None
    return session.get(ChatConversation, conversation_id)


def start(session: Session, lang: str) -> ChatConversation:
    conv = ChatConversation(lang=lang, turns=[], user_turns=0, profile={}, stage="exploring")
    session.add(conv)
    session.flush()
    return conv


def record_exchange(conv: ChatConversation, user_text: str, reply: str, *,
                    profile: dict, summary: str, stage: str) -> None:
    """Store one visitor message and the model's reply to it, together."""
    conv.turns = list(conv.turns or []) + [
        {"role": "user", "content": user_text[:MAX_TURN_CHARS]},
        {"role": "assistant", "content": (reply or "")[:MAX_TURN_CHARS]},
    ]
    conv.user_turns = (conv.user_turns or 0) + 1
    if profile:
        conv.profile = profile
    if summary:
        conv.summary = summary
    conv.stage = stage or conv.stage
    conv.updated_at = utcnow()


def purge_stale(session: Session, now: Optional[dt.datetime] = None) -> int:
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=retention_days())
    res = session.execute(delete(ChatConversation).where(ChatConversation.updated_at < cutoff))
    return res.rowcount or 0
