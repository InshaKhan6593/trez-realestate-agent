"""Error reporting with Sentry (ARCHITECTURE.md §15, layer 3).

Off unless SENTRY_DSN is set. Errors only (no performance tracing: Langfuse
does that). Phone numbers are masked before an event leaves, the same way as
in Langfuse, and Sentry's own personal-data collection stays off.
"""

from __future__ import annotations

import os

from agent.trace import mask


def init_sentry(component: str) -> bool:
    """Start Sentry for this process ("webhook" or "worker"). -> on or off."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    import sentry_sdk

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        send_default_pii=False,
        traces_sample_rate=0,
        before_send=_masked,
    )
    sentry_sdk.set_tag("component", component)
    return True


def _masked(event: dict, _hint: dict) -> dict:
    """Mask phone numbers anywhere in the event: messages, exception text,
    log records, local variables."""
    return mask(data=event)
