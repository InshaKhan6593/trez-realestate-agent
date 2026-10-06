"""Load one raw snapshot into Supabase: photos to Storage, rows + events to Postgres.

    uv run python -m sync.ingest data/raw/2026-10-06

Snapshots must be ingested in date order, each exactly once. Re-running the
same snapshot is a no-op, and an older one is refused, so an absence can
never be counted twice and history never runs backwards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx
import psycopg
from dotenv import load_dotenv
from PIL import Image
from psycopg.types.json import Jsonb

from .diff import Current, Plan, plan_sync
from .snapshot import Snapshot, load_snapshot

BUCKET = "listing-photos"


class Refused(RuntimeError):
    """The snapshot is older than one already ingested."""


class AlreadyIngested(RuntimeError):
    """Re-running a snapshot is a no-op, not an error."""


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------

def upload_photos(snap: Snapshot, media_dir: Path, url: str, key: str) -> tuple[dict, list[str]]:
    """Upload each photo not yet in the bucket. -> ({file: (w, h)}, missing files)."""
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    wanted = sorted({f for listing in snap.listings.values() for f in listing.photos})
    sizes: dict[str, tuple[int, int]] = {}
    missing: list[str] = []
    with httpx.Client(base_url=f"{url}/storage/v1", headers=headers, timeout=60) as client:
        existing = _list_bucket(client)
        for name in wanted:
            path = media_dir / name
            if not path.is_file():
                missing.append(name)
                continue
            with Image.open(path) as img:
                sizes[name] = img.size
                mime = Image.MIME.get(img.format or "", "image/jpeg")
            if name in existing:
                continue
            resp = client.post(
                f"/object/{BUCKET}/{name}",
                content=path.read_bytes(),
                headers={"Content-Type": mime, "x-upsert": "false"},
            )
            resp.raise_for_status()
    return sizes, missing


def _list_bucket(client: httpx.Client, page: int = 1000) -> set[str]:
    names: set[str] = set()
    offset = 0
    while True:
        resp = client.post(
            f"/object/list/{BUCKET}", json={"prefix": "", "limit": page, "offset": offset}
        )
        resp.raise_for_status()
        batch = resp.json()
        names.update(obj["name"] for obj in batch)
        if len(batch) < page:
            return names
        offset += page


# --------------------------------------------------------------------------
# Postgres
# --------------------------------------------------------------------------

_CURRENT_SQL = """
SELECT zameen_id, id, price_pkr, content_hash, status, missing_runs,
       title, description, purpose, property_type, size_sqyd,
       bedrooms, bathrooms, location_id
