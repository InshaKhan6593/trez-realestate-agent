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
