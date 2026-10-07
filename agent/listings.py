"""Listing tools: the only source of facts the agent may state. Read-only.

Every listing returned carries `availability`, decided here in code:
  available   status available AND seen on Zameen within FRESH_FOR
  unverified  status available but not seen recently, or needs_verification:
              never say "available"; the agent must confirm (§10)
  sold / rented / on_hold / withdrawn   as the agent marked it
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from psycopg import AsyncConnection
from psycopg.rows import dict_row

from .locations import Tree, load_tree

FRESH_FOR = timedelta(days=7)
SUGGEST = 3


def availability(status: str, last_verified_at: datetime | None, now: datetime) -> str:
    if status != "available":
        return "unverified" if status == "needs_verification" else status
    if last_verified_at is None or now - last_verified_at > FRESH_FOR:
        return "unverified"
    return "available"


_SUMMARY_SQL = """
SELECT l.id, l.zameen_id, l.url, l.title, l.purpose, l.price_pkr, l.price_period,
       l.property_type, l.zameen_type, l.size_sqyd, l.bedrooms, l.bathrooms,
       l.location_id, l.lat, l.lng, l.status, l.last_verified_at,
       (SELECT count(*) FROM listing_media m WHERE m.listing_id = l.id) AS photo_count,
       cardinality(l.video_urls) AS video_count
