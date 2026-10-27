"""The consent wordings the chat form shows, versioned on the server.

A candidate row records which version the person agreed to and the server's
copy of its text, never a text the browser sends: a browser can claim any
wording it likes. To change the wording, add a new version here, point
CURRENT at it and ship the widget with the same text and id. Never edit or
remove an old entry; rows that point at it must keep meaning what they meant.
"""

from __future__ import annotations

from typing import Optional

CONSENTS = {
    "chat-2025-cs": "Souhlasím se zpracováním osobních údajů za účelem oslovení s pracovní nabídkou.",
    "chat-2025-en": "I agree to the processing of my personal data so that I can be contacted about job opportunities.",
}

CURRENT = {"cs": "chat-2025-cs", "en": "chat-2025-en"}


def resolve(consent_id: Optional[str], lang: str) -> tuple[str, str]:
    """(version, text) for a submission. A known id wins; anything else (an old
    widget that sends no id, an id nobody issued) gets the current wording for
    the language, which is what the widget shows."""
    if consent_id in CONSENTS:
        return consent_id, CONSENTS[consent_id]
    version = CURRENT["en" if lang == "en" else "cs"]
    return version, CONSENTS[version]
