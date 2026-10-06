"""Load data/raw/<run>/ into parsed listings and a location tree. No network."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .parse import (
    PROPERTY_TYPES,
    Listing,
    Location,
    Unusable,
    location_chain,
    parse_listing,
)


@dataclass(frozen=True)
class Snapshot:
    name: str                          # 'data/raw/2026-10-06T1730'
    scraped_at: datetime
    complete: bool                     # run-summary.json proved the whole search was seen
    listings: dict[int, Listing]       # by zameen_id
    locations: dict[int, Location]
    photo_paths: dict[str, Path]       # file name -> file on disk
    unusable: tuple[str, ...]
    unmapped_types: tuple[str, ...]    # Zameen categories not in PROPERTY_TYPES yet


def load_snapshot(raw_dir: Path) -> Snapshot:
    records = json.loads((raw_dir / "listings.json").read_text("utf-8"))
    if not records:
        raise ValueError(f"{raw_dir}: empty snapshot")

    summary_path = raw_dir / "run-summary.json"
    complete = bool(
        summary_path.exists() and json.loads(summary_path.read_text("utf-8")).get("complete")
    )

    listings: dict[int, Listing] = {}
    locations: dict[int, Location] = {}
    photo_paths: dict[str, Path] = {}
    unusable: list[str] = []
    for record in records:
        try:
            listing = parse_listing(record)
        except Unusable as err:
            unusable.append(str(err))
            continue
        listings[listing.zameen_id] = listing
        for loc in location_chain(record["zameen"]):
            locations[loc.id] = loc
        # Where each photo was saved, as the scraper recorded it.
        for media in record.get("downloadedMedia") or []:
            if media.get("file") and media.get("localFile") and not media.get("error"):
                photo_paths[media["file"]] = (raw_dir / media["localFile"]).resolve()

    unmapped = sorted({
        l.zameen_type for l in listings.values()
        if l.zameen_type and l.zameen_type.lower() not in PROPERTY_TYPES
    })
    return Snapshot(
        name=raw_dir.as_posix(),
        scraped_at=max(datetime.fromisoformat(r["scrapedAt"].replace("Z", "+00:00")) for r in records),
        complete=complete,
        listings=listings,
        locations=locations,
        photo_paths=photo_paths,
        unusable=tuple(unusable),
        unmapped_types=tuple(unmapped),
    )
