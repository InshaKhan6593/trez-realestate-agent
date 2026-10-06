"""The responder: the model writes ONE WhatsApp reply from the plan and facts.

It never decides anything; it words what the planner decided, using only the
facts the tools returned. It also declares what it claimed (listings it
called available, listings it mentioned) and what it could not answer, so the
validator can check the claims and the code can hand the rest to an agent.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from .llm import LLM, Usage
from .money import pkr
from .planner import Plan
from .runner import Facts

ASK_HINTS = {
    "purpose": "whether they want to buy or rent",
    "location_id": "which area they are interested in",
    "property_types": "whether they want a house, flat or plot",
    "budget_max": "their budget (in crore/lakh)",
    "bedrooms_min": "how many bedrooms they need",
    "timeline": "when they are looking to buy or move",
    "payment_mode": "whether they will pay cash or in installments",
    "decision_maker": "whether anyone else (family) will decide with them",
}

SYSTEM = """You are the WhatsApp assistant of Trez Enterprises, a real estate agency in Karachi.
Buyers message about Trez's own listings on Zameen.com. Write ONE short WhatsApp reply.

Hard rules:
1. Use ONLY the FACTS given. Never invent or estimate a price, size, room count, amenity,
   availability, location detail or installment amount. Copy prices exactly as written in FACTS.
2. A listing's "availability": "available" may be called available; "unverified" means you
   must NOT say it is available: say the agent will confirm availability; "sold"/"rented" say so.
3. If the buyer asks something the FACTS do not answer (e.g. a feature the listing does not
   mention, fees, documents, discounts), say the listing does not mention it and the agent
   will confirm, and put that question in "unanswered". "Not mentioned" never means "no".
4. Never negotiate, never hint at a discount, never promise anything.
5. Reply in the buyer's language and style: roman_urdu -> Roman Urdu, urdu -> Urdu script,
   english -> English, mixed -> follow the buyer.
6. Order: first every item in MUST, then answer the buyer's questions, then at most ONE
   question (only the one in ASK, if any). Never ask more than one question.
7. Keep it short and natural for WhatsApp (about 40-90 words; up to 3 listings as short
   lines with title, size, price and area). Plain text, *bold* allowed, no headings.
8. If SEARCH says nothing was found in the asked place, say so honestly first, then offer
   what was found nearby with its area and distance.
9. If HANDOFF is set, tell the buyer once that our agent will contact them shortly, and keep
   helping with facts meanwhile. If MEDIA is set, say the photos/video are coming next.
10. If asked whether you are a bot: you are Trez Enterprises' assistant; offer the agent.

Return JSON: {"reply": "...", "listing_ids_mentioned": [...], "says_available": [...],
"unanswered": [...]} using the listing_id numbers from FACTS."""


class ReplyDraft(BaseModel):
    reply: str
    listing_ids_mentioned: list[int] = Field(default_factory=list)
    says_available: list[int] = Field(default_factory=list,
                                      description="listing_ids the reply calls available")
    unanswered: list[str] = Field(default_factory=list,
                                  description="buyer questions the FACTS could not answer")


def _listing_fact(l: dict) -> dict:
    keep = ("listing_id", "zameen_id", "title", "purpose", "property_type", "zameen_type",
            "bedrooms", "bathrooms", "location", "availability", "photo_count", "video_count",
            "description", "furnishing_status", "completion_status", "url", "distance_km")
    out = {k: l[k] for k in keep if l.get(k) not in (None, "", [])}
    out["price"] = pkr(l["price_pkr"]) + (f" per {l['price_period']}" if l.get("price_period") else "")
    if l.get("size_sqyd"):
        out["size"] = f"{l['size_sqyd']:g} sq yd"
    if l.get("amenities"):
        out["listing_says"] = {a["label"]: a["value"] for a in l["amenities"]}
    if l.get("payment_plan"):
        out["installment_plan"] = {k: (pkr(v) if v > 999 else v) for k, v in l["payment_plan"].items()}
    return out


def build_prompt(plan: Plan, facts: Facts, burst: list[dict], recent: list[dict],
                 language: str, name: str | None) -> list[dict]:
    payload = {
        "BUYER_NAME": name,
        "LANGUAGE": language,
        "EARLIER_CONVERSATION": recent[-8:],
        "BUYER_MESSAGES_NOW": [m["text"] or f"[{m['type']}]" for m in burst],
        "MUST": [_must_text(m, facts) for m in plan.must],
        "FACTS": {
            "listings": [_listing_fact(l) for l in facts.listings.values()],
            "search": None if facts.search is None else {
                "stage": facts.search["stage"],
                "asked_location": facts.search.get("asked_location"),
                "nothing_in_asked_location": facts.search.get("nothing_in_asked_location", False),
                "results": [_listing_fact(r) for r in facts.search["results"]],
                "closest_elsewhere": _listing_fact(facts.search["closest_elsewhere"])
                if facts.search.get("closest_elsewhere") else None,
            },
            "place": facts.location,
            "which_listing_do_they_mean": [
                {"options": [_listing_fact(c) for c in u["candidates"]]} for u in facts.unresolved_refs],
        },
        "ASK": ASK_HINTS.get(plan.ask) if plan.ask else None,
        "HANDOFF": plan.handoff["reason"] if plan.handoff else None,
        "MEDIA": facts.media,
    }
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]


def _must_text(m: dict, facts: Facts) -> str:
    l = facts.listings.get(m.get("listing_id"), {})
    title = l.get("title", f"listing {m.get('listing_id')}")
    if m["kind"] == "listing_gone":
        return f"Tell them '{title}' (listing_id {m['listing_id']}) is now {m['status']}; do not offer it."
    if m["kind"] == "price_changed":
        return (f"Tell them the price of '{title}' (listing_id {m['listing_id']}) changed from "
                f"{pkr(m['was'])} to {pkr(m['now'])}.")
    if m["kind"] == "listing_unverified":
        return f"Tell them the agent will confirm whether '{title}' (listing_id {m['listing_id']}) is still available."
    return json.dumps(m)


async def respond(llm: LLM, usage: Usage, messages: list[dict]) -> ReplyDraft:
    return await llm.structured("responder", messages, ReplyDraft, usage)
