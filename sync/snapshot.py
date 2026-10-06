"""Load data/raw/<date>/ into parsed listings and a location tree. No network."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .parse import Listing, Location, Unusable, location_chain, parse_listing


@dataclass(frozen=True)
class Snapshot:
    name: str                          # 'data/raw/2026-10-06'
    scraped_at: datetime
    complete: bool                     # run-summary.json proved the whole search was seen
    listings: dict[int, Listing]       # by zameen_id
    locations: dict[int, Location]
    unusable: tuple[str, ...]


def load_snapshot(raw_dir: Path) -> Snapshot:
    records = json.loads((raw_dir / "listings.json").read_text("utf-8"))
    if not records:
        raise ValueError(f"{raw_dir}: empty snapshot")

    # No run-summary.json (snapshots older than 2026-10-06) means no proof
    # the run was complete, so its absences will not be counted.
    summary_path = raw_dir / "run-summary.json"
    complete = bool(
        summary_path.exists() and json.loads(summary_path.read_text("utf-8")).get("complete")
    )

    listings: dict[int, Listing] = {}
    unusable: list[str] = []
    locations: dict[int, Location] = {}
    for record in records:
        try:
            listing = parse_listing(record)
        except Unusable as err:
            unusable.append(str(err))
            continue
        listings[listing.zameen_id] = listing
        for loc in location_chain(record):
            # Keep the deepest path seen for a node.
            if loc.id not in locations or len(loc.path) > len(locations[loc.id].path):
                locations[loc.id] = loc

    return Snapshot(
        name=raw_dir.as_posix(),
        scraped_at=max(datetime.fromisoformat(r["scrapedAt"].replace("Z", "+00:00")) for r in records),
        complete=complete,
        listings=listings,
        locations=locations,
        unusable=tuple(unusable),
    )
