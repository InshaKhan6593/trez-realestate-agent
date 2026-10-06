"""Rebuild everything the agent knows about a buyer, from Postgres, each turn.

The conversation is never held in memory (§5): a reply 3 seconds or 3 days
later goes through exactly this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .listings import availability
from .planner import KnownListing, LeadState

RECENT = 12


@dataclass
class TurnContext:
    state: LeadState
    name: str | None
    language: str | None
    burst: list[dict]                          # this turn's messages (type, text)
    recent: list[dict] = field(default_factory=list)   # earlier messages, oldest first


async def load_context(conn: AsyncConnection, lead_id: int, burst_ids: list[int]) -> TurnContext:
    cur = conn.cursor(row_factory=dict_row)
    now = datetime.now(timezone.utc)
    lead = await (await cur.execute(
        "SELECT name, language, handoff_state FROM leads WHERE id = %s", (lead_id,))).fetchone()
    slots = {r["slot"]: {"value": r["value"], "source": r["source"], "confidence": r["confidence"]}
             for r in await (await cur.execute(
                 "SELECT slot, value, source, confidence FROM lead_slots WHERE lead_id = %s",
                 (lead_id,))).fetchall()}
    asked = {r["slot"]: r["n"] for r in await (await cur.execute(
        "SELECT slot, count(*) AS n FROM open_questions WHERE lead_id = %s GROUP BY slot",
        (lead_id,))).fetchall()}
    open_q = [r["slot"] for r in await (await cur.execute(
        "SELECT slot FROM open_questions WHERE lead_id = %s AND status = 'open' ORDER BY asked_at",
        (lead_id,))).fetchall()]
    listings = [
        KnownListing(r["listing_id"], r["zameen_id"], r["relation"], r["price_shown"], r["status_shown"],
                     r["price_pkr"], availability(r["status"], r["last_verified_at"], now), r["last_at"])
        for r in await (await cur.execute(
            """SELECT ll.listing_id, l.zameen_id, ll.relation, ll.price_shown, ll.status_shown,
                      l.price_pkr, l.status, l.last_verified_at, ll.last_at
               FROM lead_listings ll JOIN listings l ON l.id = ll.listing_id
               WHERE ll.lead_id = %s ORDER BY ll.last_at DESC""", (lead_id,))).fetchall()
    ]
    burst = await (await cur.execute(
        "SELECT id, type, text, at, context_id FROM messages WHERE id = ANY(%s) ORDER BY at, id",
        (burst_ids,))).fetchall()
    first_at = burst[0]["at"] if burst else now
    recent = list(reversed(await (await cur.execute(
        """SELECT direction, type, text, at FROM messages
           WHERE lead_id = %s AND at < %s AND NOT (id = ANY(%s))
           ORDER BY at DESC, id DESC LIMIT %s""", (lead_id, first_at, burst_ids, RECENT))).fetchall()))
    gap = (first_at - recent[-1]["at"]).total_seconds() / 3600 if recent else None
    return TurnContext(
        state=LeadState(lead_id=lead_id, handoff_state=lead["handoff_state"], slots=slots, asked=asked,
                        open_questions=open_q, listings=listings, hours_since_last_message=gap),
        name=lead["name"], language=lead["language"],
        burst=[{"type": m["type"], "text": m["text"]} for m in burst],
        recent=[{"from": "buyer" if m["direction"] == "in" else "us", "text": m["text"]} for m in recent],
    )
