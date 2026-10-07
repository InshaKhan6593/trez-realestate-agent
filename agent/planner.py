"""The planner: plain code, no model. Pure: no I/O, so every rule is testable.

Input: what we know about the buyer (LeadState) + what the extractor read in
this burst (Extraction). Output: a Plan the tool runner and responder follow.

Rules (ARCHITECTURE.md §7, §9 and the Phase 1 scope):
- Stated beats inferred; newer beats older.
- Purpose (buy/rent) is never assumed.
- Answer first; then at most ONE question, the most useful missing fact,
  never one already known, never one asked twice already.
- Negotiation, legal/documents, a request for a person, a seller, a visit
  request, frustration -> handoff. The bot keeps serving facts until the
  agent takes over; it never negotiates or promises.
- Scores are computed here from facts; the model only reports signals. A buyer
  ready to pay a token is hot, and stays hot (remembered as `ready_to_pay`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .schemas import Extraction

# Order in which missing facts are worth asking about.
QUESTION_ORDER = ["purpose", "location_id", "property_types", "budget_max",
                  "bedrooms_min", "timeline", "payment_mode", "decision_maker"]
MAX_ASKS = 2
KNOWN_AT = 0.6                     # confidence at which a slot counts as known
RETURNING_AFTER_HOURS = 6          # §8: a new episode
HANDOFF_INTENTS = {"negotiation": "negotiation", "ask_human": "asked_for_human",
                   "legal_or_documents": "legal_or_documents", "seller": "seller"}
HANDOFF_SIGNALS = {"visit_request": "visit_request", "token_or_bayana": "ready_to_pay",
                   "frustrated": "frustrated"}
# The reason the agent reads first: money on the table, then the deal, then the rest.
HANDOFF_ORDER = ["ready_to_pay", "negotiation", "visit_request", "legal_or_documents", "seller",
                 "frustrated", "asked_for_human", "hot_lead", "not_in_data"]
# A clarifying question whose answer fills a slot: stored as that slot's open
# question, so the buyer's short answer ("Emaar Panorama") is read as the answer.
ASK_FILLS = {"which_place": "location_id"}
# Slots with fixed choices: anything else the model returns is not stored.
SLOT_CHOICES = {
    "purpose": {"sale", "rent"},
    "timeline": {"under_1_month", "1_3_months", "3_6_months", "browsing"},
    "payment_mode": {"cash", "installments", "bank_loan", "selling_first"},
    "decision_maker": {"self", "with_family", "for_someone_else"},
    "use": {"live", "invest"},
}
# Questions about the deal or its papers are about the listing being discussed.
LISTING_INTENTS = {"listing_question", "availability", "photos", "video", "negotiation", "legal_or_documents"}


@dataclass
class KnownListing:
    """A listing this buyer touched, with what we told them and what is true now."""
    listing_id: int
    zameen_id: int
    relation: str
    price_shown: int | None
    status_shown: str | None
    price_now: int
    availability_now: str


@dataclass
class LeadState:
    lead_id: int
    handoff_state: str = "none"                       # none | requested | taken
    slots: dict[str, dict] = field(default_factory=dict)   # slot -> {value, source, confidence}
    asked: dict[str, int] = field(default_factory=dict)    # slot -> times asked
    open_questions: list[str] = field(default_factory=list)
    listings: list[KnownListing] = field(default_factory=list)
    hours_since_last_message: float | None = None


@dataclass
class Plan:
    slot_updates: dict[str, dict] = field(default_factory=dict)
    ignored_slots: list[dict] = field(default_factory=list)    # values outside a slot's choices, not stored
    clear_slots: list[str] = field(default_factory=list)       # e.g. an old place, replaced
    listing_refs: list[dict] = field(default_factory=list)    # {"zameen_id"} or {"history": text} or {"current": True}
    want_photos: bool = False
    want_video: bool = False
    search: dict | None = None                               # Criteria fields, from slots
    preview: dict | None = None                              # buy/rent unknown: count stock both ways
    location_text: str | None = None                         # to resolve with find_location
    place_words: str | None = None                           # their words, to check the model's place pick
    ask: str | None = None
    must: list[dict] = field(default_factory=list)           # things the reply has to say
    handoff: dict | None = None                              # {"reason", "open_items"}
    reject: list[dict] = field(default_factory=list)
    scores: dict = field(default_factory=dict)
    returning: bool = False
    greet: bool = False                                      # first contact, or they greeted


def _known(slots: dict, name: str) -> bool:
    s = slots.get(name)
    if not s or s.get("value") in (None, "", []) or s.get("confidence", 0) < KNOWN_AT:
        return False
    # Buy or rent is never assumed: only what the buyer said counts.
    return s.get("source") == "stated" if name == "purpose" else True


def merge_slots(current: dict, ext: Extraction, ignored: list[dict] | None = None) -> dict:
    """-> the updates to store. A stated value replaces an inferred one; an
    inferred guess never overwrites something the buyer said. A value outside
    a slot's fixed choices is not stored; it goes to `ignored` (shown in the trace)."""
    updates: dict[str, dict] = {}
    for u in ext.slot_updates:
        if u.value in (None, "", []):
            continue
        if u.slot in SLOT_CHOICES and str(u.value) not in SLOT_CHOICES[u.slot]:
            if ignored is not None:
                ignored.append({"slot": u.slot, "value": u.value})
            continue
        before = current.get(u.slot)
        if before and before.get("source") == "stated" and u.source == "inferred":
            continue
        updates[u.slot] = {"value": u.value, "source": u.source, "confidence": u.confidence}
    return updates


