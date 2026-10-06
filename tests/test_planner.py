"""Planner rules. Pure: no database, no model."""

from datetime import datetime, timezone

from agent.planner import KnownListing, LeadState, merge_slots, plan, score
from agent.schemas import Extraction, Intent, ListingRef, SlotUpdate

NOW = datetime(2026, 10, 6, tzinfo=timezone.utc)


def ext(*intents, slots=(), signals=(), rejected=(), language="roman_urdu"):
    return Extraction(
        language=language,
        intents=[Intent(**i) if isinstance(i, dict) else Intent(type=i) for i in intents],
        slot_updates=[SlotUpdate(slot=s, value=v, source=src, confidence=c) for s, v, src, c in slots],
        signals=list(signals),
        rejected=[ListingRef(**r) for r in rejected],
    )


def stated(value, confidence=1.0):
    return {"value": value, "source": "stated", "confidence": confidence}


# --- slots --------------------------------------------------------------------

def test_a_guess_never_overwrites_what_the_buyer_said():
    current = {"budget_max": stated(40_000_000)}
    e = ext("search", slots=[("budget_max", 50_000_000, "inferred", 0.6), ("use", "live", "stated", 1.0)])
    assert merge_slots(current, e) == {"use": stated("live")}


def test_stated_replaces_inferred_and_late_answers_close_open_questions():
    state = LeadState(1, slots={"budget_max": {"value": 30_000_000, "source": "inferred", "confidence": 0.5}},
                      open_questions=["budget_max"])
    p = plan(state, ext("other", slots=[("budget_max", 30_000_000, "stated", 1.0)]))
    assert p.slot_updates["budget_max"]["source"] == "stated"


# --- questions ----------------------------------------------------------------

def test_purpose_is_asked_first_and_never_assumed():
    p = plan(LeadState(1), ext("search", slots=[("location_id", 17289, "stated", 1.0)]))
    assert p.search is None and p.ask == "purpose"


def test_one_question_the_most_useful_missing_one():
    state = LeadState(1, slots={"purpose": stated("sale"), "location_id": stated(17289)})
    assert plan(state, ext("search")).ask == "property_types"


def test_never_asks_again_after_two_tries():
    state = LeadState(1, slots={"purpose": stated("sale"), "location_id": stated(17289)},
                      asked={"property_types": 2})
    assert plan(state, ext("search")).ask == "budget_max"


def test_no_bedroom_question_for_a_plot():
    state = LeadState(1, slots={"purpose": stated("sale"), "location_id": stated(1345),
                                "property_types": stated(["plot"]), "budget_max": stated(10_000_000)})
    assert plan(state, ext("search")).ask == "timeline"


def test_quiet_when_not_interested():
    assert plan(LeadState(1), ext("not_interested")).ask is None


# --- search -------------------------------------------------------------------

def test_search_uses_everything_known_and_skips_rejected_listings():
    rejected = KnownListing(7, 111, "rejected", None, None, 1, "available")
    state = LeadState(1, slots={"purpose": stated("sale"), "property_types": stated(["flat"])},
                      listings=[rejected])
    p = plan(state, ext("search", slots=[("location_id", 17289, "stated", 1.0),
                                         ("budget_max", 50_000_000, "stated", 1.0)]))
    assert p.search["purpose"] == "sale" and p.search["location_id"] == 17289
    assert p.search["budget_max"] == 50_000_000 and p.search["exclude_listing_ids"] == [7]


def test_unknown_place_name_goes_to_the_location_finder():
    state = LeadState(1, slots={"purpose": stated("sale")})
    p = plan(state, ext("search", slots=[("location_text", "askri 6", "stated", 0.9)]))
    assert p.location_text == "askri 6"


# --- listings -----------------------------------------------------------------

def test_a_link_is_looked_up_and_photos_requested():
    p = plan(LeadState(1), ext({"type": "photos", "listing": {"zameen_id": 54467403}}))
    assert p.listing_refs == [{"zameen_id": 54467403}] and p.want_photos


