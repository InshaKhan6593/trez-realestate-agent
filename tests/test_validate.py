"""Validator, template and place-name rules. Pure: no database, no model."""

from agent.handoff import want_text
from agent.locations import Place, Tree, places_named
from agent.planner import Plan
from agent.respond import ReplyDraft
from agent.runner import Facts
from agent.validate import check, template


def listing(listing_id, price, photos=0, availability="available", title="Brigadier House"):
    return {"listing_id": listing_id, "zameen_id": 50_000_000 + listing_id, "title": title, "price_pkr": price,
            "size_sqyd": 300.0, "location": "Askari 6, Malir Cantonment", "availability": availability,
            "photo_count": photos, "video_count": 0}


def unclear(*candidates):
    return Facts(unresolved_refs=[{"ref": {"history": "wo wala"}, "candidates": list(candidates)}])


# --- facts the responder was shown --------------------------------------------

def test_listings_offered_as_which_one_are_facts_too():
    # The responder was shown these real rows; quoting them is not inventing.
    facts = unclear(listing(35, 80_000_000), listing(39, 79_500_000))
    draft = ReplyDraft(reply="PKR 8 Crore wala ya PKR 7.95 Crore wala?", says_available=[35])
    assert check(draft, Plan(ask="which_listing"), facts, set()) == []


def test_no_question_when_none_was_planned():
    # Live run, returning buyer: "Kya aap isay visit karna chahenge?" (visits are out of scope).
    draft = ReplyDraft(reply="Price ab PKR 7.95 Crore hai. Kya aap visit karna chahenge?")
    facts = Facts(listings={39: listing(39, 79_500_000)})
    assert check(draft, Plan(), facts, set()) == [
        "the reply asks a question, but no question was planned; ask nothing"]
    assert check(draft, Plan(ask="timeline"), facts, set()) == []


def test_the_template_asks_which_place_when_the_name_fits_several():
    facts = Facts(location={"status": "ask", "text": "Emaar", "options": [
        {"name": "Emaar Panorama", "place_id": 17182, "in": "..."},
        {"name": "Emaar The Views", "place_id": 17808, "in": "..."}]})
    t = template(Plan(), facts, "english")
    assert t.text == "Which one do you mean: Emaar Panorama / Emaar The Views?" and not t.needs_agent


def test_a_price_nobody_gave_is_still_caught():
    draft = ReplyDraft(reply="Ye PKR 7.5 Crore ka hai.")
    assert check(draft, Plan(), unclear(listing(35, 80_000_000)), set()) == [
        "the reply states PKR 7.5 Crore, which is not in the facts"]


def test_the_buyers_own_offer_may_be_repeated():
    draft = ReplyDraft(reply="Aap ka PKR 7.8 Crore ka offer agent tak pohanch jayega.")
    assert check(draft, Plan(handoff={"reason": "negotiation"}), Facts(), {78_000_000}) == []


def test_a_changed_price_may_state_the_old_price():
    # Live run, returning buyer: the MUST said "changed from 8.5 Cr to 7.95 Cr",
    # and the old price was then rejected as not a fact.
    plan = Plan(must=[{"kind": "price_changed", "listing_id": 39, "was": 85_000_000, "now": 79_500_000}])
    facts = Facts(listings={39: listing(39, 79_500_000)})
    draft = ReplyDraft(reply="Is ki qeemat PKR 8.5 Crore se PKR 7.95 Crore ho gayi hai.", listing_ids_mentioned=[39])
    assert check(draft, plan, facts, set()) == []


def test_the_json_answer_pasted_into_the_message_is_caught():
    draft = ReplyDraft(reply='Filename: data.json\n\n'
                             '{"reply": "Aap buy karna chahte hain?", "listing_ids_mentioned": []}')
    assert check(draft, Plan(), Facts(), set())[0].startswith("the reply contains the JSON answer itself")


