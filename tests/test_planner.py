"""Planner rules. Pure: no database, no model."""

from datetime import datetime, timezone

from agent.planner import KnownListing, LeadState, after_tools, merge_slots, plan, score
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


def test_a_token_offer_is_hot_and_remembered():
    # "8 crore final karein to aaj token de dun": warm by points alone, but money is on the table.
    slots = {"purpose": stated("sale"), "budget_max": stated(85_000_000)}
    known = KnownListing(35, 52735413, "inquired", 80_000_000, "available", 80_000_000, "available")
    p = plan(LeadState(1, slots=slots, listings=[known]),
             ext("negotiation", "ask_human", signals=["token_or_bayana"]))
    assert p.slot_updates["ready_to_pay"]["value"] is True
    assert p.scores["priority"] == "hot"
    # The agent reads the strongest reason first, not the alphabetically first one.
    assert p.handoff["reasons"] == ["ready_to_pay", "negotiation", "asked_for_human", "hot_lead"]
    # Next turn, no signal in the message: still hot, because it was remembered.
    later = plan(LeadState(1, slots={**slots, "ready_to_pay": stated(True)}), ext("thanks"))
    assert later.scores["priority"] == "hot"


def test_price_talk_without_a_reference_means_the_listing_being_discussed():
    known = KnownListing(35, 52735413, "inquired", 80_000_000, "available", 80_000_000, "available")
    p = plan(LeadState(1, listings=[known]), ext("negotiation"))
    assert p.listing_refs == [{"current": True}]


def test_answering_which_place_shows_what_is_there():
    # Live run: to "Emaar Panorama or Emaar The Views?" the buyer said "Emaar Panorama";
    # the model also read it as a listing ("from_history"), which blocked the search.
    state = LeadState(1, slots={"purpose": stated("sale"), "property_types": stated(["flat"])},
                      open_questions=["location_id"])
    e = Extraction(language="english", intents=[Intent(type="listing_question",
                                                       listing=ListingRef(from_history="Emaar Panorama"))],
                   slot_updates=[SlotUpdate(slot="location_id", value=17182, said="Emaar Panorama",
                                            source="stated", confidence=1.0)])
    p = plan(state, e)
    assert p.listing_refs == []                         # the place, not a listing
    assert p.search is not None and p.search["location_id"] == 17182


def test_an_unclear_listing_makes_the_question_which_one():
    p = plan(LeadState(1, slots={"purpose": stated("sale")}), ext({"type": "listing_question",
                                                                   "listing": {"from_history": "wo wala"}}))
    assert p.ask == "location_id"
    after_tools(p, unclear_listing=True)
    assert p.ask == "which_listing"


def test_values_are_stored_in_the_type_the_code_uses():
    # The model sometimes sends numbers as text ("3"): comparing a listing's bedrooms with "3" breaks the search.
    e = ext("search", slots=[("bedrooms_min", "3", "stated", 1.0), ("budget_max", "60,000,000", "stated", 1.0),
                             ("property_types", "flat", "stated", 1.0), ("size_min_sqyd", "ya", "stated", 1.0)])
    p = plan(LeadState(1), e)
    assert p.slot_updates["bedrooms_min"]["value"] == 3 and p.slot_updates["budget_max"]["value"] == 60_000_000
    assert p.slot_updates["property_types"]["value"] == ["flat"]
    assert "size_min_sqyd" not in p.slot_updates
    assert {"slot": "size_min_sqyd", "value": "ya", "why": "not a usable value"} in p.ignored_slots


def test_the_model_answers_one_field_per_detail_and_the_code_gets_them_all():
    # Live run: "teen kamron wala ghar" as a free list kept "3 rooms" and dropped "ghar" 9 times in 12.
    e = Extraction.model_validate({"language": "roman_urdu", "intents": [{"type": "search"}], "wants": {
        "bedrooms_min": {"value": "3", "said": "teen kamron", "source": "stated", "confidence": 1},
        "property_types": {"value": ["house"], "said": "ghar", "source": "stated", "confidence": 1},
        "budget_max": None}})
    assert {u.slot for u in e.slot_updates} == {"bedrooms_min", "property_types"}
    schema = Extraction.model_json_schema()
    assert "wants" in schema["required"] and "slot_updates" not in schema["properties"]
    assert "property_types" in schema["$defs"]["Wants"]["required"]       # every field must be answered


