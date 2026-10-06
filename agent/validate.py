"""The validator: code checks the model's reply before anything is sent (§9).

A confident, well-written wrong reply is the failure that matters most, so
facts are checked, not style:
- every money amount in the reply is one the tools returned (or the buyer's own budget)
- only listings the tools call "available" are called available
- every MUST item's listing is mentioned
- at most one question
Fail -> one regeneration with the problems listed -> fail again -> template.
"""

from __future__ import annotations

from .money import amounts_in, pkr, same_amount
from .planner import Plan
from .respond import ReplyDraft
from .runner import Facts


def check(draft: ReplyDraft, plan: Plan, facts: Facts, buyer_amounts: set[int]) -> list[str]:
    problems: list[str] = []
    allowed = facts.allowed_prices | buyer_amounts
    for amount in amounts_in(draft.reply):
        if not any(same_amount(amount, a) for a in allowed):
            problems.append(f"the reply states {pkr(amount)}, which is not in the facts")

    availability = {l["listing_id"]: l["availability"] for l in facts.listings.values()}
    for r in (facts.search or {}).get("results", []):
        availability[r["listing_id"]] = r["availability"]
    for listing_id in draft.says_available:
        if availability.get(listing_id) != "available":
            problems.append(f"listing {listing_id} is called available but is "
                            f"{availability.get(listing_id, 'not in the facts')}")

    for must in plan.must:
        if "listing_id" in must and must["listing_id"] not in draft.listing_ids_mentioned:
            problems.append(f"a required update about listing {must['listing_id']} is missing")

    questions = draft.reply.count("?") + draft.reply.count("؟")
    if questions > 1:
        problems.append(f"the reply asks {questions} questions; at most one is allowed")
    if not draft.reply.strip():
        problems.append("the reply is empty")
    return problems


def template(plan: Plan, facts: Facts, language: str) -> str:
    """A safe reply built from facts alone, when the model fails twice."""
    urdu = language != "english"
    lines: list[str] = []
    for must in plan.must:
        l = facts.listings.get(must.get("listing_id"), {})
        title = l.get("title", "")
        if must["kind"] == "listing_gone":
            lines.append(f"{title}: ab {must['status']} ho chuki hai." if urdu else f"{title}: now {must['status']}.")
        elif must["kind"] == "price_changed":
            lines.append(f"{title}: nayi qeemat {pkr(must['now'])}." if urdu else f"{title}: new price {pkr(must['now'])}.")
    shown = list(facts.listings.values())[:2] or (facts.search or {}).get("results", [])[:3]
    for l in shown:
        size = f", {l['size_sqyd']:g} sq yd" if l.get("size_sqyd") else ""
        lines.append(f"• {l['title']}{size}, {pkr(l['price_pkr'])}, {l['location'].split(',')[0]}")
    lines.append("Hamare agent jald aap se rabta karenge." if urdu else "Our agent will contact you shortly.")
    return "\n".join(lines)
