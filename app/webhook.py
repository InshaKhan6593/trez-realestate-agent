"""Ingress (ARCHITECTURE.md §5, box 1): verify, store, queue, return 200 fast.

    uv run uvicorn app.webhook:app --port 8000

Nothing slow happens here. Meta retries a webhook that is slow or fails, so
the request only stores the message and queues a turn; the arq worker does
the rest. A storage failure returns 500 on purpose: Meta retries, and the
unique wa_message_id makes the retry safe.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager

from arq import create_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from psycopg_pool import AsyncConnectionPool

from . import inbox, store
from .config import DB_CONNECT, get_settings
from .errors import init_sentry
from .meta import parse_webhook, signature_ok

log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_sentry("webhook")
    settings = get_settings()
    app.state.settings = settings
    app.state.db = AsyncConnectionPool(
        settings.database_url, kwargs=DB_CONNECT, min_size=1, max_size=5, open=False
    )
    await app.state.db.open()
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield
    await app.state.redis.aclose()
    await app.state.db.close()


app = FastAPI(title="Trez WhatsApp agent", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/webhook")
async def verify(request: Request) -> PlainTextResponse:
    """Meta's one-time handshake when the webhook URL is registered."""
    p = request.query_params
    token = request.app.state.settings.whatsapp_verify_token
    if token and p.get("hub.mode") == "subscribe" and p.get("hub.verify_token") == token:
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def receive(request: Request) -> dict:
    settings = request.app.state.settings
    raw = await request.body()
    if not signature_ok(raw, request.headers.get("X-Hub-Signature-256"),
                        settings.whatsapp_app_secret):
        raise HTTPException(status_code=401, detail="bad signature")

    inbound, statuses = parse_webhook(json.loads(raw))
    redis = request.app.state.redis
    async with request.app.state.db.connection() as conn:
        for st in statuses:
            await store.save_status(conn, st)
        for msg in inbound:
            lead_id = await store.save_inbound(conn, msg)
            if lead_id is None:
                continue                       # duplicate of an answered message
            seq = await inbox.bump_seq(redis, lead_id)
            await redis.enqueue_job(
                "process_turn", lead_id, seq,
                _job_id=f"turn:{lead_id}:{seq}",
                _defer_by=settings.debounce_seconds,
            )
    return {"ok": True}