def test_a_value_outside_a_slots_choices_is_not_stored():
    # Live run: "main dealer hoon" came back as decision_maker = "dealer".
    p = plan(LeadState(1), ext("more_options", slots=[("decision_maker", "dealer", "stated", 1.0),
                                                      ("timeline", "1_3_months", "stated", 1.0)],
                               signals=["dealer"]))
    assert "decision_maker" not in p.slot_updates and p.slot_updates["timeline"]["value"] == "1_3_months"
    assert p.ignored_slots == [{"slot": "decision_maker", "value": "dealer"}]


def test_dealer_is_junk():
    assert score({}, ext("search", signals=["dealer"]), None)["priority"] == "junk"


def test_an_inferred_purpose_is_not_known():
    # "flat chahiye" does not say buy or rent; a model's guess must not decide it.
    state = LeadState(1)
    p = plan(state, ext("search", slots=[("purpose", "sale", "inferred", 0.9),
                                         ("location_id", 6655, "stated", 1.0),
                                         ("property_types", ["flat"], "stated", 1.0)]))
    assert p.search is None and p.ask == "purpose"
    assert p.preview["property_types"] == ["flat"] and p.preview["location_id"] == 6655


def test_the_preview_counts_with_everything_the_buyer_said():
    # Live run: "120 gaz plot, 1 crore tak" was counted as "all plots", and the reply guessed
    # that none matched. The count now uses size and budget too.
    p = plan(LeadState(1), ext("search", slots=[("property_types", ["plot"], "stated", 1.0),
                                                ("size_min_sqyd", 120, "stated", 1.0),
                                                ("budget_max", 10_000_000, "stated", 1.0)]))
    assert p.preview["size_min_sqyd"] == 120 and p.preview["budget_max"] == 10_000_000
    assert p.preview["show_both"] is False


def test_asked_again_to_see_options_shows_both_ways():
    # Live run: "jo hai woh dikha dein" got the buy-or-rent question again and no listings.
    state = LeadState(1, slots={"property_types": stated(["house"])}, asked={"purpose": 1})
    assert plan(state, ext("more_options")).preview["show_both"] is True
    assert plan(LeadState(1), ext("more_options")).preview["show_both"] is True


def test_more_options_leaves_out_what_they_have_seen():
    # Live run: "aur options dikhayein" returned the same three listings.
    seen = [KnownListing(i, 100 + i, "shown", 1, "available", 1, "available") for i in (5, 6)]
    rejected = KnownListing(7, 107, "rejected", 1, "available", 1, "available")
    state = LeadState(1, slots={"purpose": stated("sale")}, listings=[*seen, rejected])
    assert sorted(plan(state, ext("more_options")).search["exclude_listing_ids"]) == [5, 6, 7]
    assert plan(state, ext("search")).search["exclude_listing_ids"] == [7]   # a new search: only rejected


def test_a_seller_is_not_searched_for_and_their_property_is_not_their_wants():
    # Live run: a seller was shown three houses for sale, and the alert read "Wants: buy · house".
    e = Extraction(language="roman_urdu", intents=[Intent(type="seller")], slot_updates=[
        SlotUpdate(slot="purpose", value="sale", said="bechna hai", source="stated", confidence=1.0),
        SlotUpdate(slot="location_id", value=21109, said="Askari 6", source="stated", confidence=1.0),
        SlotUpdate(slot="size_max_sqyd", value=375, said="375 gaz", source="stated", confidence=1.0)])
    p = plan(LeadState(1), e)
    assert p.slot_updates == {} and p.search is None and p.preview is None
    assert p.handoff["reason"] == "seller"
    assert p.handoff["open_items"] == ["Wants to sell their property: Askari 6, 375 gaz"]


def test_a_visit_request_is_remembered():
    p = plan(LeadState(1), ext("listing_question", signals=["visit_request"]))
    assert p.slot_updates["wants_visit"]["value"] is True and p.handoff["reason"] == "visit_request"
    slots = {"purpose": stated("sale"), "budget_max": stated(60_000_000), "wants_visit": stated(True)}
    assert plan(LeadState(1, slots=slots), ext("thanks")).scores["intent"] == 35   # 20 budget + 15 visit


def test_a_new_place_by_name_replaces_the_old_place():
    state = LeadState(1, slots={"purpose": stated("sale"), "location_id": stated(12242)})
    p = plan(state, ext("search", slots=[("location_text", "Bahria Town", "stated", 1.0)]))
    assert p.location_text == "Bahria Town" and "location_id" in p.clear_slots
    assert p.search["location_id"] is None        # not searched around the old place
