"""One turn: answer everything a buyer sent in one burst, exactly once.

    seq moved on?          -> stale: a newer job will answer the whole burst
    lead locked?           -> locked: retry shortly
    claim unanswered msgs  -> nothing to answer? done
    agent has taken it?    -> log only, the agent is replying by hand
    build reply            (agent.graph: extract -> plan -> tools -> respond -> check)
    seq moved on now?      -> superseded: hand the messages back, the newer job
                              answers everything (§5: discard draft, regenerate)
    agent took it now?     -> takeover: draft kept for the record, never sent
    send text, then media; alert the agent if a handoff was requested
    commit the buyer's memory
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from . import inbox, store
from .config import Settings
from .sender import SendResult

log = logging.getLogger(__name__)

# (conn, lead_id, turn_id, claimed messages) -> an AgentReply-like object with
# .text, .media_listing_id, .photos, .video, .alert and async .commit(conn)
ReplyFn = Callable[[AsyncConnection, int, int, list[dict]], Awaitable[object]]
SendFn = Callable[..., Awaitable[SendResult]]

TURN_STATUS = {"sent": "sent", "not_sent": "not_sent", "failed": "failed"}


async def run_turn(redis: Redis, pool: AsyncConnectionPool, settings: Settings,
                   lead_id: int, seq: int, *, reply_fn: ReplyFn, send_fn: SendFn,
                   media_fn=None) -> str:
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
                reply = await reply_fn(conn, lead_id, claimed.turn_id, claimed.messages)
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
                await store.finish_turn(conn, claimed.turn_id, "takeover", reply=reply.text)
                return "takeover"

            result = await send_fn(settings, claimed.phone, reply.text)
            now = datetime.now(timezone.utc)
            await store.save_outbound(conn, lead_id, claimed.turn_id, reply.text,
                                      result.wa_message_id, result.status, result.error, now)

            if result.status != "failed" and media_fn and getattr(reply, "media_listing_id", None):
                for sent in await media_fn(conn, settings, claimed.phone, reply.media_listing_id,
                                           photos=4 if reply.photos else 0, videos=reply.video):
                    await store.save_outbound(
                        conn, lead_id, claimed.turn_id, sent.ref, sent.result.wa_message_id,
                        sent.result.status, sent.result.error, datetime.now(timezone.utc),
                        type="image" if sent.kind == "photo" else "text")

            # Memory first, so a handoff alert includes what this turn was about.
            await reply.commit(conn)
            alert = getattr(reply, "alert", None)
            if alert:
                alert_result = await send_fn(settings, alert["phone"], alert["text"])
                log.info("handoff %s alert to agent: %s", alert.get("handoff_id"), alert_result.status)
            await store.finish_turn(
                conn, claimed.turn_id, TURN_STATUS[result.status], reply=reply.text, error=result.error,
                latency_ms=int((now - claimed.first_at).total_seconds() * 1000),
            )
            return result.status
    finally:
        await inbox.release_lock(redis, lead_id, token)
