"""One log line per Anthropic call, so token spend and cache hits show in the logs.

    from talent.usage import log_usage
    log_usage("chat", resp, model=MODEL)

Only counts: never message contents, never anything about the candidate.
"""

from __future__ import annotations

import logging

log = logging.getLogger("miton-talent.usage")


def log_usage(feature: str, response, *, model: str | None = None) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return

    def n(name: str) -> int:
        value = getattr(usage, name, None)
        return value if isinstance(value, int) else 0

    log.info(
        "anthropic usage feature=%s model=%s input=%d output=%d cache_write=%d cache_read=%d",
        feature, model or getattr(response, "model", "?"),
        n("input_tokens"), n("output_tokens"),
        n("cache_creation_input_tokens"), n("cache_read_input_tokens"),
    )
