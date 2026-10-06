"""One turn: answer everything a buyer sent in one burst, exactly once.

    seq moved on?          -> stale: a newer job will answer the whole burst
    lead locked?           -> locked: retry shortly
    claim unanswered msgs  -> nothing to answer? done
    agent has taken it?    -> log only, the agent is replying by hand
    build reply
    seq moved on now?      -> superseded: hand the messages back, the newer job
                              answers everything (§5: discard draft, regenerate)
    agent took it now?     -> takeover: draft kept for the record, never sent
    send + persist
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from . import inbox, store
from .config import Settings
from .sender import SendResult

log = logging.getLogger(__name__)

ReplyFn = Callable[[list[dict]], str]
SendFn = Callable[[Settings, str, str], Awaitable[SendResult]]

TURN_STATUS = {"sent": "sent", "not_sent": "not_sent", "failed": "failed"}


async def run_turn(redis: Redis, pool: AsyncConnectionPool, settings: Settings,
                   lead_id: int, seq: int, *, reply_fn: ReplyFn, send_fn: SendFn) -> str:
    if await inbox.current_seq(redis, lead_id) != seq:
        return "stale"
    token = await inbox.acquire_lock(redis, lead_id)
    if token is None:
        return "locked"
    try:
        async with pool.connection() as conn:
            claimed = await store.claim_turn(conn, lead_id)
            if claimed is None:
                return "nothing"
            if claimed.agent_has_it:
                await store.finish_turn(conn, claimed.turn_id, "takeover")
                return "takeover"

            try:
                text = reply_fn(claimed.messages)
            except Exception as err:
                log.exception("reply failed for lead %s turn %s", lead_id, claimed.turn_id)
                await store.finish_turn(conn, claimed.turn_id, "failed", error=repr(err))
                return "failed"

            if await inbox.current_seq(redis, lead_id) != seq:
                await store.release_turn(conn, claimed.turn_id)
                return "superseded"
            # The agent may have taken the chat while the draft was written:
            # never let the bot and the agent both answer.
            if await store.agent_has_taken(conn, lead_id):
                await store.finish_turn(conn, claimed.turn_id, "takeover", reply=text)
                return "takeover"

            result = await send_fn(settings, claimed.phone, text)
            now = datetime.now(timezone.utc)
            await store.save_outbound(conn, lead_id, claimed.turn_id, text,
                                      result.wa_message_id, result.status, result.error, now)
            await store.finish_turn(
                conn, claimed.turn_id, TURN_STATUS[result.status], reply=text, error=result.error,
                latency_ms=int((now - claimed.first_at).total_seconds() * 1000),
            )
            return result.status
    finally:
        await inbox.release_lock(redis, lead_id, token)
