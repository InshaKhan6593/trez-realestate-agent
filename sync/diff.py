"""Compare a snapshot with what the database holds. Pure: decides, never writes.

Missing from Zameen's search is evidence, not proof. On 2026-10-06 listing
54550059 was live, by Trez, PKR 10 Crore, and still absent from the agency
search. So:

  1st consecutive absence from a complete run  -> counted, stays available
  2nd consecutive absence                      -> needs_verification + event
  incomplete run, or < 70% of the last count   -> absences not counted at all

The sync never sets sold/rented/on_hold: only the agent knows that (§10).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parse import Listing
from .snapshot import Snapshot

MISSES_BEFORE_VERIFICATION = 2
# A run returning under 70% of the last complete count is a broken scrape
# (layout change, block, timeout), not a mass sell-off (§11).
MIN_SHARE_OF_PREVIOUS = 0.7
TRACKED_ON_PORTAL = ("available", "on_hold", "needs_verification")


@dataclass(frozen=True)
class Current:
    """What the database holds for one Zameen listing."""
    listing_id: int
    price_pkr: int
    content_hash: str
    details: dict
    status: str
    missing_runs: int


@dataclass(frozen=True)
class Event:
    zameen_id: int
    type: str
    old: dict | None
    new: dict | None


@dataclass
class Plan:
    inserts: list[Listing] = field(default_factory=list)
    updates: list[Listing] = field(default_factory=list)        # present: refresh fields
    reappeared: list[int] = field(default_factory=list)         # needs_verification -> available
    missed: dict[int, int] = field(default_factory=dict)        # zameen_id -> new missing_runs
    to_verify: list[int] = field(default_factory=list)          # -> needs_verification
    events: list[Event] = field(default_factory=list)
    absences_skipped: str | None = None

    def summary(self) -> dict:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.type] = counts.get(e.type, 0) + 1
        out = {"events": counts, "missing_once": sum(1 for r in self.missed.values() if r == 1)}
        if self.absences_skipped:
            out["absences_skipped"] = self.absences_skipped
        return out


def plan_sync(
    current: dict[int, Current],
    snap: Snapshot,
    previous_complete_count: int | None,
) -> Plan:
    plan = Plan()

    for zid, listing in sorted(snap.listings.items()):
        before = current.get(zid)
        if before is None:
            plan.inserts.append(listing)
            plan.events.append(Event(zid, "new", None, _headline(listing)))
            continue

        plan.updates.append(listing)
        if listing.price_pkr != before.price_pkr:
            plan.events.append(Event(
                zid, "price_changed",
                {"price_pkr": before.price_pkr}, {"price_pkr": listing.price_pkr},
            ))
        if listing.content_hash != before.content_hash:
            new = listing.details()
            changed = sorted(k for k in new if new[k] != before.details.get(k))
            plan.events.append(Event(
                zid, "details_changed",
                {k: before.details.get(k) for k in changed}, {k: new[k] for k in changed},
            ))
        if before.status == "needs_verification":
            plan.reappeared.append(zid)
            plan.events.append(Event(zid, "relisted", {"status": before.status}, {"status": "available"}))

    if not snap.complete:
        plan.absences_skipped = "run incomplete"
    elif previous_complete_count and len(snap.listings) < MIN_SHARE_OF_PREVIOUS * previous_complete_count:
        plan.absences_skipped = (
            f"{len(snap.listings)} listings vs {previous_complete_count} last time"
        )
    else:
        for zid, before in sorted(current.items()):
            if zid in snap.listings or before.status not in TRACKED_ON_PORTAL:
                continue
            runs = before.missing_runs + 1
            plan.missed[zid] = runs
            if runs >= MISSES_BEFORE_VERIFICATION and before.status != "needs_verification":
                plan.to_verify.append(zid)
                plan.events.append(Event(
                    zid, "missing_from_portal",
                    {"status": before.status}, {"status": "needs_verification", "missing_runs": runs},
                ))
    return plan


def _headline(listing: Listing) -> dict:
    return {
        "price_pkr": listing.price_pkr,
        "purpose": listing.purpose,
        "property_type": listing.property_type,
        "size_sqyd": str(listing.size_sqyd) if listing.size_sqyd is not None else None,
        "location_id": listing.location_id,
    }
