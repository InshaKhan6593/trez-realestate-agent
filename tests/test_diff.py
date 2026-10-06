"""The sync's decisions, on synthetic data. No database, no network."""

from dataclasses import replace
from datetime import datetime, timezone

from sync.diff import Current, plan_sync
from sync.parse import Listing
from sync.snapshot import Snapshot


def _listing(zid: int, price: int = 50_000_000, title: str = "House") -> Listing:
    return Listing(
        zameen_id=zid, url=f"https://www.zameen.com/Property/x-{zid}-1-1.html", title=title,
        description=None, purpose="sale", price_pkr=price, price_period=None,
        price_text=None, property_type="house", zameen_type="House", size_sqyd=None,
        size_text=None, bedrooms=4, bathrooms=4, location_id=21109, photos=(),
    )


def _current(listing: Listing, status: str = "available", missing_runs: int = 0) -> Current:
    return Current(
        listing_id=listing.zameen_id, price_pkr=listing.price_pkr,
        content_hash=listing.content_hash, details=listing.details(),
        status=status, missing_runs=missing_runs,
    )


def _snap(listings, complete=True) -> Snapshot:
    return Snapshot(
        name="test", scraped_at=datetime(2026, 10, 6, tzinfo=timezone.utc), complete=complete,
        listings={l.zameen_id: l for l in listings}, locations={}, unusable=(),
    )


# Ten listings, so one going missing stays above the 70% short-run guard.
BASE = [_listing(i) for i in range(1, 11)]
DB = {l.zameen_id: _current(l) for l in BASE}


def _types(plan):
    return sorted((e.zameen_id, e.type) for e in plan.events)


def test_unchanged_snapshot_produces_no_events():
    plan = plan_sync(DB, _snap(BASE), 10)
    assert plan.events == [] and plan.missed == {} and len(plan.updates) == 10


def test_new_listing():
    plan = plan_sync(DB, _snap([*BASE, _listing(11)]), 10)
    assert _types(plan) == [(11, "new")]
    assert [l.zameen_id for l in plan.inserts] == [11]


def test_price_change_records_old_and_new():
    snap = _snap([replace(BASE[0], price_pkr=55_000_000), *BASE[1:]])
    plan = plan_sync(DB, snap, 10)
    [event] = plan.events
    assert (event.type, event.old, event.new) == (
        "price_changed", {"price_pkr": 50_000_000}, {"price_pkr": 55_000_000},
    )


def test_details_change_names_the_fields():
    snap = _snap([replace(BASE[0], title="East Open House"), *BASE[1:]])
    [event] = plan_sync(DB, snap, 10).events
    assert event.type == "details_changed"
    assert event.old == {"title": "House"} and event.new == {"title": "East Open House"}


def test_first_absence_is_only_counted():
    plan = plan_sync(DB, _snap(BASE[:9]), 10)
    assert plan.missed == {10: 1} and plan.to_verify == [] and plan.events == []


def test_second_absence_asks_for_verification_never_sold():
    db = {**DB, 10: _current(BASE[9], missing_runs=1)}
    plan = plan_sync(db, _snap(BASE[:9]), 10)
    assert plan.to_verify == [10]
    [event] = plan.events
    assert event.type == "missing_from_portal" and event.new["status"] == "needs_verification"


def test_already_flagged_listing_is_not_flagged_again():
    db = {**DB, 10: _current(BASE[9], status="needs_verification", missing_runs=2)}
    plan = plan_sync(db, _snap(BASE[:9]), 10)
    assert plan.missed == {10: 3} and plan.to_verify == [] and plan.events == []


def test_incomplete_run_counts_no_absences():
    plan = plan_sync(DB, _snap(BASE[:9], complete=False), 10)
    assert plan.missed == {} and plan.absences_skipped == "run incomplete"


def test_short_run_counts_no_absences():
    # 6 of 10 came back: a broken scrape, not four sales.
    plan = plan_sync(DB, _snap(BASE[:6]), 10)
    assert plan.missed == {} and "6 listings vs 10" in plan.absences_skipped


def test_reappearing_listing_is_relisted():
    db = {**DB, 10: _current(BASE[9], status="needs_verification", missing_runs=2)}
    plan = plan_sync(db, _snap(BASE), 10)
    assert _types(plan) == [(10, "relisted")] and plan.reappeared == [10]


def test_agent_statuses_are_left_alone():
    # Sold by the agent but still on Zameen: the sync does not resurrect it,
    # and a sold listing going missing is not "missing".
    db = {**DB, 1: _current(BASE[0], status="sold"), 10: _current(BASE[9], status="rented")}
    plan = plan_sync(db, _snap(BASE[:9]), 10)
    assert plan.events == [] and plan.reappeared == [] and 10 not in plan.missed
