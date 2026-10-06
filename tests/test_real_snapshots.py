"""The diff on the real 2026-09-03 -> 2026-10-06 Trez snapshots.

Expected values come from an independent comparison made on 2026-10-06 and
spot-checked on zameen.com. Skipped when data/ is absent: it is git-ignored.
"""

from pathlib import Path

import pytest

from sync.diff import Current, plan_sync
from sync.snapshot import load_snapshot

RAW = Path(__file__).resolve().parent.parent / "data" / "raw"
OLD, NEW = RAW / "2026-09-03", RAW / "2026-10-06"

pytestmark = pytest.mark.skipif(
    not (OLD / "listings.json").exists() or not (NEW / "run-summary.json").exists(),
    reason="real snapshots not present (data/ is git-ignored)",
)


@pytest.fixture(scope="module")
def plan():
    old, new = load_snapshot(OLD), load_snapshot(NEW)
    current = {
        zid: Current(
            listing_id=zid, price_pkr=l.price_pkr, content_hash=l.content_hash,
            details=l.details(), status="available", missing_runs=0,
        )
        for zid, l in old.listings.items()
    }
    return plan_sync(current, new, len(old.listings))


def test_new_snapshot_is_complete():
    snap = load_snapshot(NEW)
    assert snap.complete
    assert len(snap.listings) == 74 and snap.unusable == ()


def test_every_photo_is_full_size():
    for listing in load_snapshot(NEW).listings.values():
        assert all(name.endswith("-800x1200.jpeg") for name in listing.photos), listing.zameen_id


def test_new_listings(plan):
    assert len(plan.inserts) == 25


def test_eight_listings_missed_once_and_none_called_sold(plan):
    assert len(plan.missed) == 8
    assert set(plan.missed.values()) == {1}
    assert plan.to_verify == []
    assert not any(e.type == "missing_from_portal" for e in plan.events)


def test_live_listing_missing_from_search_stays_available(plan):
    # 54550059: live on Zameen, by Trez, PKR 10 Crore, but not in the agency search.
    assert plan.missed.get(54550059) == 1
    assert 54550059 not in plan.to_verify


def test_price_changes(plan):
    changes = {
        e.zameen_id: (e.old["price_pkr"], e.new["price_pkr"])
        for e in plan.events if e.type == "price_changed"
    }
    assert changes[54550053] == (102_500_000, 110_000_000)   # confirmed on zameen.com
    assert changes[47398147] == (125_000_000, 135_000_000)
    assert len(changes) == 5
