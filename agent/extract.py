"""The extractor: the model reads the buyer's messages and reports, as JSON,
what they asked and what they told us. It decides nothing.

It is grounded in our data: it picks places from the real place list (by id)
and listings from the ones this buyer already discussed (by Zameen id), so it
never has to guess a spelling or invent an id.
"""

from __future__ import annotations

import json

from .context import TurnContext
from .llm import LLM, Usage
from .schemas import Extraction

SYSTEM = """You read WhatsApp messages from people asking Trez Enterprises (a Karachi real estate
agency) about property. Buyers write in Roman Urdu, Urdu or English, often mixed.
Report ONLY what the messages say, as JSON matching the schema. Decide nothing, answer nothing.

Rules:
- intents: every distinct thing they want in BUYER_MESSAGES_NOW, in order.
- A Zameen link or listing number: put it in listing.zameen_id.
- "this one / the DHA one / the first one / the cheaper one": if you can tell which of
  LISTINGS_DISCUSSED it is, use its zameen_id; otherwise describe it in listing.from_history.
- slot_updates: facts about what they want. source "stated" if they said it, "inferred" if implied
  (confidence below 0.7 for inferred).
  * purpose: "sale" if they want to buy, "rent" if they want to rent. Never guess it.
  * location_id: the id of the matching place in PLACES (choose the most specific that fits;
    "Askari V" = "Askari 5", "Malir Cantt" = "Malir Cantonment"). If no place fits, use location_text.
  * budget_min/budget_max: whole PKR. "4 cr tak" -> budget_max 40000000; "80 lakh" -> 8000000;
    a bare "4.5" in a house-buying chat means crore (inferred).
  * size in square yards: 1 marla = 25 sq yd, 1 kanal = 500 sq yd, sq ft / 9 = sq yd.
  * property_types: list from house, flat, plot, commercial, portion.
  * timeline: under_1_month | 1_3_months | 3_6_months | browsing.
  * payment_mode: cash | installments | bank_loan | selling_first.
- A short answer like "3 tak" or "haan" answers the matching OPEN_QUESTIONS item.
- signals: only if clearly present (visit_request: wants to see it; token_or_bayana: ready to pay
  token; cash_ready; urgent; dealer: an agent/dealer fishing for stock; frustrated; repeat_question).
- rejected: listings they said no to, with rejected_reason in their words."""


def build_prompt(ctx: TurnContext, places: list[dict], discussed: list[dict]) -> list[dict]:
    payload = {
        "PLACES": places,
        "LISTINGS_DISCUSSED": discussed,
        "OPEN_QUESTIONS": ctx.state.open_questions,
        "WHAT_WE_KNOW": {k: v["value"] for k, v in ctx.state.slots.items()},
        "EARLIER_CONVERSATION": ctx.recent[-8:],
        "BUYER_MESSAGES_NOW": [m["text"] or f"[{m['type']}]" for m in ctx.burst],
    }
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]


async def extract(llm: LLM, usage: Usage, messages: list[dict]) -> Extraction:
    return await llm.structured("extractor", messages, Extraction, usage)
