"""Tracing with Langfuse: every buyer turn as one readable trace.

    session  "lead-42"           one per buyer: the whole conversation, turn by turn
    trace    "whatsapp-turn"     one per turn; input = what the buyer sent,
                                 output = what the bot replied
      load-context    span        what we knew about the buyer before this turn
      extract-request generation  the extractor: prompt, JSON answer, tokens, cost
      plan-turn       chain       what the code decided (search? ask what? handoff?)
      run-tools       span        each tool (search-listings, get-listing, ...) with args and result
      write-reply     generation  the responder, once per attempt
      check-reply     guardrail   the validator: passed, or the problems it found
      use-template    span        only if the reply failed twice
      send-reply / send-media / save-memory / alert-agent    what went out, and to whom
    scores: reply-passed, used-template, handoff, fit, intent, delivery

Postgres (turns, messages) stays the business record; this is for reading and
debugging. Names are stable on purpose: filters and dashboards refer to them.

Off unless LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set: the SDK then
records nothing and every call here is a cheap no-op, so tests and offline
runs need no Langfuse. Phone numbers are masked before anything leaves.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from dotenv import load_dotenv
from langfuse import Langfuse, LangfuseMedia, propagate_attributes

TRACE_NAME = "whatsapp-turn"
# Pakistani mobile numbers, international (92 3xx...) or local (03xx...).
PHONE = re.compile(r"(?<!\d)(\+?92|0)(3\d{2})(\d{3})(\d{4})(?!\d)")

_client: Langfuse | None = None
_enabled = False


def _mask_text(text: str) -> str:
    return PHONE.sub(lambda m: f"{m.group(1)}{m.group(2)}•••{m.group(4)}", text)


def mask(*, data: Any, **_: Any) -> Any:
    """Langfuse mask hook: runs on every input, output and metadata."""
    if isinstance(data, str):
        return _mask_text(data)
    if isinstance(data, dict):
        return {k: mask(data=v) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask(data=v) for v in data]
    return data


def configure(client: Langfuse | None = None) -> Langfuse:
    """Set the client (tests pass one with an in-memory exporter), or build it
    from .env: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST,
    LANGFUSE_TRACING_ENVIRONMENT."""
    global _client, _enabled
    if client is None:
        load_dotenv()
        keys = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip(), os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
        _enabled = all(keys)
        client = Langfuse(public_key=keys[0] or "off", secret_key=keys[1] or "off",
                          tracing_enabled=_enabled, mask=mask)
    else:
        _enabled = True
    _client = client
    return client


def client() -> Langfuse:
    return _client or configure()


def enabled() -> bool:
    client()
    return _enabled


@contextmanager
def step(name: str, as_type: str = "span", **fields: Any) -> Iterator[Any]:
    """One observation under whatever is current (turn, node, tool...).
    `fields`: input, output, metadata, level, model, model_parameters, ..."""
    with client().start_as_current_observation(name=name, as_type=as_type, **fields) as obs:
        yield obs


@contextmanager
def turn(lead_id: int, turn_id: int, messages: list[dict], *, tags: list[str] | None = None) -> Iterator[Any]:
    """The root of one turn. Everything created inside belongs to this trace,
    and the trace to the buyer's session."""
    kinds = {m.get("type") for m in messages}
    tags = [*(tags or []), *(["voice-note"] if "audio" in kinds else [])]
    with client().start_as_current_observation(
        name=TRACE_NAME, as_type="agent", input=buyer_input(messages),
        metadata={"lead_id": lead_id, "turn_id": turn_id},
    ) as root, propagate_attributes(
        session_id=f"lead-{lead_id}", user_id=f"lead-{lead_id}", trace_name=TRACE_NAME,
        tags=tags or None, metadata={"turn_id": str(turn_id)},
    ):
        yield root


def buyer_input(messages: list[dict]) -> list[dict]:
    """What the buyer sent, as chat messages (Langfuse renders these as a
    conversation). A voice note shows its transcript when there is one, and
    plays inline when its audio is attached as `audio` (bytes, mime)."""
    out = []
    for m in messages:
        kind, text = m.get("type"), m.get("text")
        if kind == "text":
            content: Any = text or ""
        elif kind == "audio":
            label = f"[voice note] {text}" if text else "[voice note, not transcribed]"
            content = label
            if m.get("audio"):
                data, mime = m["audio"]
                content = [{"type": "text", "text": label},
                           {"type": "input_audio", "audio": LangfuseMedia(content_bytes=data, content_type=mime)}]
        else:
            content = f"[{kind}] {text}" if text else f"[{kind}]"
        out.append({"role": "user", "content": content})
    return out


def score(name: str, value: float | str | bool, *, comment: str | None = None) -> None:
    """A score on the current trace: filter traces by it in the UI."""
    if not enabled():
        return
    if isinstance(value, bool):
        client().score_current_trace(name=name, value=1 if value else 0, data_type="BOOLEAN", comment=comment)
    elif isinstance(value, str):
        client().score_current_trace(name=name, value=value, data_type="CATEGORICAL", comment=comment)
    else:
        client().score_current_trace(name=name, value=float(value), data_type="NUMERIC", comment=comment)


def trace_id() -> str | None:
    """Id of the current trace, to store on the turn (None when tracing is off)."""
    return client().get_current_trace_id() if enabled() else None


def url() -> str | None:
    """Link to the current trace in the Langfuse UI."""
    if not enabled():
        return None
    trace_id = client().get_current_trace_id()
    return client().get_trace_url(trace_id=trace_id) if trace_id else None


def flush() -> None:
    if _client is not None and _enabled:
        _client.flush()
