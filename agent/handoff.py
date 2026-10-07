"""Human handoff with full context (Phase 1).

States on leads.handoff_state:
  none       the bot is the only one talking
  requested  the agent was alerted; the bot keeps serving, within limits
  taken      the agent is talking; the bot stays silent (app.turn checks this
             before claiming and again right before sending)

Every handoff is a row in `handoffs` with the summary the agent read and the
items the bot could not answer. Reassigning to another agent adds a new row
pointing at the old one, with a fresh summary, so nothing is lost on the way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .listings import availability
from .locations import load_tree

RECENT_MESSAGES = 10


async def build_context(conn: AsyncConnection, lead_id: int) -> dict:
    """Everything an agent needs to continue the conversation without asking
    the buyer to repeat themselves. Built from the database, never from memory."""
    cur = conn.cursor(row_factory=dict_row)
    tree = await load_tree(conn)
    now = datetime.now(timezone.utc)
    lead = await (await cur.execute(
        """SELECT id, phone, name, language, priority, fit_score, intent_score, handoff_state,
                  created_at, last_inbound_at
           FROM leads WHERE id = %s""", (lead_id,))).fetchone()
    if lead is None:
        raise ValueError(f"no lead {lead_id}")
    slots = await (await cur.execute(
        "SELECT slot, value, confidence, source, updated_at FROM lead_slots WHERE lead_id = %s ORDER BY slot",
        (lead_id,))).fetchall()
    listings = await (await cur.execute(
        """SELECT l.id, l.zameen_id, l.title, l.price_pkr, l.status, l.last_verified_at,
                  l.location_id, l.url, ll.relation, ll.reason, ll.price_shown, ll.status_shown, ll.last_at
           FROM lead_listings ll JOIN listings l ON l.id = ll.listing_id
           WHERE ll.lead_id = %s ORDER BY ll.last_at DESC""", (lead_id,))).fetchall()
    waiting = await (await cur.execute(
        "SELECT slot, asked_at FROM open_questions WHERE lead_id = %s AND status = 'open' ORDER BY asked_at",
        (lead_id,))).fetchall()
    messages = await (await cur.execute(
        """SELECT direction, type, text, at FROM messages WHERE lead_id = %s
           ORDER BY at DESC, id DESC LIMIT %s""", (lead_id, RECENT_MESSAGES))).fetchall()
    return {
        "lead": lead,
        "wants": [{"slot": s["slot"], "value": s["value"], "source": s["source"],
                   "confidence": s["confidence"], "updated_at": s["updated_at"],
                   "text": want_text(s["slot"], s["value"], tree)} for s in slots],
        "listings": [{
            "zameen_id": r["zameen_id"], "title": r["title"], "url": r["url"],
            "location": tree.label(r["location_id"]) if r["location_id"] in tree.places else None,
            "relation": r["relation"], "reason": r["reason"],
            "price_now": r["price_pkr"], "price_shown": r["price_shown"],
            "availability_now": availability(r["status"], r["last_verified_at"], now),
            "status_shown": r["status_shown"],
        } for r in listings],
        "waiting_for_buyer": [w["slot"] for w in waiting],
        "recent_messages": list(reversed(messages)),
    }


def _pkr(n: int | None) -> str:
    if n is None:
        return "?"
    trim = lambda x: f"{x:.2f}".rstrip("0").rstrip(".")  # noqa: E731
    if n >= 10**7:
        return f"PKR {trim(n / 10**7)} Cr"
    if n >= 10**5:
        return f"PKR {trim(n / 10**5)} lakh"
    return f"PKR {n:,}"


# What the buyer wants, as the agent would say it. Order = how an agent reads a lead.
_WANT_ORDER = ["ready_to_pay", "purpose", "property_types", "location_id", "location_text", "budget_min",
               "budget_max", "size_min_sqyd", "size_max_sqyd", "bedrooms_min", "timeline", "payment_mode",
               "use", "decision_maker"]
_WORDS = {
    "purpose": {"sale": "buy", "rent": "rent"},
    "timeline": {"under_1_month": "within a month", "1_3_months": "in 1-3 months",
                 "3_6_months": "in 3-6 months", "browsing": "just browsing"},
    "payment_mode": {"cash": "pays cash", "installments": "wants installments", "bank_loan": "bank loan",
                     "selling_first": "selling another property first"},
    "use": {"live": "to live in", "invest": "to invest"},
    "decision_maker": {"self": "decides alone", "with_family": "decides with family",
                       "for_someone_else": "buying for someone else"},
}
# Questions the bot is waiting on, as words.
_ASKED = {"purpose": "buy or rent", "location_id": "area", "property_types": "property type",
          "budget_max": "budget", "bedrooms_min": "bedrooms", "timeline": "timeline",
          "payment_mode": "cash or installments", "decision_maker": "who decides"}


def want_text(slot: str, value, tree) -> str:
    """One thing the buyer wants, in words: 'up to PKR 8.50 Cr', 'Askari 6',
    '5+ bedrooms'. A slot without wording here still shows, as 'slot: value'."""
    if slot in _WORDS:
        return _WORDS[slot].get(str(value), f"{slot.replace('_', ' ')}: {value}")
    if slot == "ready_to_pay":
        return "ready to pay a token"
    if slot == "property_types":
        return "/".join(value) if isinstance(value, list) else str(value)
    if slot == "location_id":
        return tree.label(value).split(", ")[0] if value in tree.places else f"place {value}"
    if slot == "location_text":
        return f"{value} (not one of our places)"
    if slot in ("budget_min", "budget_max") and isinstance(value, int | float):
        return ("from " if slot == "budget_min" else "up to ") + _pkr(int(value))
    if slot in ("size_min_sqyd", "size_max_sqyd") and isinstance(value, int | float):
        return ("from " if slot == "size_min_sqyd" else "up to ") + f"{value:g} sq yd"
    if slot == "bedrooms_min":
        return f"{value}+ bedrooms"
    return f"{slot.replace('_', ' ')}: {value}"


def format_alert(ctx: dict, reason: str, open_items: list[str], also: list[str] | None = None) -> str:
    """The WhatsApp message the agent gets. Plain facts from the database."""
    lead = ctx["lead"]
    head = {"hot": "🔥 HOT", "warm": "WARM"}.get(lead["priority"] or "", "Lead")
    why = reason.replace("_", " ") + (f" (also: {', '.join(r.replace('_', ' ') for r in also)})" if also else "")
    lines = [f"{head}: {lead['name'] or 'Buyer'} (+{lead['phone']}). Reason: {why}."]
    wants = sorted((w for w in ctx["wants"] if w["slot"] != "name"),
                   key=lambda w: _WANT_ORDER.index(w["slot"]) if w["slot"] in _WANT_ORDER else len(_WANT_ORDER))
    if wants:
        lines.append("Wants: " + " · ".join(
            w["text"] + ("" if w["source"] == "stated" else " (guess)") for w in wants))
    for item in ctx["listings"][:5]:
        changed = ""
        if item["price_shown"] and item["price_now"] != item["price_shown"]:
            changed = f" (was {_pkr(item['price_shown'])} when shown)"
        lines.append(f"- {item['relation']}: {item['title']} [{item['zameen_id']}], {_pkr(item['price_now'])}"
                     f"{changed}, {item['availability_now']}"
                     + (f", rejected: {item['reason']}" if item["reason"] else ""))
    if open_items:
        lines.append("Bot could not answer: " + "; ".join(open_items))
    if ctx["waiting_for_buyer"]:
        lines.append("Bot asked, no answer yet: " + ", ".join(
            dict.fromkeys(_ASKED.get(s, s.replace("_", " ")) for s in ctx["waiting_for_buyer"])))
    last_in = next((m["text"] for m in reversed(ctx["recent_messages"])
                    if m["direction"] == "in" and m["text"]), None)
    if last_in:
        lines.append(f'Last message: "{last_in}"')
    lines.append("Reply #take to take over.")
    return "\n".join(lines)


@dataclass(frozen=True)
class HandoffRequest:
    handoff_id: int
    agent: dict | None              # {id, name, phone}; None if no active agent is set up
    alert: str
    new: bool                       # False: an open handoff was updated, not created


async def _pick_agent(cur, lead_id: int) -> dict | None:
    row = await (await cur.execute(
        """SELECT a.id, a.name, a.phone FROM leads l JOIN agents a ON a.id = l.agent_id
           WHERE l.id = %s AND a.active""", (lead_id,))).fetchone()
    if row:
        return row
    return await (await cur.execute(
        "SELECT id, name, phone FROM agents WHERE active ORDER BY id LIMIT 1")).fetchone()


async def request_handoff(conn: AsyncConnection, lead_id: int, reason: str,
                          open_items: list[str] | None = None, also: list[str] | None = None) -> HandoffRequest:
    """Alert an agent. If a handoff is already open, add the new items to it
    instead of opening another (the agent gets one thread, not a flood).
    `also`: the other reasons this turn, shown after the main one."""
    open_items = [i for i in (open_items or []) if i]
    cur = conn.cursor(row_factory=dict_row)
    async with conn.transaction():
        ctx = await build_context(conn, lead_id)
        agent = await _pick_agent(cur, lead_id)
        existing = await (await cur.execute(
            """SELECT id, open_items FROM handoffs
               WHERE lead_id = %s AND released_at IS NULL ORDER BY requested_at DESC LIMIT 1""",
            (lead_id,))).fetchone()
        if existing:
            items = list(dict.fromkeys([*existing["open_items"], *open_items]))
            alert = format_alert(ctx, reason, items, also)
            await cur.execute("UPDATE handoffs SET open_items = %s, summary = %s WHERE id = %s",
                              (Jsonb(items), alert, existing["id"]))
            return HandoffRequest(existing["id"], agent, alert, new=False)
        alert = format_alert(ctx, reason, open_items, also)
        handoff_id = (await (await cur.execute(
            """INSERT INTO handoffs (lead_id, reason, summary, open_items, agent_id)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (lead_id, reason, alert, Jsonb(open_items), agent["id"] if agent else None))).fetchone())["id"]
        await cur.execute(
            """UPDATE leads SET handoff_state = 'requested', agent_id = coalesce(%s, agent_id)
               WHERE id = %s AND handoff_state = 'none'""",
            (agent["id"] if agent else None, lead_id))
        return HandoffRequest(handoff_id, agent, alert, new=True)


