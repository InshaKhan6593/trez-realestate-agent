"""Agent tools against the local database loaded from snapshot 2026-10-06T1730.

Skipped when local Supabase is not running or not loaded.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from agent.listings import (
    Criteria,
    availability,
    get_listing,
    resolve_listing,
    search_listings,
)
from agent.locations import decide, find_location, place_choices

DB_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:54322/postgres")


def _loaded() -> bool:
    try:
        with psycopg.connect(DB_URL, connect_timeout=2) as conn:
            return conn.execute(
                "SELECT count(*) FROM listings WHERE zameen_id = 54467403").fetchone()[0] == 1
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _loaded(), reason="local Supabase with listings not available")


def run(fn, *args, **kwargs):
    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL) as conn:
            return await fn(conn, *args, **kwargs)
    return asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)


def listing_id(zameen_id: int) -> int:
    with psycopg.connect(DB_URL) as conn:
        return conn.execute("SELECT id FROM listings WHERE zameen_id = %s", (zameen_id,)).fetchone()[0]


# --- locations ---------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("askari 5 sector j", "Askari 5 - Sector J"),
    ("Askari 5 Sector-J", "Askari 5 - Sector J"),
    ("dha phase8", "DHA Phase 8"),
    ("malir cantonment", "Malir Cantonment"),
    ("gulistan e jauhar", "Gulistan-e-Jauhar"),
])
def test_typed_place_names_resolve(text, expected):
    matches = run(find_location, text)
    assert decide(matches) == "match" and matches[0].name == expected


def test_a_typo_is_asked_about_not_guessed():
    matches = run(find_location, "askri 6")
    assert decide(matches) == "ask" and matches[0].name == "Askari 6"


@pytest.mark.parametrize("text", ["bahria town", "clifton"])
def test_places_we_have_nothing_in_are_none(text):
    # "bahria town" must not match Gadap Town on the shared word "town".
    assert decide(run(find_location, text)) == "none"


def test_place_choices_are_zameens_names_with_context():
    choices = run(place_choices)
    sector_j = next(c for c in choices if c["id"] == 17289)
    assert sector_j["name"] == "Askari 5 - Sector J" and sector_j["in"].startswith("Askari 5, Malir Cantonment")


# --- search ------------------------------------------------------------------

def test_exact_place_first():
    r = run(search_listings, Criteria("sale", ["flat"], location_id=17289, budget_max=50_000_000))
    assert r["stage"] == "exact" and r["total"] > 0
    assert all(x["location"].startswith("Askari 5 - Sector J") for x in r["results"])
    assert all(x["price_pkr"] <= 50_000_000 for x in r["results"])


def test_nothing_in_the_sector_moves_up_one_level():
    # Sector G has a house but no flat: one level up is Askari 5; nearest first.
    r = run(search_listings, Criteria("sale", ["flat"], location_id=12242, budget_max=50_000_000))
    assert r["stage"] == "nearby" and r["nothing_in_asked_location"]
    assert (r["widened_to"], r["levels_up"]) == ("Askari 5", 1)
    assert all(x["location"].startswith("Askari 5 - Sector") for x in r["results"])
    distances = [x["distance_km"] for x in r["results"]]
    assert distances == sorted(distances)


def test_climbs_level_by_level_until_a_level_has_matches():
    # Askari 2 has no house, nor does Karachi Cantonment above it; the next level,
    # Cantt, has houses in Malir Cantonment: shown closest first, with real distances.
    r = run(search_listings, Criteria("sale", ["house"], location_id=6638))
    assert (r["widened_to"], r["levels_up"]) == ("Cantt", 2)
    assert all("Cantt" in x["location"] for x in r["results"])
    distances = [x["distance_km"] for x in r["results"]]
    assert distances == sorted(distances) and distances[0] > 10      # honest: it is far


def test_nothing_fits_everything_so_the_closest_are_shown_with_how_they_differ():
    # Live run: "10 marla ghar, 5 crore" matched nothing, and "show what you have" got a question.
    # A budget below every house: the cheapest houses come first, each saying what it misses.
    r = run(search_listings, Criteria("sale", ["house"], budget_max=1_000, size_min_sqyd=100))
    assert r["stage"] == "closest" and r["no_exact_match"] and r["total"] == 0 and r["results"]
    assert all(x["property_type"] == "house" and x["purpose"] == "sale" for x in r["results"])
    assert all({"criterion": "budget_max", "asked": 1_000} in x["differs_from_request"] for x in r["results"])
    prices = [x["price_pkr"] for x in r["results"]]
    fewest = min(len(x["differs_from_request"]) for x in r["results"])
    assert len(r["results"][0]["differs_from_request"]) == fewest
    assert prices[0] == min(p for x, p in zip(r["results"], prices) if len(x["differs_from_request"]) == fewest)


def test_the_closest_never_change_buy_or_rent_or_the_type():
    r = run(search_listings, Criteria("sale", ["no-such-type"], budget_max=1_000))
    assert r["stage"] == "exact" and r["results"] == []


def test_purpose_is_never_assumed():
    with pytest.raises(ValueError):
        run(search_listings, Criteria("", ["house"]))


def test_rent_and_sale_never_mix():
    r = run(search_listings, Criteria("rent", [], budget_max=10_000_000))
    assert r["results"] and all(x["purpose"] == "rent" for x in r["results"])


# --- get / resolve -----------------------------------------------------------

def test_link_resolves_by_the_zameen_id_inside_it():
    r = run(resolve_listing, None, text="ye wala? https://www.zameen.com/Property/x-54467403-1345-2.html")
    assert r["how"] == "zameen_id" and r["match"] == listing_id(54467403)


def test_get_listing_has_the_installment_plan_and_availability():
    listing = run(get_listing, listing_id(54467403))
    assert listing["payment_plan"]["monthlyAmount"] == 60_000
    assert listing["availability"] in ("available", "unverified")
    assert listing["location"].startswith("Memon Goth")


def test_amenities_come_through_as_the_agent_entered_them():
    listing = run(get_listing, listing_id(47397605))
    amenities = {a["slug"]: a["value"] for a in listing["amenities"]}
    assert amenities["built-in-year"] == 2023 and amenities["parking-spaces"] == 2


def test_stale_means_unverified_never_available():
    now = datetime.now(timezone.utc)
    assert availability("available", now - timedelta(days=1), now) == "available"
    assert availability("available", now - timedelta(days=8), now) == "unverified"
    assert availability("needs_verification", now, now) == "unverified"
    assert availability("sold", now, now) == "sold"


def test_buy_or_rent_unsaid_and_nothing_in_the_place_shows_the_nearest_of_the_one_way_that_has_any():
    # Live run: plots asked after "Askari 6" (none there) got "our agent will contact you"
    # instead of the plots one level up. A place with listings but none of a type sold elsewhere:
    from agent.planner import LeadState, Plan
    from agent.runner import run_tools
    with psycopg.connect(DB_URL) as conn:
        kind, place = conn.execute(
            """SELECT k.property_type, l.location_id FROM listings l,
                      (SELECT property_type FROM listings WHERE status = 'available' GROUP BY property_type
                       HAVING bool_and(purpose = 'sale')) k
               WHERE l.status = 'available' AND NOT EXISTS (SELECT 1 FROM listings x WHERE x.status = 'available'
                     AND x.location_id = l.location_id AND x.property_type = k.property_type)
               LIMIT 1""").fetchone()
    plan = Plan(preview={"property_types": [kind], "location_id": place, "exclude_listing_ids": []})
    facts = run(run_tools, LeadState(1), plan)
    assert facts.stock_preview["for_sale"] == facts.stock_preview["for_rent"] == 0
    assert facts.stock_preview["listings_shown_for"] == ["sale"]
    assert facts.search["buyer_has_not_said_buy_or_rent"] and facts.search["stage"] in ("nearby", "closest")
    assert facts.search["results"] and all(r["property_type"] == kind for r in facts.search["results"])


def test_buy_or_rent_unsaid_and_nothing_in_the_place_shows_the_nearest_both_ways():
    # Live: "show me some houses in DHA" (Trez has nothing there) got "we have none" and a
    # budget question, though houses are for sale and for rent elsewhere.
    from agent.planner import LeadState, Plan
    from agent.runner import run_tools
    with psycopg.connect(DB_URL) as conn:
        found = conn.execute(
            """SELECT k.property_type, p.id FROM locations p,
                      (SELECT property_type FROM listings WHERE status = 'available' GROUP BY property_type
                       HAVING count(DISTINCT purpose) = 2) k
               WHERE p.depth >= 2 AND NOT EXISTS (SELECT 1 FROM listings x JOIN locations y ON y.id = x.location_id
                     WHERE x.status = 'available' AND x.property_type = k.property_type AND p.id = ANY(y.path))
               LIMIT 1""").fetchone()
    if not found:
        pytest.skip("no kind sold and rented with a place lacking it, in this snapshot")
    kind, place = found
    plan = Plan(preview={"property_types": [kind], "location_id": place, "exclude_listing_ids": []})
    facts = run(run_tools, LeadState(1), plan)
    assert facts.stock_preview["for_sale"] == facts.stock_preview["for_rent"] == 0
    assert sorted(facts.stock_preview["listings_shown_for"]) == ["rent", "sale"]
    assert {r["purpose"] for r in facts.search["results"]} == {"sale", "rent"}
    assert facts.search["buyer_has_not_said_buy_or_rent"]
