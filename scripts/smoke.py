"""End-to-end check of a deployed bot, without WhatsApp: send the webhook a
signed message shaped exactly like Meta's, then read back what the worker did.

    uv run python -m scripts.smoke https://<domain> "Askari 6 mein ghar chahiye"

Needs in the environment (not .env, which holds the local values):
    SMOKE_APP_SECRET   the deployed WHATSAPP_APP_SECRET (signs the request)
    SMOKE_DATABASE_URL the deployed DATABASE_URL (reads the turn back)

Uses a test buyer (9299 55556xxx). With no WhatsApp token set the bot is in dry
run: the reply is recorded as not_sent and nothing reaches WhatsApp.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx
import psycopg

from tests.meta_payloads import signed, text_message

PHONE = "929955556001"


def main() -> int:
    base, text = sys.argv[1].rstrip("/"), sys.argv[2] if len(sys.argv) > 2 else "Assalam o alaikum, ghar chahiye"
    secret, db = os.environ["SMOKE_APP_SECRET"], os.environ["SMOKE_DATABASE_URL"]

    print("health:", httpx.get(f"{base}/health", timeout=15).text)
    wamid = f"wamid.smoke.{uuid.uuid4().hex}"
    body, headers = signed(text_message(PHONE, wamid, text), secret)
    resp = httpx.post(f"{base}/webhook", content=body, headers=headers, timeout=15)
    print("webhook:", resp.status_code)
    if resp.status_code != 200:
        return 1

    # The worker waits DEBOUNCE_SECONDS for more messages, then runs the turn.
    with psycopg.connect(db, autocommit=True) as conn:
        for _ in range(45):
            row = conn.execute(
                """SELECT t.status, t.reply, t.latency_ms, t.langfuse_trace_id, t.error
                   FROM messages m JOIN turns t ON t.id = m.turn_id
                   WHERE m.wa_message_id = %s AND t.finished_at IS NOT NULL""", (wamid,)).fetchone()
            if row:
                status, reply, latency, trace_id, error = row
                print(f"turn: {status} in {latency} ms; trace {trace_id}")
                print("reply:", reply or error)
                return 0 if status in ("sent", "not_sent") else 1
            time.sleep(2)
    print("no finished turn after 90 s: check the worker's logs")
    return 1


if __name__ == "__main__":
    sys.exit(main())