async def take(conn: AsyncConnection, lead_id: int, agent_id: int) -> None:
    """The agent is now talking to the buyer: the bot goes silent."""
    async with conn.transaction():
        await conn.execute(
            "UPDATE leads SET handoff_state = 'taken', agent_id = %s WHERE id = %s", (agent_id, lead_id))
        await conn.execute(
            """UPDATE handoffs SET taken_at = coalesce(taken_at, now()), agent_id = %s
               WHERE lead_id = %s AND released_at IS NULL""", (agent_id, lead_id))


async def release(conn: AsyncConnection, lead_id: int) -> None:
    """The agent is done: the bot resumes (with the full history, as always)."""
    async with conn.transaction():
        await conn.execute("UPDATE leads SET handoff_state = 'none' WHERE id = %s", (lead_id,))
        await conn.execute(
            "UPDATE handoffs SET released_at = now() WHERE lead_id = %s AND released_at IS NULL",
            (lead_id,))


async def reassign(conn: AsyncConnection, lead_id: int, to_agent_id: int, note: str | None = None) -> HandoffRequest:
    """Agent to agent: close the current handoff and open one for the new agent
    with a fresh summary (and the open items carried over)."""
    cur = conn.cursor(row_factory=dict_row)
    async with conn.transaction():
        current = await (await cur.execute(
            """SELECT id, reason, open_items FROM handoffs
               WHERE lead_id = %s AND released_at IS NULL ORDER BY requested_at DESC LIMIT 1""",
            (lead_id,))).fetchone()
        items = list(current["open_items"]) if current else []
        if note:
            items.append(f"note from previous agent: {note}")
        if current:
            await cur.execute("UPDATE handoffs SET released_at = now() WHERE id = %s", (current["id"],))
        agent = await (await cur.execute(
            "SELECT id, name, phone FROM agents WHERE id = %s AND active", (to_agent_id,))).fetchone()
        if agent is None:
            raise ValueError(f"agent {to_agent_id} is not an active agent")
        reason = current["reason"] if current else "reassigned"
        alert = format_alert(await build_context(conn, lead_id), reason, items)
        handoff_id = (await (await cur.execute(
            """INSERT INTO handoffs (lead_id, reason, summary, open_items, agent_id, replaces_id)
               VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
            (lead_id, reason, alert, Jsonb(items), agent["id"], current["id"] if current else None),
        )).fetchone())["id"]
        await cur.execute(
            "UPDATE leads SET handoff_state = 'requested', agent_id = %s WHERE id = %s", (agent["id"], lead_id))
        return HandoffRequest(handoff_id, agent, alert, new=True)