def score(slots: dict, ext: Extraction, stock_matches: int | None) -> dict:
    """§7: fit = can Trez serve them; intent = how ready they are. 0-100 each."""
    known = lambda n: _known(slots, n)  # noqa: E731
    fit = sum(20 for n in ("purpose", "budget_max", "location_id", "property_types") if known(n))
    if stock_matches:
        fit += 20
    intent = 0
    if known("budget_max"):
        intent += 20
    intent += {"under_1_month": 25, "1_3_months": 15}.get(str((slots.get("timeline") or {}).get("value")), 0)
    intent += {"cash": 15, "installments": 8, "bank_loan": 8}.get(str((slots.get("payment_mode") or {}).get("value")), 0)
    intent += 10 if known("use") else 0
    intent += 10 if known("decision_maker") else 0
    intent += 15 * sum(s in ext.signals for s in ("visit_request", "cash_ready"))
    ready_to_pay = known("ready_to_pay")          # offered a token: the strongest signal there is
    intent += 40 if ready_to_pay else 0
    if "dealer" in ext.signals:
        intent -= 20
    fit, intent = max(0, min(100, fit)), max(0, min(100, intent))
    if "dealer" in ext.signals:
        priority = "junk"
    elif ready_to_pay or (intent >= 60 and fit >= 50):
        priority = "hot"
    elif intent >= 60:
        priority = "redirect"
    elif fit >= 50:
        priority = "warm" if intent >= 30 else "nurture"
    else:
        priority = "cold"
    return {"fit": fit, "intent": intent, "priority": priority}