def test_a_question_without_a_reference_means_the_listing_being_discussed():
    known = KnownListing(5, 54467403, "inquired", 8_500_000, "available", 8_500_000, "available")
    p = plan(LeadState(1, listings=[known]), ext({"type": "listing_question", "topic": "parking"}))
    assert p.listing_refs == [{"current": True}]


# --- returning buyers ---------------------------------------------------------

def test_returning_buyer_hears_what_changed_first():
    sold = KnownListing(5, 1, "liked", 80_000_000, "available", 80_000_000, "sold")
    cheaper = KnownListing(6, 2, "shown", 90_000_000, "available", 85_000_000, "available")
    stale = KnownListing(7, 3, "shown", 70_000_000, "available", 70_000_000, "unverified")
    state = LeadState(1, listings=[sold, cheaper, stale], hours_since_last_message=48)
    must = plan(state, ext("greeting")).must
    assert {"kind": "listing_gone", "listing_id": 5, "status": "sold"} in must
    assert {"kind": "price_changed", "listing_id": 6, "was": 90_000_000, "now": 85_000_000} in must
    assert {"kind": "listing_unverified", "listing_id": 7} in must


def test_no_change_report_within_the_same_sitting():
    sold = KnownListing(5, 1, "liked", 80_000_000, "available", 80_000_000, "sold")
    assert plan(LeadState(1, listings=[sold], hours_since_last_message=1), ext("greeting")).must == []


# --- handoff ------------------------------------------------------------------

def test_negotiation_hands_off_and_carries_the_question():
    p = plan(LeadState(1), ext({"type": "negotiation", "question": "last kitna hoga?"}))
    assert p.handoff == {"reason": "negotiation", "reasons": ["negotiation"],
                         "open_items": ["last kitna hoga?"]}
    assert p.ask is None


def test_visit_request_hands_off():
    assert plan(LeadState(1), ext("other", signals=["visit_request"])).handoff["reason"] == "visit_request"


def test_no_new_handoff_while_the_agent_has_it():
    assert plan(LeadState(1, handoff_state="taken"), ext("negotiation")).handoff is None


def test_no_questions_while_a_handoff_is_pending():
    state = LeadState(1, handoff_state="requested", slots={"purpose": stated("sale")})
    assert plan(state, ext("search")).ask is None


# --- scores -------------------------------------------------------------------

def test_hot_lead_from_facts_not_vibes():
    slots = {"purpose": stated("sale"), "budget_max": stated(90_000_000), "location_id": stated(21109),
             "property_types": stated(["house"]), "timeline": stated("under_1_month"),
             "payment_mode": stated("cash")}
    s = score(slots, ext("search"), stock_matches=5)
    assert s == {"fit": 100, "intent": 60, "priority": "hot"}
    p = plan(LeadState(1, slots=slots), ext("search"), stock_matches=5)
    assert p.handoff["reason"] == "hot_lead"


def test_dealer_is_junk():
    assert score({}, ext("search", signals=["dealer"]), None)["priority"] == "junk"


def test_an_inferred_purpose_is_not_known():
    # "flat chahiye" does not say buy or rent; a model's guess must not decide it.
    state = LeadState(1)
    p = plan(state, ext("search", slots=[("purpose", "sale", "inferred", 0.9),
                                         ("location_id", 6655, "stated", 1.0),
                                         ("property_types", ["flat"], "stated", 1.0)]))
    assert p.search is None and p.ask == "purpose"
    assert p.preview == {"property_types": ["flat"], "location_id": 6655}


def test_a_new_place_by_name_replaces_the_old_place():
    state = LeadState(1, slots={"purpose": stated("sale"), "location_id": stated(12242)})
    p = plan(state, ext("search", slots=[("location_text", "Bahria Town", "stated", 1.0)]))
    assert p.location_text == "Bahria Town" and "location_id" in p.clear_slots
    assert p.search["location_id"] is None        # not searched around the old place
