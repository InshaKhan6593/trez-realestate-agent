"""Meta WhatsApp Cloud API webhook: signature check and payload parsing. Pure."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timezone


def signature_ok(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """X-Hub-Signature-256 is 'sha256=' + HMAC-SHA256(app secret, raw body).

    Must be computed on the exact bytes received, before any JSON parsing.
    An unset secret never validates: fail closed, not open.
    """
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256="))


@dataclass(frozen=True)
class Inbound:
    wa_message_id: str
    phone: str                 # wa_id, digits only
    name: str | None           # WhatsApp profile name
    type: str
    text: str | None           # body, caption, button/list title, location...
    media_id: str | None
    context_id: str | None     # the message this one quotes
    at: datetime
    payload: dict              # the raw message object


@dataclass(frozen=True)
class Status:
    wa_message_id: str
    status: str                # sent | delivered | read | failed
    error: str | None


def _text_of(msg: dict) -> tuple[str | None, str | None]:
    """-> (text, media_id) for every message type a buyer can send."""
    kind = msg.get("type")
    body = msg.get(kind) or {}
    if kind == "text":
        return body.get("body"), None
    if kind in ("image", "video", "document", "sticker"):
        return body.get("caption"), body.get("id")
    if kind == "audio":                     # voice notes: transcribed in a later step
        return None, body.get("id")
    if kind == "location":
        parts = [body.get("name"), body.get("address")]
        label = ", ".join(p for p in parts if p)
        return f"[location {body.get('latitude')},{body.get('longitude')}] {label}".strip(), None
    if kind == "interactive":               # tapped a reply button or list row
        reply = body.get("button_reply") or body.get("list_reply") or {}
        return reply.get("title"), None
    if kind == "button":                    # tapped a template quick-reply
        return body.get("text"), None
    if kind == "reaction":
        return body.get("emoji"), None
    return None, None


def parse_webhook(payload: dict) -> tuple[list[Inbound], list[Status]]:
    inbound: list[Inbound] = []
    statuses: list[Status] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            names = {
                c.get("wa_id"): (c.get("profile") or {}).get("name")
                for c in value.get("contacts") or []
            }
            for msg in value.get("messages") or []:
                text, media_id = _text_of(msg)
                inbound.append(Inbound(
                    wa_message_id=msg["id"],
                    phone=msg["from"],
                    name=names.get(msg["from"]),
                    type=msg.get("type", "unknown"),
                    text=text,
                    media_id=media_id,
                    context_id=(msg.get("context") or {}).get("id"),
                    at=datetime.fromtimestamp(int(msg["timestamp"]), tz=timezone.utc),
                    payload=msg,
                ))
            for st in value.get("statuses") or []:
                errors = st.get("errors") or []
                statuses.append(Status(
                    wa_message_id=st["id"],
                    status=st["status"],
                    error="; ".join(
                        f"{e.get('code')}: {e.get('title') or e.get('message')}" for e in errors
                    ) or None,
                ))
    return inbound, statuses
