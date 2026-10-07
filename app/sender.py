"""Send WhatsApp messages through the Meta Cloud API, or record a dry run.

Dry run (no WHATSAPP_ACCESS_TOKEN): nothing reaches Meta and every result is
not_sent, never claimed as sent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)
DRY_RUN = "dry run: WHATSAPP_ACCESS_TOKEN not set"


@dataclass(frozen=True)
class SendResult:
    status: str                    # sent | failed | not_sent
    wa_message_id: str | None = None
    error: str | None = None


def _graph(settings: Settings, path: str) -> str:
    return (f"https://graph.facebook.com/{settings.whatsapp_api_version}/"
            f"{settings.whatsapp_phone_number_id}/{path}")


async def _send(settings: Settings, to: str, message: dict) -> SendResult:
    if settings.dry_run:
        log.info("dry run, not sent to %s: %s", to, message)
        return SendResult("not_sent", error=DRY_RUN)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                _graph(settings, "messages"),
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json={"messaging_product": "whatsapp", "to": to, **message},
            )
        if resp.status_code >= 400:
            return SendResult("failed", error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        return SendResult("sent", wa_message_id=resp.json()["messages"][0]["id"])
    except httpx.HTTPError as err:
        return SendResult("failed", error=f"{type(err).__name__}: {err}")


async def send_text(settings: Settings, to: str, body: str, *, preview_url: bool = False) -> SendResult:
    """preview_url=True shows a link card (used for listing video links)."""
    return await _send(settings, to, {"type": "text", "text": {"body": body, "preview_url": preview_url}})


async def send_template(settings: Settings, to: str, name: str, language: str, params: list[str]) -> SendResult:
    """An approved Meta template: the only message delivered outside the 24-hour window."""
    return await _send(settings, to, {"type": "template", "template": {
        "name": name, "language": {"code": language},
        "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
    }})


# Meta rejects a template parameter with newlines, tabs or 4+ spaces in a row, and
# caps the body at 1024 characters (the template's own words included).
TEMPLATE_PARAM_MAX = 900


def template_param(text: str) -> str:
    """Text made valid as one template parameter: lines joined with ' · ',
    spaces collapsed, cut to fit (the end marked with '…')."""
    one_line = " · ".join(" ".join(line.split()) for line in text.splitlines() if line.strip())
    return one_line if len(one_line) <= TEMPLATE_PARAM_MAX else one_line[:TEMPLATE_PARAM_MAX - 1] + "…"


async def send_alert(settings: Settings, to: str, text: str) -> SendResult:
    """The alert to a human agent. It is business-initiated: outside the 24
    hours after the agent last wrote to this number, Meta delivers only an
    approved template. With WHATSAPP_ALERT_TEMPLATE set, the alert goes as that
    template; without it, as plain text (delivered only inside that window)."""
    if settings.whatsapp_alert_template:
        return await send_template(settings, to, settings.whatsapp_alert_template,
                                   settings.whatsapp_alert_template_language, [template_param(text)])
    return await send_text(settings, to, text)


async def send_image(settings: Settings, to: str, media_id: str, caption: str | None = None) -> SendResult:
    image = {"id": media_id}
    if caption:
        image["caption"] = caption
    return await _send(settings, to, {"type": "image", "image": image})


async def upload_media(settings: Settings, content: bytes, mime: str, filename: str) -> str:
    """Upload a file to Meta once; the returned id is sent instead of a URL,
    so photos need no public hosting. Raises on failure (and in dry run)."""
    if settings.dry_run:
        raise RuntimeError(DRY_RUN)
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            _graph(settings, "media"),
            headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
            data={"messaging_product": "whatsapp", "type": mime},
            files={"file": (filename, content, mime)},
        )
    resp.raise_for_status()
    return resp.json()["id"]