def test_bringing_in_the_agent_without_saying_so_is_caught():
    # Live run: "Agent aapko guide kar dega" with no handoff, and not declared.
    draft = ReplyDraft(reply="Kitne bedrooms chahiye? Agent aapko guide kar dega.")
    assert check(draft, Plan(ask="bedrooms_min"), Facts(), set()) == [
        "the reply brings in our agent, but no handoff was made; do not mention the agent"]
    assert check(draft, Plan(ask="bedrooms_min", handoff={"reason": "negotiation"}), Facts(), set()) == []


def test_the_prompts_own_words_never_reach_the_buyer():
    # Live run: "Pehle wale ka advance ... FACTS mein kuch mention nahi hai".
    from agent.respond import build_prompt
    from agent.validate import prompt_words
    words = prompt_words(build_prompt(Plan(), Facts(), [{"type": "text", "text": "advance?"}], [], "roman_urdu", None))
    assert {"FACTS", "ASK", "stock_preview", "which_listing_do_they_mean"} <= words
    leak = ReplyDraft(reply="Advance ke baare mein FACTS mein kuch mention nahi hai.")
    assert check(leak, Plan(), Facts(), set(), words) == [
        "the reply uses our internal words ['FACTS']; talk about the listing, never about our data"]
    fine = ReplyDraft(reply="Listing mein advance ka zikr nahi hai; Sector J ka flat PKR 5.5 Crore ka hai.")
    assert check(fine, Plan(), Facts(), {55_000_000}, words) == []


def test_a_visit_is_never_arranged_by_the_bot():
    # Live run: "Aap kal dekhne aa sakte hain".
    draft = ReplyDraft(reply="Aap kal aa sakte hain.", promises_visit=True)
    assert check(draft, Plan(handoff={"reason": "visit_request"}), Facts(), set()) == [
        "the reply arranges or confirms a visit; only our agent arranges visits, say they will"]


def test_a_feature_is_claimed_only_if_the_listing_says_it():
    # Live run: "parking bhi hai" for a house whose listing says nothing about parking.
    house = {**listing(42, 79_500_000), "amenities": [{"label": "Lawn or Garden", "value": True}]}
    facts = Facts(listings={42: house})
    made_up = ReplyDraft(reply="Parking bhi hai.", features_said=["parking"])
    assert check(made_up, Plan(), facts, set()) == [
        "the reply says a listing has ['parking'], which its facts do not say; claim only features written in FACTS"]
    real = ReplyDraft(reply="Lawn bhi hai.", features_said=["Lawn or Garden"])
    assert check(real, Plan(), facts, set()) == []


def test_a_search_that_found_nothing_is_said_plainly():
    # Live run: "Yeh list kar raha hoon" when the search had found nothing.
    facts = Facts(search={"stage": "none", "total": 0, "results": []})
    draft = ReplyDraft(reply="Yeh options dekh lein.")
    assert check(draft, Plan(), facts, set()) == [
        "the search found nothing that matches; say so plainly (and set claims_no_listings)"]
    honest = ReplyDraft(reply="Is budget mein abhi kuch nahi mila.", claims_no_listings=True)
    assert check(honest, Plan(), facts, set()) == []


def test_closest_options_are_introduced_as_not_an_exact_match():
    facts = Facts(search={"stage": "closest", "no_exact_match": True, "total": 0,
                          "results": [listing(42, 79_500_000)]})
    as_if_exact = ReplyDraft(reply="Ye options hain.", listing_ids_mentioned=[42])
    assert check(as_if_exact, Plan(), facts, set()) == [
        "the search found nothing that matches; say so plainly (and set claims_no_listings)"]
    honest = ReplyDraft(reply="Bilkul aisa nahi mila; sab se qareeb ye hai.", listing_ids_mentioned=[42],
                        claims_no_listings=True)
    assert check(honest, Plan(), facts, set()) == []


