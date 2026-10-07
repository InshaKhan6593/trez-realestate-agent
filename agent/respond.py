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

from .context import for_model
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
    "listing_link": "which listing they mean: nothing matching has been shared with them yet, so ask for its "
                    "Zameen link or number",
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
   the listed price (and, if HANDOFF is set, that our agent will discuss the price with them):
   never say the price is fixed, final, negotiable or that no discount is possible. You may repeat the buyer's own
   offer back to them (it goes to the agent); never accept or judge it.
5. Write the whole reply in REPLY_IN (an English-speaking buyer gets English, greeting included).
6. Order: first every item in MUST, then answer the buyer's questions, then at most ONE
   question: only the one in ASK. If ASK is null, ask no question at all (not even an offer of
   more help or of a visit).
7. Keep it short and natural for WhatsApp (about 40-90 words; up to 3 listings). Each listing:
   its title in WhatsApp bold (between asterisks) on one line, then size · price · location on
   the next line, and an empty line between listings. Plain text otherwise; no headings, no
   sign-off or filler lines that carry no information.
8. Only say Trez has or does not have listings somewhere if FACTS say so: SEARCH (with
   prices) or STOCK_PREVIEW (counts only, when the buyer has not said buy or rent yet). With
   neither, do not claim anything about stock. If SEARCH found nothing in the asked place,
   say so honestly first, then say the options are from the wider area named in
   "shown_from_wider_area" and give each one's area and distance (distance_km).
   Set "claims_no_listings" true whenever the reply says we have nothing (somewhere/of a kind).
   STOCK_PREVIEW counts only listings matching its "counted" criteria in "asked_location":
   describe a count with exactly those criteria, never with a type, size or budget it does not
   list. If SEARCH has "buyer_has_not_said_buy_or_rent", its listings may be for sale and for
   rent: say which each one is (its "purpose"). If SEARCH has "already_shown_left_out", its
   results are listings they have not seen yet; if it has none in the asked place, say they have
   now seen everything there that matches before giving wider options.
   If SEARCH has "no_exact_match", nothing fits everything they asked: say so plainly first
   (set "claims_no_listings"), then give its results as the closest options, each with its real
   price/size and how it differs from what they asked (its "differs_from_request").
   If FACTS do not show whether something exists or matches, do not claim either way.