FROM listings l
"""


def _summary(row: dict, tree: Tree, now: datetime) -> dict:
    return {
        "listing_id": row["id"],
        "zameen_id": row["zameen_id"],
        "title": row["title"],
        "purpose": row["purpose"],
        "price_pkr": row["price_pkr"],
        "price_period": row["price_period"],
        "property_type": row["property_type"],
        "zameen_type": row["zameen_type"],
        "size_sqyd": float(row["size_sqyd"]) if row["size_sqyd"] is not None else None,
        "bedrooms": row["bedrooms"],
        "bathrooms": row["bathrooms"],
        "location_id": row["location_id"],
        "location": tree.label(row["location_id"]) if row["location_id"] in tree.places else None,
        "availability": availability(row["status"], row["last_verified_at"], now),
        "photo_count": row["photo_count"],
        "video_count": row["video_count"],
        "url": row["url"],
    }


# --------------------------------------------------------------------------
# get_listing
# --------------------------------------------------------------------------

async def get_listing(conn: AsyncConnection, listing_id: int) -> dict | None:
    """Everything the agent may say about one listing. Amenities and the
    installment plan are what the agent entered on Zameen: 'the listing says'."""
    tree = await load_tree(conn)
    cur = conn.cursor(row_factory=dict_row)
    row = await (await cur.execute(_SUMMARY_SQL + " WHERE l.id = %s", (listing_id,))).fetchone()
    if row is None:
        return None
    extra = await (await cur.execute(
        """SELECT description, amenities, payment_plan, furnishing_status, completion_status,
                  occupancy_status, ownership_status, zameen_verification, video_urls,
                  zameen_created_at, zameen_updated_at, last_verified_at
           FROM listings WHERE id = %s""",
        (listing_id,),
    )).fetchone()
    now = datetime.now(timezone.utc)
    return {
        **_summary(row, tree, now),
        "description": extra["description"],
        "amenities": extra["amenities"],
        "payment_plan": extra["payment_plan"],
        "furnishing_status": extra["furnishing_status"],
        "completion_status": extra["completion_status"],
        "occupancy_status": extra["occupancy_status"],
        "ownership_status": extra["ownership_status"],
        "zameen_verification": extra["zameen_verification"],
        "video_urls": extra["video_urls"],
        "posted_on_zameen": extra["zameen_created_at"],
        "updated_on_zameen": extra["zameen_updated_at"],
        "last_verified_at": extra["last_verified_at"],
    }


# --------------------------------------------------------------------------
# search_listings
# --------------------------------------------------------------------------

@dataclass
class Criteria:
    purpose: str                                   # sale | rent (required: never defaulted)
    property_types: list[str] = field(default_factory=list)
    location_id: int | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    size_min_sqyd: float | None = None
    size_max_sqyd: float | None = None
    bedrooms_min: int | None = None
    exclude_listing_ids: list[int] = field(default_factory=list)


def _matches(row: dict, c: Criteria) -> bool:
    if row["purpose"] != c.purpose:
        return False
    if c.property_types and row["property_type"] not in c.property_types:
        return False
    if c.budget_min is not None and row["price_pkr"] < c.budget_min:
        return False
    if c.budget_max is not None and row["price_pkr"] > c.budget_max:
        return False
    size = float(row["size_sqyd"]) if row["size_sqyd"] is not None else None
    if c.size_min_sqyd is not None and (size is None or size < c.size_min_sqyd):
        return False
    if c.size_max_sqyd is not None and (size is None or size > c.size_max_sqyd):
        return False
    if c.bedrooms_min is not None and (row["bedrooms"] or 0) < c.bedrooms_min:
        return False
    return row["id"] not in c.exclude_listing_ids


def _fit(row: dict, c: Criteria) -> float:
    """Lower is better: how far from what the buyer described."""
    target = c.budget_max or c.budget_min
    price_gap = abs(row["price_pkr"] - target) / target if target else 0.0
    return price_gap


def _km(a_lat, a_lng, b_lat, b_lng) -> float | None:
    if None in (a_lat, a_lng, b_lat, b_lng):
        return None
    from math import asin, cos, radians, sin, sqrt
    d_lat, d_lng = radians(b_lat - a_lat), radians(b_lng - a_lng)
    h = sin(d_lat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(d_lng / 2) ** 2
    return 6371 * 2 * asin(sqrt(h))


async def search_listings(conn: AsyncConnection, c: Criteria, limit: int = SUGGEST) -> dict:
    """Listings that fit, best first, and what had to be widened to find them.

    1. exact   : in the place asked for (and everything under it)
    2. nearby  : nothing there, so up the tree one level at a time (Sector G ->
                 Askari 5 -> Malir Cantonment -> Cantt -> Karachi); the first
                 level with matches is shown, closest first, each with its
                 distance and the level it came from, so the reply says so
    3. none    : no match at any level (e.g. nothing of that type/budget at all)
    Only listings the agent may call available are suggested.
    """
    if c.purpose not in ("sale", "rent"):
        raise ValueError("purpose is required (sale or rent) and is never assumed")
    tree = await load_tree(conn)
    now = datetime.now(timezone.utc)
    cur = conn.cursor(row_factory=dict_row)
    rows = await (await cur.execute(_SUMMARY_SQL + " WHERE l.status = 'available'")).fetchall()
    fits = [r for r in rows
            if _matches(r, c) and availability(r["status"], r["last_verified_at"], now) == "available"]

    def present(row, distance=None):
        out = _summary(row, tree, now)
        if distance is not None:
            out["distance_km"] = round(distance, 1)
        return out

    asked = tree.places.get(c.location_id) if c.location_id else None
    if asked is None:
        best = sorted(fits, key=lambda r: (_fit(r, c), -r["id"]))
        return {"stage": "exact", "asked_location": None, "total": len(fits),
                "results": [present(r) for r in best[:limit]]}

    inside = [r for r in fits if r["location_id"] in tree.places
              and asked.id in tree.places[r["location_id"]].path]
    if inside:
        best = sorted(inside, key=lambda r: (_fit(r, c), -r["id"]))
        return {"stage": "exact", "asked_location": tree.label(asked.id), "total": len(inside),
                "results": [present(r) for r in best[:limit]]}

    # Nothing there: go up the tree one level at a time; the first level that
    # has matches is shown, closest to the asked place first, with distances.
    def km(r):
        d = _km(asked.lat, asked.lng, r["lat"], r["lng"])
        return d if d is not None else float("inf")

    for levels_up, ancestor_id in enumerate(reversed(asked.path[:-1]), start=1):
        under = [r for r in fits if r["location_id"] in tree.places
                 and ancestor_id in tree.places[r["location_id"]].path]
        if under:
            best = sorted(under, key=lambda r: (km(r), _fit(r, c)))
            return {"stage": "nearby", "asked_location": tree.label(asked.id),
                    "nothing_in_asked_location": True,
                    "widened_to": tree.places[ancestor_id].name, "levels_up": levels_up,
                    "total": len(under),
                    "results": [present(r, km(r) if km(r) != float("inf") else None) for r in best[:limit]]}

    return {"stage": "none", "asked_location": tree.label(asked.id),
            "nothing_in_asked_location": True, "total": 0, "results": []}


# --------------------------------------------------------------------------
# resolve_listing
# --------------------------------------------------------------------------

async def resolve_listing(conn: AsyncConnection, lead_id: int | None, *,
                          text: str | None = None, location_id: int | None = None,
                          property_type: str | None = None) -> dict:
    """Which listing does the buyer mean?

    A Zameen link or number in their message: any number in it that is one of
    our listings' Zameen ids (no URL format is assumed). Otherwise "wo DHA
    wala": this buyer's own listings first, narrowed by what they said.
    -> {"match": listing_id | None, "candidates": [summaries]}; never a guess.
    """
    tree = await load_tree(conn)
    now = datetime.now(timezone.utc)
    cur = conn.cursor(row_factory=dict_row)

    numbers = _numbers(text)
    if numbers:
        rows = await (await cur.execute(_SUMMARY_SQL + " WHERE l.zameen_id = ANY(%s)", (numbers,))).fetchall()
        if rows:
            return {"match": rows[0]["id"] if len(rows) == 1 else None, "how": "zameen_id",
                    "candidates": [_summary(r, tree, now) for r in rows]}

    if lead_id is None:
        return {"match": None, "how": None, "candidates": []}
    rows = await (await cur.execute(
        _SUMMARY_SQL + """ JOIN lead_listings ll ON ll.listing_id = l.id
           WHERE ll.lead_id = %s ORDER BY ll.last_at DESC""",
        (lead_id,),
    )).fetchall()
    if location_id in tree.places:
        rows = [r for r in rows if r["location_id"] in tree.places
                and location_id in tree.places[r["location_id"]].path]
    if property_type:
        rows = [r for r in rows if r["property_type"] == property_type]
    return {"match": rows[0]["id"] if len(rows) == 1 else None, "how": "buyer_history",
            "candidates": [_summary(r, tree, now) for r in rows[:5]]}


def _numbers(text: str | None) -> list[int]:
    return [int(n) for n in re.findall(r"\d{6,}", text or "")]


async def zameen_ids_in(conn: AsyncConnection, text: str | None) -> list[int]:
    """Our listings' Zameen ids that appear in the buyer's own words (a link or
    a listing number), in order. Only ids of our listings count, so a phone
    number or a price never matches; no URL format is assumed."""
    numbers = _numbers(text)
    if not numbers:
        return []
    ours = {r[0] for r in await (await conn.execute(
        "SELECT zameen_id FROM listings WHERE zameen_id = ANY(%s)", (numbers,))).fetchall()}
    return [n for n in dict.fromkeys(numbers) if n in ours]