def test_the_agent_may_be_mentioned_while_their_handoff_is_open():
    # Live run: "theek hai shukriya" after a visit request: the agent mention was rejected.
    draft = ReplyDraft(reply="Shukriya! Hamare agent jald rabta karenge.", promises_agent_contact=True)
    assert check(draft, Plan(), Facts(), set()) != []
    assert check(draft, Plan(), Facts(), set(), handoff_open=True) == []


def test_numbers_written_without_a_unit():
    from agent.money import bare_numbers
    assert bare_numbers("doosra wala 8.5 pe de do") == [8.5]
    assert bare_numbers("8 crore final karein, 7.9 lakh") == []              # units: amounts_in reads those
    assert bare_numbers("call 03001234567") == []


# --- what the models are told about a message ----------------------------------

def test_messages_we_cannot_take_in_are_described_as_such():
    from agent.context import for_model, has_urdu_script, unheard
    burst = [{"type": "audio", "text": None}, {"type": "image", "text": "ye wala?"},
             {"type": "location", "text": "[location 24.9,67.1] Gulistan-e-Jauhar"},
             {"type": "sticker", "text": None}, {"type": "text", "text": "salam"}]
    assert for_model(burst[0]) == {"kind": "voice_note", "words": None, "we_can_see_or_hear_its_content": False}
    assert for_model(burst[1])["words"] == "ye wala?" and for_model(burst[4]) == "salam"
    assert for_model(burst[2])["we_can_see_or_hear_its_content"] is True
    assert unheard(burst) == ["audio", "image"]                # a sticker needs no one
    assert has_urdu_script(["پہلے والے کی تصاویر"]) and not has_urdu_script(["pehle wale ki photos"])


# --- photos -------------------------------------------------------------------

def test_photos_promised_but_not_being_sent():
    facts = Facts(listings={35: listing(35, 80_000_000, photos=3)})
    draft = ReplyDraft(reply="Photos bhej rahe hain.", promises_media=True)
    assert check(draft, Plan(), facts, set()) == [
        "the reply says photos or a video are coming, but none are being sent"]
    facts.media = {"listing_id": 35, "photo_count": 3, "photos_sending": 3, "video_urls": []}
    assert check(draft, Plan(), facts, set()) == []


def test_the_number_of_photos_said_is_the_number_sent():
    # Live run: "Photos 11 hain, main bhej raha hoon" while 4 (DEFAULT_PHOTOS) go out.
    facts = Facts(listings={35: listing(35, 80_000_000, photos=11)},
                  media={"listing_id": 35, "photo_count": 11, "photos_sending": 4, "video_urls": []})
    wrong = ReplyDraft(reply="11 photos bhej raha hoon.", promises_media=True, photos_coming_said=11)
    assert check(wrong, Plan(), facts, set()) == [
        "the reply says 11 photos are being sent, but 4 are; say 4 (the listing's total may be added)"]
    right = ReplyDraft(reply="4 photos bhej raha hoon, total 11 hain.", promises_media=True, photos_coming_said=4)
    assert check(right, Plan(), facts, set()) == []


def test_no_photos_said_when_the_listing_has_them():
    # Live run, turn 7: "Photos ke liye abhi available nahi hain" after 3 photos were sent.
    facts = Facts(listings={35: listing(35, 80_000_000, photos=3)})
    draft = ReplyDraft(reply="Photos abhi available nahi hain.", says_no_photos=True)
    assert check(draft, Plan(), facts, set()) == [
        "the reply says photos are not available, but listing 35 has 3 photos"]
    facts.listings[35]["photo_count"] = 0
    assert check(draft, Plan(), facts, set()) == []


# --- template -----------------------------------------------------------------

def test_the_template_is_never_empty():
    # Live run, turn 4: no facts to build from -> an empty WhatsApp message was sent.
    t = template(Plan(), Facts(), "roman_urdu")
    assert t.text == "Hamare agent jald aap se rabta karenge." and t.needs_agent and t.listing_ids == []


