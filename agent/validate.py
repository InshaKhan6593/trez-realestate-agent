"""The validator: code checks the model's reply before anything is sent (§9).

A confident, well-written wrong reply is the failure that matters most, so
facts are checked, not style:
- every money amount in the reply is one the tools returned (or the buyer's own budget)
- only listings the tools call "available" are called available
- every MUST item's listing is mentioned
- "we have nothing" only when a search or the stock preview found nothing
- "our agent will..." only when the agent is involved; photos "coming" only when
  they are being sent, and only as many as are sent; "no photos" only when the
  listing has none
- no visit arranged or confirmed (the agent does that); none of the prompt's own words
- at most one question
Fail -> one regeneration with the problems listed -> fail again -> template.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .money import amounts_in, pkr, same_amount
from .planner import Plan
from .respond import ReplyDraft
from .runner import Facts

INTERNAL_FIELDS = ("listing_ids_mentioned", "says_available", "promises_agent_contact", '"reply"')


def prompt_words(messages: list[dict]) -> set[str]:
    """The prompt's own vocabulary, read from the prompt the responder got: its
    section names (FACTS, ASK, ...) and every snake_case key (stock_preview,
    listing_id, ...). None of these belongs in a WhatsApp message, and reading
    them from the prompt keeps this check right when the prompt changes."""
    import json
    words: set[str] = set()

    def walk(node, top=False):
        if isinstance(node, dict):
            for k, v in node.items():
                if (top and k.isupper()) or "_" in k:
                    words.add(k)
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    for m in messages:
        if m.get("role") == "user":
            try:
                walk(json.loads(m["content"]), top=True)
            except (ValueError, TypeError):
                pass
    return words
# The word the buyer reads when the agent is brought in (Roman Urdu/English, Urdu script).
AGENT_WORDS = ("agent", "ایجنٹ")


def check(draft: ReplyDraft, plan: Plan, facts: Facts, buyer_amounts: set[int],
          internal_words: set[str] = frozenset(), *, handoff_open: bool = False) -> list[str]:
    problems: list[str] = []
    # The model's own JSON pasted into the message (live run: 'Filename: data.json {"reply": ...').
    if any(f in draft.reply for f in INTERNAL_FIELDS):
        problems.append("the reply contains the JSON answer itself; write only the WhatsApp message in \"reply\"")
    # The prompt's own words in the message (live run: "FACTS mein kuch mention nahi hai").
    leaked = sorted(w for w in internal_words if re.search(rf"(?<![A-Za-z_]){re.escape(w)}(?![A-Za-z_])", draft.reply))
    if leaked:
        problems.append(f"the reply uses our internal words {leaked}; talk about the listing, never about our data")
    # Every feature the reply says a listing has must be written in a listing's facts
    # (live run: "parking bhi hai" for a house whose listing says nothing about parking).
    facts_text = json.dumps([*facts.listings.values(), *(facts.search or {}).get("results", []), *facts.candidates],
                            ensure_ascii=False, default=str).lower()
    unsupported = [f for f in draft.features_said if f.strip() and f.strip().lower() not in facts_text]
    if unsupported:
        problems.append(f"the reply says a listing has {unsupported}, which its facts do not say; claim only "
                        "features written in FACTS")
    # Visits are arranged by the agent only (live run: "Aap kal dekhne aa sakte hain").
    if draft.promises_visit:
        problems.append("the reply arranges or confirms a visit; only our agent arranges visits, say they will")
    # A MUST item tells the model to state these (e.g. the old and the new price).
    must_prices = {m[k] for m in plan.must for k in ("was", "now") if isinstance(m.get(k), int)}
    allowed = facts.allowed_prices | buyer_amounts | must_prices
    for amount in amounts_in(draft.reply):
        if not any(same_amount(amount, a) for a in allowed):
            problems.append(f"the reply states {pkr(amount)}, which is not in the facts")

    availability = facts.availability()
    for listing_id in draft.says_available:
        if availability.get(listing_id) != "available":
            problems.append(f"listing {listing_id} is called available but is "
                            f"{availability.get(listing_id, 'not in the facts')}")

    for must in plan.must:
        if "listing_id" in must and must["listing_id"] not in draft.listing_ids_mentioned:
            problems.append(f"a required update about listing {must['listing_id']} is missing")

    found_nothing = facts.search is not None and not facts.search.get("results")
    if found_nothing and not draft.claims_no_listings:
        # Live run: "Yeh list kar raha hoon" when the search had found nothing.
        problems.append("the search found nothing that matches; say so plainly (and set claims_no_listings)")
    if draft.claims_no_listings:
        searched_nothing = facts.search is not None and facts.search.get("nothing_in_asked_location", facts.search["total"] == 0)
        preview_nothing = facts.stock_preview is not None and not (
            facts.stock_preview["for_sale"] or facts.stock_preview["for_rent"])
        place_unknown = facts.location is not None and facts.location["status"] == "none"
        if not (searched_nothing or preview_nothing or place_unknown):
            problems.append("the reply says we have no listings, but the facts do not say that")

    # "Our agent will contact/confirm" is true only if the agent will be involved:
    # a handoff, a question the facts cannot answer (handed off), or an
    # unverified listing (the agent must confirm it).
    agent_involved = handoff_open or bool(plan.handoff) or bool(draft.unanswered) or any(
        l["availability"] == "unverified" for l in facts.listings.values())
    if draft.promises_agent_contact and not agent_involved:
        problems.append("the reply says our agent will contact them, but no handoff was made")
    elif not agent_involved and any(w in draft.reply.lower() for w in AGENT_WORDS):
        # Said but not declared (live run: "Agent aapko guide kar dega" with no handoff).
        problems.append("the reply brings in our agent, but no handoff was made; do not mention the agent")

    # Photos and videos: only what is actually being sent, and never "none" when we have them.
    sending = facts.media is not None and (facts.media["photo_count"] or facts.media["video_urls"])
    if draft.promises_media and not sending:
        problems.append("the reply says photos or a video are coming, but none are being sent")
    sending_count = (facts.media or {}).get("photos_sending", 0)
    if draft.photos_coming_said is not None and draft.photos_coming_said != sending_count:
        # Live run: "Photos 11 hain, main bhej raha hoon" when 4 go out.
        problems.append(f"the reply says {draft.photos_coming_said} photos are being sent, but "
                        f"{sending_count} are; say {sending_count} (the listing's total may be added)")
    with_photos = [l for l in facts.listings.values() if l.get("photo_count")]
    if draft.says_no_photos and with_photos:
        problems.append(f"the reply says photos are not available, but listing "
                        f"{with_photos[0]['listing_id']} has {with_photos[0]['photo_count']} photos")

    questions = draft.reply.count("?") + draft.reply.count("؟")
    if questions > 1:
        problems.append(f"the reply asks {questions} questions; at most one is allowed")
    elif questions and plan.ask is None:
        # Live run: "Kya aap isay visit karna chahenge?" when nothing was to be asked.
        problems.append("the reply asks a question, but no question was planned; ask nothing")
    if not draft.reply.strip():
        problems.append("the reply is empty")
    return problems


@dataclass
class Template:
    text: str
    listing_ids: list[int]          # listings it names, in order (remembered like a model reply's)
    needs_agent: bool               # it had nothing safe to say: the agent must answer


def template(plan: Plan, facts: Facts, language: str) -> Template:
    """A safe reply built from facts alone, when the model fails twice. Never
    empty: with no facts to give, it says the agent will reply, and the caller
    hands the message to the agent so that is true."""
    urdu = language != "english"
    lines: list[str] = []
    for must in plan.must:
        l = facts.listings.get(must.get("listing_id"), {})
        title = l.get("title", "")
        if must["kind"] == "listing_gone":
            lines.append(f"{title}: ab {must['status']} ho chuki hai." if urdu else f"{title}: now {must['status']}.")
        elif must["kind"] == "price_changed":
            lines.append(f"{title}: nayi qeemat {pkr(must['now'])}." if urdu else f"{title}: new price {pkr(must['now'])}.")
    if facts.location is not None and facts.location["status"] == "none":
        place = facts.location["text"]
        lines.append(f"{place} mein abhi hamare paas listings nahi hain." if urdu
                     else f"We have no listings in {place} right now.")
    elif facts.location is not None and facts.location["status"] == "ask":
        names = " / ".join(o["name"] for o in facts.location["options"])
        lines.append(f"Aap kaun si jagah ki baat kar rahe hain: {names}?" if urdu
                     else f"Which one do you mean: {names}?")
    shown = (list(facts.listings.values())[:2] or (facts.search or {}).get("results", [])[:3]
             or facts.candidates[:3])
    for l in shown:
        size = f", {l['size_sqyd']:g} sq yd" if l.get("size_sqyd") else ""
        place = f", {l['location'].split(',')[0]}" if l.get("location") else ""
        lines.append(f"• {l['title']}{size}, {pkr(l['price_pkr'])}{place}")
    if not facts.listings and not facts.search and facts.candidates:
        lines.append("Aap in mein se kaun si listing ke baare mein pooch rahe hain?" if urdu
                     else "Which of these listings do you mean?")
    needs_agent = not lines
    if plan.handoff or needs_agent:
        lines.append("Hamare agent jald aap se rabta karenge." if urdu else "Our agent will contact you shortly.")
    return Template("\n".join(lines), [l["listing_id"] for l in shown], needs_agent)
