"""Conversation rows in Postgres: leads, messages, turns. Async (psycopg 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from .meta import Inbound, Status


async def save_inbound(conn: AsyncConnection, msg: Inbound) -> int | None:
    """Store one inbound message. -> lead_id if it still needs a turn, else None.

    Meta retries webhooks and can deliver the same message twice; the unique
    wa_message_id makes the second insert a no-op. A duplicate of a message
    that is still unanswered returns the lead again, so a retry after a failed
    enqueue heals itself instead of leaving the buyer without a reply.
    """
    lead_id = (await (await conn.execute(
        """INSERT INTO leads (phone, name, last_inbound_at) VALUES (%s, %s, %s)
           ON CONFLICT (phone) DO UPDATE SET
             name = coalesce(EXCLUDED.name, leads.name),
             last_inbound_at = greatest(leads.last_inbound_at, EXCLUDED.last_inbound_at)
           RETURNING id""",
        (msg.phone, msg.name, msg.at),
    )).fetchone())[0]
    await conn.execute(
        """INSERT INTO messages (lead_id, direction, wa_message_id, type, text, media_id,
                                 context_id, payload, at)
           VALUES (%s, 'in', %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (wa_message_id) DO NOTHING""",
        (lead_id, msg.wa_message_id, msg.type, msg.text, msg.media_id,
         msg.context_id, Jsonb(msg.payload), msg.at),
    )
    unanswered = await (await conn.execute(
        "SELECT 1 FROM messages WHERE wa_message_id = %s AND turn_id IS NULL",
        (msg.wa_message_id,),
    )).fetchone()
    return lead_id if unanswered else None


# Delivery statuses only move forward: Meta can deliver them out of order, and
# a late "sent" must not overwrite "read". "failed" always wins.
_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 9}


async def save_status(conn: AsyncConnection, st: Status) -> None:
    rank = _RANK.get(st.status)
    if rank is None:
        return
    await conn.execute(
        """UPDATE messages SET delivery_status = %(status)s,
                               error = coalesce(%(error)s, error)
           WHERE wa_message_id = %(id)s AND direction = 'out'
             AND CASE delivery_status WHEN 'sent' THEN 1 WHEN 'delivered' THEN 2
                                      WHEN 'read' THEN 3 WHEN 'failed' THEN 9 ELSE 0 END
                 < %(rank)s""",
        {"status": st.status, "error": st.error, "id": st.wa_message_id, "rank": rank},
    )


@dataclass(frozen=True)
class Claimed:
    turn_id: int
    phone: str
    agent_has_it: bool         # handoff_state = 'taken': the agent is replying
    messages: list[dict]       # id, type, text, at — oldest first
    first_at: datetime


async def claim_turn(conn: AsyncConnection, lead_id: int) -> Claimed | None:
    """Open a turn and claim every unanswered inbound message for it.

    Caller holds the per-lead lock, so no other worker claims concurrently.
    -> None when there is nothing to answer.
    """
    async with conn.transaction():
        rows = await (await conn.execute(
            """SELECT id, type, text, at FROM messages
               WHERE lead_id = %s AND direction = 'in' AND turn_id IS NULL
               ORDER BY at, id FOR UPDATE""",
            (lead_id,),
        )).fetchall()
        if not rows:
            return None
        phone, handoff_state = (await (await conn.execute(
            "SELECT phone, handoff_state FROM leads WHERE id = %s", (lead_id,)
        )).fetchone())
        turn_id = (await (await conn.execute(
            "INSERT INTO turns (lead_id) VALUES (%s) RETURNING id", (lead_id,)
        )).fetchone())[0]
        await conn.execute(
            "UPDATE messages SET turn_id = %s WHERE id = ANY(%s)", (turn_id, [r[0] for r in rows])
        )
    messages = [{"id": r[0], "type": r[1], "text": r[2], "at": r[3]} for r in rows]
    return Claimed(turn_id, phone, handoff_state == "taken", messages, messages[0]["at"])


async def agent_has_taken(conn: AsyncConnection, lead_id: int) -> bool:
    row = await (await conn.execute(
        "SELECT handoff_state = 'taken' FROM leads WHERE id = %s", (lead_id,)
    )).fetchone()
    return bool(row and row[0])


async def release_turn(conn: AsyncConnection, turn_id: int) -> None:
    """A newer message arrived mid-turn: hand the messages back for the next turn."""
    async with conn.transaction():
        await conn.execute("UPDATE messages SET turn_id = NULL WHERE turn_id = %s", (turn_id,))
        await conn.execute(
            "UPDATE turns SET status = 'superseded', finished_at = now() WHERE id = %s", (turn_id,)
        )


async def set_trace(conn: AsyncConnection, turn_id: int, trace_id: str | None) -> None:
    """Remember the turn's Langfuse trace (None when tracing is off)."""
    if trace_id:
        await conn.execute("UPDATE turns SET langfuse_trace_id = %s WHERE id = %s", (trace_id, turn_id))


async def finish_turn(conn: AsyncConnection, turn_id: int, status: str, *,
                      reply: str | None = None, error: str | None = None,
                      latency_ms: int | None = None) -> None:
    await conn.execute(
        """UPDATE turns SET status = %s, reply = %s, error = %s, latency_ms = %s,
                            finished_at = now() WHERE id = %s""",
        (status, reply, error, latency_ms, turn_id),
    )


async def save_outbound(conn: AsyncConnection, lead_id: int, turn_id: int, text: str,
                        wa_message_id: str | None, status: str, error: str | None,
                        at: datetime, *, type: str = "text") -> None:
    """type 'image' stores the photo's storage path in text, for the record."""
    await conn.execute(
        """INSERT INTO messages (lead_id, direction, wa_message_id, type, text,
                                 delivery_status, error, turn_id, at)
           VALUES (%s, 'out', %s, %s, %s, %s, %s, %s, %s)""",
        (lead_id, wa_message_id, type, text, status, error, turn_id, at),
    )
    if status == "sent":
        await conn.execute("UPDATE leads SET last_outbound_at = %s WHERE id = %s", (at, lead_id))