def test_the_template_asks_which_one_from_the_candidates():
    t = template(Plan(), unclear(listing(35, 80_000_000), listing(39, 79_500_000)), "roman_urdu")
    assert "PKR 8 Crore" in t.text and "PKR 7.95 Crore" in t.text and "kaun si listing" in t.text
    assert t.listing_ids == [35, 39] and not t.needs_agent


def test_the_template_names_the_listings_it_shows():
    facts = Facts(listings={35: listing(35, 80_000_000)})
    t = template(Plan(handoff={"reason": "negotiation"}), facts, "english")
    assert t.listing_ids == [35] and t.text.endswith("Our agent will contact you shortly.")


# --- which places do the buyer's words name? ----------------------------------
# A made-up tree, so the rule is tested apart from whatever Trez lists today.

def _place(pid, name, *path):
    return Place(pid, name, path[-2] if len(path) > 1 else None, path, None, None)


TREE = Tree({p.id: p for p in [
    _place(1, "Karachi", 1),
    _place(2, "Sunrise Towers", 1, 2), _place(3, "Sunrise Heights", 1, 3),          # a new project, two towers
    _place(4, "Askari 5", 1, 4), _place(5, "Askari 5 - Sector J", 1, 4, 5), _place(6, "Askari 6", 1, 6),
    _place(7, "Gulshan-e-Iqbal", 1, 7), _place(8, "Gulshan-e-Iqbal - Block 2", 1, 7, 8),
]})


def test_words_that_fit_several_places_are_ambiguous():
    assert places_named(TREE, "sunrise") == [3, 2]                  # ask: Heights or Towers?
    assert places_named(TREE, "Askari") == [4, 6]                   # Sector J is inside Askari 5


def test_an_exact_or_single_place_is_not_ambiguous():
    assert places_named(TREE, "askari 5") == [4]                    # its full name, despite Sector J
    assert places_named(TREE, "Askari 5 Sector J") == [5]           # punctuation and case ignored
    assert places_named(TREE, "gulshan") == [7]                     # Block 2 is inside it
    assert places_named(TREE, "Sunrise Towers") == [2]


def test_a_full_place_name_in_the_message_is_found():
    from agent.locations import places_in_text
    assert places_in_text(TREE, "Askari 6 villa rate?") == [6]
    assert places_in_text(TREE, "askari 5 sector j mein flat") == [5]          # the more specific one
    assert places_in_text(TREE, "Askari 6 ya Gulshan-e-Iqbal?") == [6, 7]      # two places: caller decides
    assert places_in_text(TREE, "askari mein ghar") == []                      # not a full name: model's job
    assert places_in_text(TREE, "Sunrise Towers ka rate") == [2]               # a new project, no code change


def test_words_naming_no_place_leave_it_to_the_model():
    assert places_named(TREE, "Askari V") == []                     # the model reads "V" as 5
    assert places_named(TREE, "Bahria Town") == []
    assert places_named(TREE, "sunrise", among={2}) == [2]          # only places we offer count


# --- the agent's alert --------------------------------------------------------

class _Tree:
    places = {21109: object()}

    def label(self, place_id):
        return "Askari 6, Malir Cantonment, Cantt, Karachi"


def test_the_alert_says_what_the_buyer_wants_in_words():
    tree = _Tree()
    assert want_text("budget_max", 85_000_000, tree) == "up to PKR 8.5 Cr"
    assert want_text("location_id", 21109, tree) == "Askari 6"
    assert want_text("purpose", "sale", tree) == "buy"
    assert want_text("bedrooms_min", 4, tree) == "4+ bedrooms"
    assert want_text("timeline", "under_1_month", tree) == "within a month"
    assert want_text("ready_to_pay", True, tree) == "ready to pay a token"
    # Something we have no wording for still shows, readably, instead of vanishing.
    assert want_text("parking_needed", "yes", tree) == "parking needed: yes"
    assert want_text("timeline", "someday", tree) == "timeline: someday"