FROM listings WHERE zameen_id IS NOT NULL
"""


def load_current(cur) -> dict[int, Current]:
    out = {}
    for (zid, lid, price, chash, status, missing, title, desc, purpose,
         ptype, size, beds, baths, loc) in cur.execute(_CURRENT_SQL).fetchall():
        out[zid] = Current(
            listing_id=lid, price_pkr=price, content_hash=chash, status=status,
            missing_runs=missing,
            details={
                "title": title, "description": desc, "purpose": purpose,
                "property_type": ptype,
                "size_sqyd": str(size) if size is not None else None,
                "bedrooms": beds, "bathrooms": baths, "location_id": loc,
            },
        )
    return out


def check_order(cur, snap: Snapshot) -> None:
    if cur.execute("SELECT 1 FROM ingest_runs WHERE snapshot = %s", (snap.name,)).fetchone():
        raise AlreadyIngested(f"{snap.name} was already ingested; nothing to do")
    latest = cur.execute("SELECT max(scraped_at) FROM ingest_runs").fetchone()[0]
    if latest is not None and snap.scraped_at <= latest:
        raise Refused(
            f"{snap.name} (scraped {snap.scraped_at:%Y-%m-%d %H:%M}) is not newer than "
            f"the last ingested snapshot ({latest:%Y-%m-%d %H:%M}); snapshots go in date order"
        )


_LISTING_COLS = (
    "url, title, description, purpose, price_pkr, price_period, price_text, "
    "property_type, zameen_type, size_sqyd, size_text, bedrooms, bathrooms, "
    "location_id, content_hash"
)
_LISTING_VALS = (
    "%(url)s, %(title)s, %(description)s, %(purpose)s, %(price_pkr)s, %(price_period)s, "
    "%(price_text)s, %(property_type)s, %(zameen_type)s, %(size_sqyd)s, %(size_text)s, "
    "%(bedrooms)s, %(bathrooms)s, %(location_id)s, %(content_hash)s"
)


def write_plan(cur, snap: Snapshot, plan: Plan, sizes: dict) -> int:
    seen = snap.scraped_at
    run_id = cur.execute(
        """INSERT INTO ingest_runs (snapshot, scraped_at, complete, listing_count)
           VALUES (%s, %s, %s, %s) RETURNING id""",
        (snap.name, seen, snap.complete, len(snap.listings)),
    ).fetchone()[0]

    # Parents before children so the foreign key holds.
    for loc in sorted(snap.locations.values(), key=lambda l: l.depth):
        cur.execute(
            """INSERT INTO locations (id, name, parent_id, path, depth)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name,
                 parent_id = EXCLUDED.parent_id, path = EXCLUDED.path, depth = EXCLUDED.depth""",
            (loc.id, loc.name, loc.parent_id, list(loc.path), loc.depth),
        )

    for listing in plan.inserts:
        cur.execute(
            f"""INSERT INTO listings (zameen_id, {_LISTING_COLS}, status, first_seen_at,
                   last_seen_on_portal_at, last_verified_at)
                VALUES (%(zameen_id)s, {_LISTING_VALS}, 'available', %(seen)s, %(seen)s, %(seen)s)""",
            {**listing.as_row(), "seen": seen},
        )

    reappeared = set(plan.reappeared)
    for listing in plan.updates:
        cur.execute(
            f"""UPDATE listings SET ({_LISTING_COLS}) = ({_LISTING_VALS}),
                   last_seen_on_portal_at = %(seen)s, last_verified_at = %(seen)s,
                   missing_runs = 0,
                   status = CASE WHEN %(reappeared)s THEN 'available' ELSE status END,
                   updated_at = now()
                WHERE zameen_id = %(zameen_id)s""",
            {**listing.as_row(), "seen": seen, "reappeared": listing.zameen_id in reappeared},
        )

    to_verify = set(plan.to_verify)
    for zid, runs in plan.missed.items():
        cur.execute(
            """UPDATE listings SET missing_runs = %s, updated_at = now(),
                   status = CASE WHEN %s THEN 'needs_verification' ELSE status END
               WHERE zameen_id = %s""",
            (runs, zid in to_verify, zid),
        )

    ids = dict(cur.execute(
        "SELECT zameen_id, id FROM listings WHERE zameen_id = ANY(%s)",
        ([e.zameen_id for e in plan.events] + list(snap.listings),),
    ).fetchall())

    cur.executemany(
        """INSERT INTO listing_events (listing_id, type, old, new, source, ingest_run_id, at)
           VALUES (%s, %s, %s, %s, 'zameen_scrape', %s, %s)""",
        [(ids[e.zameen_id], e.type, Jsonb(e.old) if e.old else None,
          Jsonb(e.new) if e.new else None, run_id, seen) for e in plan.events],
    )

    # Photos mirror the latest snapshot exactly, in gallery order.
    for zid, listing in snap.listings.items():
        cur.execute("DELETE FROM listing_media WHERE listing_id = %s", (ids[zid],))
        cur.executemany(
            """INSERT INTO listing_media (listing_id, asset_id, seq, storage_path, width, height)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            [(ids[zid], name.split("-")[0], seq, name, *sizes[name])
             for seq, name in enumerate(listing.photos) if name in sizes],
        )

    cur.execute(
        "UPDATE ingest_runs SET summary = %s, finished_at = now() WHERE id = %s",
        (Jsonb(plan.summary()), run_id),
    )
    return run_id


def ingest(raw_dir: Path) -> dict:
    load_dotenv()
    snap = load_snapshot(raw_dir)
    media_dir = raw_dir.parent.parent / "media-store"

    with psycopg.connect(os.environ["DATABASE_URL"]) as conn, conn.cursor() as cur:
        check_order(cur, snap)
        prev = cur.execute(
            "SELECT listing_count FROM ingest_runs WHERE complete ORDER BY scraped_at DESC LIMIT 1"
        ).fetchone()
        plan = plan_sync(load_current(cur), snap, prev[0] if prev else None)

        # Storage first: if the DB write fails afterwards, an orphan photo is harmless.
        sizes, missing = upload_photos(
            snap, media_dir, os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        )
        run_id = write_plan(cur, snap, plan, sizes)
        conn.commit()

    return {
        "run_id": run_id,
        "snapshot": snap.name,
        "complete": snap.complete,
        "listings": len(snap.listings),
        "locations": len(snap.locations),
        "photos": len(sizes),
        "photos_missing_on_disk": len(missing),
        "unusable": list(snap.unusable),
        **plan.summary(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("raw_dir", type=Path)
    args = ap.parse_args()
    try:
        result = ingest(args.raw_dir)
    except AlreadyIngested as err:
        print(err)
        return 0
    except Refused as err:
        print(f"refused: {err}")
        return 1
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
