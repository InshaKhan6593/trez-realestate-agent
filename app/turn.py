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
                           (a Meta template when configured: see sender.send_alert)
    commit the buyer's memory
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from agent import trace

from . import inbox, store
from .config import Settings
from .sender import SendResult, send_alert

log = logging.getLogger(__name__)

# (conn, lead_id, turn_id, claimed messages) -> an AgentReply-like object with
# .text, .media_listing_id, .photos, .video, .alert and async .commit(conn)
ReplyFn = Callable[[AsyncConnection, int, int, list[dict]], Awaitable[object]]
SendFn = Callable[..., Awaitable[SendResult]]

TURN_STATUS = {"sent": "sent", "not_sent": "not_sent", "failed": "failed"}


async def run_turn(redis: Redis, pool: AsyncConnectionPool, settings: Settings,
                   lead_id: int, seq: int, *, reply_fn: ReplyFn, send_fn: SendFn,
                   media_fn=None, alert_fn: SendFn = send_alert) -> str:
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
            # One Langfuse trace per turn, in the buyer's session (agent.trace).
            with trace.turn(lead_id, claimed.turn_id, claimed.messages,
                            tags=["dry-run"] if settings.dry_run else None) as root:
                await store.set_trace(conn, claimed.turn_id, trace.trace_id())
                outcome = await _answer(redis, conn, settings, lead_id, seq, claimed, root,
                                        reply_fn=reply_fn, send_fn=send_fn, media_fn=media_fn,
                                        alert_fn=alert_fn)
                trace.score("outcome", outcome)
            return outcome
    finally:
        await inbox.release_lock(redis, lead_id, token)


async def _answer(redis: Redis, conn: AsyncConnection, settings: Settings, lead_id: int, seq: int,
                  claimed: store.Claimed, root, *, reply_fn: ReplyFn, send_fn: SendFn, media_fn,
                  alert_fn: SendFn) -> str:
    if claimed.agent_has_it:
        await store.finish_turn(conn, claimed.turn_id, "takeover")
        root.update(output="(bot silent: the agent is handling this chat)")
        return "takeover"

    try:
        reply = await reply_fn(conn, lead_id, claimed.turn_id, claimed.messages)
    except Exception as err:
        log.exception("reply failed for lead %s turn %s", lead_id, claimed.turn_id)
        await store.finish_turn(conn, claimed.turn_id, "failed", error=repr(err))
        root.update(output="(no reply: the agent failed)", level="ERROR", status_message=repr(err)[:500])
        return "failed"

    if await inbox.current_seq(redis, lead_id) != seq:
        await store.release_turn(conn, claimed.turn_id)
        root.update(output={"not_sent": "the buyer sent more; the next turn answers everything",
                            "draft": reply.text})
        return "superseded"
    # The agent may have taken the chat while the draft was written:
    # never let the bot and the agent both answer.
    if await store.agent_has_taken(conn, lead_id):
        await store.finish_turn(conn, claimed.turn_id, "takeover", reply=reply.text)
        root.update(output={"not_sent": "the agent took over while the reply was written",
                            "draft": reply.text})
        return "takeover"

    with trace.step("send-reply", as_type="tool", input={"to": claimed.phone, "text": reply.text}) as obs:
        result = await send_fn(settings, claimed.phone, reply.text)
        obs.update(output=_sent(result), level="ERROR" if result.status == "failed" else None)
    now = datetime.now(timezone.utc)
    await store.save_outbound(conn, lead_id, claimed.turn_id, reply.text,
                              result.wa_message_id, result.status, result.error, now)

    media = []
    if result.status != "failed" and media_fn and getattr(reply, "media_listing_id", None):
        with trace.step("send-media", as_type="tool", input={
                "listing_id": reply.media_listing_id, "photos": reply.photos, "video": reply.video}) as obs:
            for sent in await media_fn(conn, settings, claimed.phone, reply.media_listing_id,
                                       photos=None if reply.photos else 0, videos=reply.video):
                media.append({"kind": sent.kind, "ref": sent.ref, **_sent(sent.result)})
                await store.save_outbound(
                    conn, lead_id, claimed.turn_id, sent.ref, sent.result.wa_message_id,
                    sent.result.status, sent.result.error, datetime.now(timezone.utc),
                    type="image" if sent.kind == "photo" else "text")
            obs.update(output=media)

    # Memory first, so a handoff alert includes what this turn was about.
    await reply.commit(conn)
    alert = getattr(reply, "alert", None)
    if alert:
        how = (f"template {settings.whatsapp_alert_template}" if settings.whatsapp_alert_template
               else "text (delivered only inside the agent's 24-hour window)")
        with trace.step("alert-agent", as_type="tool",
                        input={"to": alert["phone"], "as": how, "text": alert["text"]}) as obs:
            alert_result = await alert_fn(settings, alert["phone"], alert["text"])
            obs.update(output=_sent(alert_result))
        log.info("handoff %s alert to agent: %s", alert.get("handoff_id"), alert_result.status)
    await store.finish_turn(
        conn, claimed.turn_id, TURN_STATUS[result.status], reply=reply.text, error=result.error,
        latency_ms=int((now - claimed.first_at).total_seconds() * 1000),
    )
    root.update(output=reply.text, metadata={"delivery": result.status, "media_messages": len(media),
                                             "agent_alerted": bool(alert)})
    score_reply(getattr(reply, "audit", {}))
    return result.status


def _sent(result: SendResult) -> dict:
    return {k: v for k, v in {"status": result.status, "wa_message_id": result.wa_message_id,
                              "error": result.error}.items() if v}


def score_reply(audit: dict) -> None:
    """Scores to filter traces by: did the reply pass the check first time,
    did it need the template, was the agent called in, how good is the lead."""
    validation = audit.get("validation") or {}
    if validation:
        trace.score("reply-passed", validation.get("attempts") == 1 and not validation.get("used_template"))
        trace.score("used-template", bool(validation.get("used_template")))
    trace.score("handoff", bool(audit.get("handoff")))
    scores = audit.get("scores") or {}
    for name in ("fit", "intent"):
        if isinstance(scores.get(name), int | float):
            trace.score(name, scores[name])
    if scores.get("priority"):
        trace.score("priority", scores["priority"])