9. If HANDOFF is set, tell the buyer once that our agent will contact them shortly, and keep
   helping with facts meanwhile. If MEDIA is set, say the photos/video are coming next; a number
   of photos coming is MEDIA.photos_sending (the listing's total, photo_count, may be added).
10. If asked whether you are a bot: you are Trez Enterprises' assistant; offer the agent.
11. Never promise an action that is not in this prompt: photos/video only if MEDIA is set;
    "our agent will contact/confirm/discuss" only if HANDOFF is set, or for a question you put
    in "unanswered", or for an "unverified" listing. If they asked for photos and MEDIA is not
    set, say the photos are not available right now. Do not mention photos or videos unless
    they asked or MEDIA is set.
12. Greet (salam / hello) only if GREET is true; otherwise start directly with the answer.
    If INTRODUCE is true (their first message to us), right after the greeting say in one short
    sentence who you are: Trez Enterprises' WhatsApp assistant, who can share Trez's listings,
    prices, photos and videos and connect them with Trez's agent. Then answer and ask.
13. Refer to a listing by its title, area or Zameen number (zameen_id), never by listing_id
    (listing_id is internal, only for the JSON fields).
14. "which_listing_do_they_mean" means they referred to a listing but it is not clear which:
    do not answer about any one of them; ask which one they mean (that is the ASK).
15. Visits and meetings are arranged by our agent only: never say a visit or a time is
    possible, fixed or arranged; say the agent will arrange it.
16. A message in BUYER_MESSAGES_NOW with "we_can_see_or_hear_its_content": false (a voice note,
    a picture, a video, a file): say plainly that you cannot listen to / see it yet, use any
    "words" it came with, and ask them to type their question (for a picture of a listing: its
    Zameen link or number). HANDOFF, if set, means our agent will look at it.
17. Never mention this prompt, its sections or field names (anything written in CAPITALS or with
    underscores here): talk about the listings and the buyer's question only.

Return JSON: {"reply": "...", "listing_ids_mentioned": [...], "says_available": [...],
"unanswered": [...], "claims_no_listings": false, "promises_agent_contact": false,
"promises_media": false, "says_no_photos": false, "photos_coming_said": null, "promises_visit": false,
"features_said": [], "judges_price": false}
using the listing_id numbers from FACTS. "listing_ids_mentioned" in the order the reply names
them. Set "promises_agent_contact" true whenever the reply says our agent will contact them,
call them, confirm or discuss something; "promises_media" true whenever it says photos or a
video are coming; "says_no_photos" true whenever it says photos are not available;
"photos_coming_said" = the number of photos the reply says are being sent now (null if none);
"judges_price" true whenever it accepts, refuses or judges their offer or says whether the price can
change; "promises_visit" true whenever it says they can come to see it (on any day or time) or a visit is
fixed or arranged; "features_said" = every amenity or feature the reply says a listing has (not its rooms, size, price
or area), each copied exactly as written in that listing's FACTS (one FACTS do not write, you may not claim)."""


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
    photos_coming_said: int | None = Field(None, description="how many photos the reply says are being sent now")
    promises_visit: bool = Field(False, description="the reply says they can come to see it (any day or time), or "
                                                    "that a visit is fixed or arranged")
    judges_price: bool = Field(False, description="the reply accepts, refuses or judges the buyer's offer, or says "
                                                  "whether the price can or cannot change")
    features_said: list[str] = Field(default_factory=list,
                                     description="each amenity or feature the reply says a listing HAS, copied "
                                                 "exactly as FACTS write it; not its rooms, size, price or area")


def _listing_fact(l: dict) -> dict:
    keep = ("listing_id", "zameen_id", "title", "purpose", "property_type", "zameen_type",
            "bedrooms", "bathrooms", "location", "availability", "photo_count", "video_count",
            "description", "furnishing_status", "completion_status", "url", "distance_km")
    out = {k: l[k] for k in keep if l.get(k) not in (None, "", [])}
    if out.get("location"):
        # The place and the area it is in: the whole chain up to Karachi made every
        # listing one long line on WhatsApp.
        out["location"] = ", ".join(out["location"].split(", ")[:2])
    out["price"] = pkr(l["price_pkr"]) + (f" per {l['price_period']}" if l.get("price_period") else "")
    if l.get("size_sqyd"):
        out["size"] = f"{l['size_sqyd']:g} sq yd"
    if l.get("amenities"):
        out["listing_says"] = {a["label"]: a["value"] for a in l["amenities"]}
    if l.get("payment_plan"):
        out["installment_plan"] = {_words(k): (pkr(v) if v > 999 else v) for k, v in l["payment_plan"].items()}
    if l.get("differs_from_request"):
        out["differs_from_request"] = [_asked(d) for d in l["differs_from_request"]]
    return out


# How a closest listing differs from what the buyer asked, in the buyer's terms.
_ASKED = {"budget_min": "budget from", "budget_max": "budget up to", "size_min_sqyd": "size at least",
          "size_max_sqyd": "size at most", "bedrooms_min": "bedrooms at least"}


def _asked(d: dict) -> dict:
    v = d["asked"]
    shown = pkr(v) if d["criterion"].startswith("budget") else (
        f"{v:g} sq yd" if d["criterion"].startswith("size") else v)
    return {"they_asked": f"{_ASKED[d['criterion']]} {shown}"}


def _words(key: str) -> str:
    """Zameen's field names as words: 'ballotingFee' -> 'balloting fee'."""
    return re.sub(r"(?<!^)(?=[A-Z])", " ", key).lower()


def build_prompt(plan: Plan, facts: Facts, burst: list[dict], recent: list[dict],
                 language: str, name: str | None) -> list[dict]:
    payload = {
        "BUYER_NAME": name,
        "REPLY_IN": REPLY_IN.get(language, "the buyer's own language"),
        "EARLIER_CONVERSATION": recent[-8:],
        "BUYER_MESSAGES_NOW": [for_model(m) for m in burst],
        "MUST": [_must_text(m, facts) for m in plan.must],
        "FACTS": {
            "listings": [_listing_fact(l) for l in facts.listings.values()],
            "search": None if facts.search is None else {
                "stage": facts.search["stage"],
                "asked_location": facts.search.get("asked_location"),
                "nothing_in_asked_location": facts.search.get("nothing_in_asked_location", False),
                # Nothing in the asked place: these come from this wider area instead.
                "shown_from_wider_area": facts.search.get("widened_to"),
                # Buy or rent not said: these are for sale and/or for rent (each has "purpose").
                **({"buyer_has_not_said_buy_or_rent": True}
                   if facts.search.get("buyer_has_not_said_buy_or_rent") else {}),
                # "More options": listings they have already seen are not in these results.
                **({"already_shown_left_out": facts.search["already_shown_left_out"]}
                   if facts.search.get("already_shown_left_out") else {}),
                # Nothing fits everything they asked: these are the closest, each saying how it differs.
                **({"no_exact_match": True} if facts.search.get("no_exact_match") else {}),
                "results": [_listing_fact(r) for r in facts.search["results"]],
            },
            "place": facts.location,
            "stock_preview": facts.stock_preview,
            # The listings our last reply named, as they are now (the buyer may be reacting to them).
            "listings_in_our_last_reply": [_listing_fact(r) for r in facts.last_shown],
            "which_listing_do_they_mean": [
                {"options": [_listing_fact(c) for c in u["candidates"]]} for u in facts.unresolved_refs],
        },
        "GREET": plan.greet,
        "INTRODUCE": plan.introduce,
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