def plan(state: LeadState, ext: Extraction, *, stock_matches: int | None = None) -> Plan:
    p = Plan()
    p.slot_updates = merge_slots(state.slots, ext, p.ignored_slots)
    # A token offer is a fact about the buyer, not just this message: remembered,
    # so the lead stays hot on the next turn too.
    if "token_or_bayana" in ext.signals:
        p.slot_updates["ready_to_pay"] = {"value": True, "source": "stated", "confidence": 1.0}
    slots = {**state.slots, **p.slot_updates}
    p.returning = (state.hours_since_last_message or 0) >= RETURNING_AFTER_HOURS
    p.greet = state.hours_since_last_message is None or any(i.type == "greeting" for i in ext.intents)
    types = {i.type for i in ext.intents}

    # --- what changed since we last told them (§9, report first) ----------
    if p.returning:
        for known in state.listings:
            if known.relation == "rejected":
                continue
            if known.availability_now in ("sold", "rented", "withdrawn"):
                p.must.append({"kind": "listing_gone", "listing_id": known.listing_id,
                               "status": known.availability_now})
            elif known.availability_now == "unverified" and known.status_shown == "available":
                p.must.append({"kind": "listing_unverified", "listing_id": known.listing_id})
            elif known.price_shown and known.price_now != known.price_shown:
                p.must.append({"kind": "price_changed", "listing_id": known.listing_id,
                               "was": known.price_shown, "now": known.price_now})

    # --- which listing(s) they mean -----------------------------------------
    # Words that are the place they named this turn are that place, not a listing.
    place_said = {_words(u.said) for u in ext.slot_updates if u.slot in ("location_id", "location_text") and u.said}
    for intent in ext.intents:
        if intent.listing and intent.listing.zameen_id:
            p.listing_refs.append({"zameen_id": intent.listing.zameen_id})
        elif intent.listing and intent.listing.from_history and _words(intent.listing.from_history) not in place_said:
            p.listing_refs.append({"history": intent.listing.from_history})
    # "photos bhejo" next to a link means that link, not the last listing discussed.
    if not p.listing_refs and state.listings and any(i.type in LISTING_INTENTS for i in ext.intents):
        p.listing_refs.append({"current": True})
    p.listing_refs = [dict(t) for t in {tuple(sorted(r.items())) for r in p.listing_refs}]
    p.want_photos = "photos" in types
    p.want_video = "video" in types
    p.reject = [r.model_dump(exclude_none=True) for r in ext.rejected]

    # --- searching ---------------------------------------------------------
    wants_search = bool(types & {"search", "more_options"}) or (
        not p.listing_refs and any(k in p.slot_updates for k in
                                   ("location_id", "budget_max", "property_types", "bedrooms_min"))) or (
        # They answered our "which place?": show them what is there.
        "location_id" in p.slot_updates and "location_id" in state.open_questions)
    if wants_search and _known(slots, "purpose"):
        p.search = {
            "purpose": slots["purpose"]["value"],
            "property_types": _as_list((slots.get("property_types") or {}).get("value")),
            "location_id": (slots.get("location_id") or {}).get("value"),
            "budget_min": (slots.get("budget_min") or {}).get("value"),
            "budget_max": (slots.get("budget_max") or {}).get("value"),
            "size_min_sqyd": (slots.get("size_min_sqyd") or {}).get("value"),
            "size_max_sqyd": (slots.get("size_max_sqyd") or {}).get("value"),
            "bedrooms_min": (slots.get("bedrooms_min") or {}).get("value"),
            "exclude_listing_ids": [k.listing_id for k in state.listings if k.relation == "rejected"],
        }
    elif wants_search:
        # Buy or rent not said yet: count what Trez has both ways (no prices),
        # so the reply can be honest about stock while asking which they want.
        p.preview = {
            "property_types": _as_list((slots.get("property_types") or {}).get("value")),
            "location_id": (slots.get("location_id") or {}).get("value"),
        }
    if "location_text" in p.slot_updates and "location_id" not in p.slot_updates:
        # A new place by name: the old place no longer applies. It is looked up;
        # until then (or if we have nothing there) the search is not tied to a place.
        p.location_text = str(p.slot_updates["location_text"]["value"])
        p.clear_slots.append("location_id")
        for target in (p.search, p.preview):
            if target is not None:
                target["location_id"] = None
    elif "location_id" in p.slot_updates and "location_text" in state.slots:
        p.clear_slots.append("location_text")
    if "location_id" in p.slot_updates:
        # The buyer's own words for the place, so code can check the model's pick.
        p.place_words = next((u.said for u in ext.slot_updates if u.slot == "location_id" and u.said), None)

    # --- scores and handoff -------------------------------------------------
    apply_scores(p, state, ext, stock_matches)

    # --- the one question ---------------------------------------------------
    quiet = "not_interested" in types or p.handoff is not None or state.handoff_state != "none"
    if not quiet:
        if not _known(slots, "purpose"):
            p.ask = "purpose" if state.asked.get("purpose", 0) < MAX_ASKS else None
        else:
            for name in QUESTION_ORDER:
                if name == "bedrooms_min" and set(_as_list((slots.get("property_types") or {}).get("value"))) & {"plot", "commercial"}:
                    continue
                if not _known(slots, name) and state.asked.get(name, 0) < MAX_ASKS \
                        and name not in state.open_questions:
                    p.ask = name
                    break
    return p


def apply_scores(p: Plan, state: LeadState, ext: Extraction, stock_matches: int | None) -> None:
    """Scores and the handoff decision. Run again once the search knows how
    much matching stock there is (fit depends on it)."""
    slots = {**state.slots, **p.slot_updates}
    types = {i.type for i in ext.intents}
    p.scores = score(slots, ext, stock_matches)
    reasons = [HANDOFF_INTENTS[t] for t in types if t in HANDOFF_INTENTS]
    reasons += [HANDOFF_SIGNALS[s] for s in ext.signals if s in HANDOFF_SIGNALS]
    if p.scores["priority"] == "hot" and state.handoff_state == "none":
        reasons.append("hot_lead")
    if reasons and state.handoff_state != "taken":
        ordered = sorted(set(reasons), key=_handoff_rank)
        p.handoff = {"reason": ordered[0], "reasons": ordered,
                     "open_items": [i.question for i in ext.intents
                                    if i.type in HANDOFF_INTENTS and i.question]}
        p.ask = None


def _handoff_rank(reason: str) -> int:
    return HANDOFF_ORDER.index(reason) if reason in HANDOFF_ORDER else len(HANDOFF_ORDER)


def after_tools(p: Plan, *, unclear_listing: bool, unclear_place: bool = False) -> None:
    """The buyer meant a listing or a place we cannot pin down: the one question
    is 'which one?' (a guess would answer about the wrong house or area)."""
    if unclear_listing:
        p.ask = "which_listing"
    elif unclear_place:
        p.ask = "which_place"


def add_unanswered(p: Plan, state: LeadState, unanswered: list[str]) -> None:
    """Questions the facts could not answer go to the agent (never guessed)."""
    if not unanswered or state.handoff_state == "taken":
        return
    if p.handoff is None:
        p.handoff = {"reason": "not_in_data", "reasons": ["not_in_data"], "open_items": []}
    p.handoff["open_items"] = list(dict.fromkeys([*p.handoff["open_items"], *unanswered]))


def _words(text: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in text.lower()).split())


def _as_list(value) -> list[str]:
    if value in (None, ""):
        return []
    return list(value) if isinstance(value, list) else [str(value)]
