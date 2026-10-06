"""After a reply: store what this turn learned, so the next turn (in 3 seconds
or 3 days) starts from it. Everything is written in one transaction."""

from __future__ import annotations

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from .planner import Plan
from .runner import Facts
from .schemas import Extraction


async def remember(conn: AsyncConnection, lead_id: int, turn_id: int, *, ext: Extraction,
                   plan: Plan, facts: Facts, mentioned: list[int], asked: bool,
                   validation: dict, usage: list[dict]) -> None:
    async with conn.transaction():
        for slot, v in plan.slot_updates.items():
            await conn.execute(
                """INSERT INTO lead_slots (lead_id, slot, value, confidence, source, turn_id, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (lead_id, slot) DO UPDATE SET value = EXCLUDED.value,
                     confidence = EXCLUDED.confidence, source = EXCLUDED.source,
                     turn_id = EXCLUDED.turn_id, updated_at = now()""",
                (lead_id, slot, Jsonb(v["value"]), v["confidence"], v["source"], turn_id))

        await conn.execute(
            """UPDATE open_questions SET status = 'answered'
               WHERE lead_id = %s AND status = 'open' AND slot = ANY(%s)""",
            (lead_id, list(plan.slot_updates)))
        if asked and plan.ask:
            await conn.execute("INSERT INTO open_questions (lead_id, slot, turn_id) VALUES (%s, %s, %s)",
                               (lead_id, plan.ask, turn_id))

        # What we told them about each listing, so a later change is reported, not repeated.
        details = facts.listings
        shown = {r["listing_id"]: r for r in (facts.search or {}).get("results", [])}
        for listing_id in set(details) | (set(shown) & set(mentioned)):
            l = details.get(listing_id) or shown[listing_id]
            relation = "inquired" if listing_id in details else "shown"
            await conn.execute(
                """INSERT INTO lead_listings (lead_id, listing_id, relation, status_shown, price_shown)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (lead_id, listing_id) DO UPDATE SET
                     relation = CASE WHEN lead_listings.relation IN ('liked', 'rejected')
                                     THEN lead_listings.relation ELSE EXCLUDED.relation END,
                     status_shown = EXCLUDED.status_shown, price_shown = EXCLUDED.price_shown,
                     last_at = now()""",
                (lead_id, listing_id, relation, l["availability"], l["price_pkr"]))
        for ref in plan.reject:
            if "zameen_id" in ref:
                await conn.execute(
                    """UPDATE lead_listings SET relation = 'rejected', reason = %s, last_at = now()
                       WHERE lead_id = %s AND listing_id = (SELECT id FROM listings WHERE zameen_id = %s)""",
                    (ext.rejected_reason, lead_id, ref["zameen_id"]))

        await conn.execute(
            """UPDATE leads SET language = %s, fit_score = %s, intent_score = %s, priority = %s
               WHERE id = %s""",
            (ext.language, plan.scores.get("fit"), plan.scores.get("intent"),
             plan.scores.get("priority"), lead_id))
        await conn.execute(
            """UPDATE turns SET extracted = %s, plan = %s, tool_calls = %s, validation = %s, usage = %s
               WHERE id = %s""",
            (Jsonb(ext.model_dump()), Jsonb(_plan_record(plan)), Jsonb(facts.tool_calls),
             Jsonb(validation), Jsonb(usage), turn_id))


def _plan_record(plan: Plan) -> dict:
    return {"ask": plan.ask, "must": plan.must, "handoff": plan.handoff, "search": plan.search,
            "listing_refs": plan.listing_refs, "want_photos": plan.want_photos,
            "want_video": plan.want_video, "scores": plan.scores, "returning": plan.returning,
            "slot_updates": plan.slot_updates}
