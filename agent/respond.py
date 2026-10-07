"""The responder: the model writes ONE WhatsApp reply from the plan and facts.

It never decides anything; it words what the planner decided, using only the
facts the tools returned. It also declares what it claimed (listings it
called available, listings it mentioned) and what it could not answer, so the
validator can check the claims and the code can hand the rest to an agent.
"""

from __future__ import annotations

import json
import re

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
    "which_listing": "which of the listings in which_listing_do_they_mean they mean (name each briefly)",
    "which_place": "which of the places in FACTS.place.options they mean (name each)",
}

REPLY_IN = {
    "roman_urdu": "Roman Urdu (Urdu written in English letters, like the buyer)",
    "urdu": "Urdu script",
    "english": "English",
    "mixed": "the buyer's own mix of Roman Urdu and English",
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
4. Never negotiate, never hint at a discount, never promise anything. On price talk say only
   the listed price and that our agent will discuss the price with them: never say the price
   is fixed, final, negotiable or that no discount is possible. You may repeat the buyer's own
   offer back to them ("aap ka offer agent tak pohanch jayega"), never accept or judge it.
5. Write the whole reply in REPLY_IN (an English-speaking buyer gets English, greeting included).
6. Order: first every item in MUST, then answer the buyer's questions, then at most ONE
   question: only the one in ASK. If ASK is null, ask no question at all (no "visit karna
   chahenge?", no "aur kuch?").
7. Keep it short and natural for WhatsApp (about 40-90 words; up to 3 listings as short
   lines with title, size, price and area). Plain text; WhatsApp bold (a title between
   asterisks) is allowed; no headings, no sign-off or filler lines ("Trez Enterprises aapki
   madad karega").
8. Only say Trez has or does not have listings somewhere if FACTS say so: SEARCH (with
   prices) or STOCK_PREVIEW (counts only, when the buyer has not said buy or rent yet). With
   neither, do not claim anything about stock. If SEARCH found nothing in the asked place,
   say so honestly first, then say the options are from the wider area named in
   "shown_from_wider_area" and give each one's area and distance (distance_km).
   Set "claims_no_listings" true whenever the reply says we have nothing (somewhere/of a kind).
9. If HANDOFF is set, tell the buyer once that our agent will contact them shortly, and keep
   helping with facts meanwhile. If MEDIA is set, say the photos/video are coming next.
10. If asked whether you are a bot: you are Trez Enterprises' assistant; offer the agent.
11. Never promise an action that is not in this prompt: photos/video only if MEDIA is set;
    "our agent will contact/confirm/discuss" only if HANDOFF is set, or for a question you put
    in "unanswered", or for an "unverified" listing. If they asked for photos and MEDIA is not
    set, say the photos are not available right now. Do not mention photos or videos unless
    they asked or MEDIA is set.
12. Greet (salam / hello) only if GREET is true; otherwise start directly with the answer.
13. Refer to a listing by its title, area or Zameen number (zameen_id), never by listing_id
    (listing_id is internal, only for the JSON fields).
14. "which_listing_do_they_mean" means they referred to a listing but it is not clear which:
    do not answer about any one of them; ask which one they mean (that is the ASK).

Return JSON: {"reply": "...", "listing_ids_mentioned": [...], "says_available": [...],
"unanswered": [...], "claims_no_listings": false, "promises_agent_contact": false,
"promises_media": false, "says_no_photos": false}
using the listing_id numbers from FACTS. "listing_ids_mentioned" in the order the reply names
them. Set "promises_agent_contact" true whenever the reply says our agent will contact them,
call them, confirm or discuss something; "promises_media" true whenever it says photos or a
video are coming; "says_no_photos" true whenever it says photos are not available."""


class ReplyDraft(BaseModel):
    reply: str
    listing_ids_mentioned: list[int] = Field(default_factory=list, description="in the order the reply names them")
    says_available: list[int] = Field(default_factory=list,
                                      description="listing_ids the reply calls available")
    unanswered: list[str] = Field(default_factory=list,
                                  description="buyer questions the FACTS could not answer")
    claims_no_listings: bool = Field(False, description="the reply says Trez has nothing of a kind/somewhere")
    promises_agent_contact: bool = Field(False, description="the reply says our agent will contact/confirm")
    promises_media: bool = Field(False, description="the reply says photos/a video are coming")
    says_no_photos: bool = Field(False, description="the reply says photos are not available")


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
        out["installment_plan"] = {_words(k): (pkr(v) if v > 999 else v) for k, v in l["payment_plan"].items()}
    return out


def _words(key: str) -> str:
    """Zameen's field names as words: 'ballotingFee' -> 'balloting fee'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()


def build_prompt(plan: Plan, facts: Facts, burst: list[dict], recent: list[dict],
                 language: str, name: str | None) -> list[dict]:
    payload = {
        "BUYER_NAME": name,
        "REPLY_IN": REPLY_IN.get(language, "the buyer's own language"),
        "EARLIER_CONVERSATION": recent[-8:],
        "BUYER_MESSAGES_NOW": [m["text"] or f"[{m['type']}]" for m in burst],
        "MUST": [_must_text(m, facts) for m in plan.must],
        "FACTS": {
            "listings": [_listing_fact(l) for l in facts.listings.values()],
            "search": None if facts.search is None else {
                "stage": facts.search["stage"],
                "asked_location": facts.search.get("asked_location"),
                "nothing_in_asked_location": facts.search.get("nothing_in_asked_location", False),
                # Nothing in the asked place: these come from this wider area instead.
                "shown_from_wider_area": facts.search.get("widened_to"),
                "results": [_listing_fact(r) for r in facts.search["results"]],
            },
            "place": facts.location,
            "stock_preview": facts.stock_preview,
            "which_listing_do_they_mean": [
                {"options": [_listing_fact(c) for c in u["candidates"]]} for u in facts.unresolved_refs],
        },
        "GREET": plan.greet,
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
