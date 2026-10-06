"""Real Trez snapshots, read from Zameen's structured data.

Snapshots are immutable, so these facts are fixed. Skipped when data/ is
absent (it is git-ignored).
"""

import json
from pathlib import Path

import pytest

from sync.diff import Current, plan_sync
from sync.snapshot import load_snapshot

DATA = Path(__file__).resolve().parent.parent / "data"
SNAP = DATA / "raw" / "2026-10-06T1730"
ARCHIVED = DATA / "archive" / "raw-before-structured-capture" / "2026-10-06"

pytestmark = pytest.mark.skipif(
    not (SNAP / "run-summary.json").exists(), reason="real snapshots not present (data/ is git-ignored)"
)


@pytest.fixture(scope="module")
def snap():
    return load_snapshot(SNAP)


def test_complete_and_every_listing_usable(snap):
    assert snap.complete
    assert len(snap.listings) == 74 and snap.unusable == ()


def test_every_listing_matches_a_search_hit(snap):
    hits = json.loads((SNAP / "search-hits.json").read_text("utf-8"))
    assert {int(h["externalID"]) for h in hits} == set(snap.listings)


def test_every_listing_sits_in_the_location_tree(snap):
    for listing in snap.listings.values():
        assert listing.location_id in snap.locations
        path = snap.locations[listing.location_id].path
        assert all(ancestor in snap.locations for ancestor in path)


def test_structured_fields_are_present(snap):
    listings = list(snap.listings.values())
    assert all(l.lat and l.lng for l in listings)
    assert sum(bool(l.amenities) for l in listings) == 69
    assert sum(bool(l.payment_plan) for l in listings) == 15
    assert all(l.zameen_data for l in listings)


def test_installment_plan_of_the_memon_goth_plot(snap):
    plan = snap.listings[54467403].payment_plan
    assert plan["advanceAmount"] == 1_700_000 and plan["monthlyAmount"] == 60_000
    assert plan["remainingInstallments"] == 36 and plan["possessionFee"] == 680_000


def test_photos_match_zameen_gallery(snap):
    for listing in snap.listings.values():
        assert len(listing.photos) == len(listing.zameen_data.get("photos") or [])
        assert all(p.file in snap.photo_paths for p in listing.photos)


def test_same_snapshot_twice_produces_no_events(snap):
    current = {
        zid: Current(listing_id=zid, price_pkr=l.price_pkr, content_hash=l.content_hash,
                     details=l.details(), status="available", missing_runs=0)
        for zid, l in snap.listings.items()
    }
    plan = plan_sync(current, snap, len(snap.listings))
    assert plan.events == [] and plan.missed == {}


@pytest.mark.skipif(not ARCHIVED.exists(), reason="archive not present")
def test_snapshots_without_structured_data_are_refused():
    old = load_snapshot(ARCHIVED)
    assert old.listings == {} and len(old.unusable) == 74
    assert "no Zameen listing data" in old.unusable[0]
