"""Meta Cloud API webhook payloads, shaped like the real ones, for tests."""

from __future__ import annotations

import hashlib
import hmac
import json
import time

APP_SECRET = "test-app-secret"


def signed(payload: dict, secret: str = APP_SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"}


def envelope(value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA_ID", "changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "923000000000", "phone_number_id": "PNID"},
            **value,
        }}]}],
    }


def text_message(phone: str, wa_id: str, body: str, name: str = "Ahmed",
                 ts: int | None = None, context_id: str | None = None) -> dict:
    msg = {"from": phone, "id": wa_id, "timestamp": str(ts or int(time.time())),
           "type": "text", "text": {"body": body}}
    if context_id:
        msg["context"] = {"from": "923000000000", "id": context_id}
    return envelope({"contacts": [{"profile": {"name": name}, "wa_id": phone}], "messages": [msg]})


def status_update(wa_id: str, status: str, error: dict | None = None) -> dict:
    st = {"id": wa_id, "status": status, "timestamp": str(int(time.time())),
          "recipient_id": "923001234567"}
    if error:
        st["errors"] = [error]
    return envelope({"statuses": [st]})
