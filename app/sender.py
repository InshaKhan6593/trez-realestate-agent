"""Send a WhatsApp text through the Meta Cloud API, or record a dry run."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SendResult:
    status: str                    # sent | failed | not_sent
    wa_message_id: str | None = None
    error: str | None = None


async def send_text(settings: Settings, to: str, body: str) -> SendResult:
    if settings.dry_run:
        log.info("dry run, not sent to %s: %s", to, body)
        return SendResult("not_sent", error="dry run: WHATSAPP_ACCESS_TOKEN not set")

    url = (f"https://graph.facebook.com/{settings.whatsapp_api_version}/"
           f"{settings.whatsapp_phone_number_id}/messages")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {settings.whatsapp_access_token}"},
                json={"messaging_product": "whatsapp", "to": to, "type": "text",
                      "text": {"body": body, "preview_url": False}},
            )
        if resp.status_code >= 400:
            return SendResult("failed", error=f"HTTP {resp.status_code}: {resp.text[:300]}")
        return SendResult("sent", wa_message_id=resp.json()["messages"][0]["id"])
    except httpx.HTTPError as err:
        return SendResult("failed", error=f"{type(err).__name__}: {err}")
