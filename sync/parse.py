"""Turn one raw scraped record into typed listing fields. Pure: no I/O.

Everything is re-derived from the raw strings the scraper kept. The scraper's
own parsed numbers are not trusted: an earlier version read "PKR 90 Thousand"
as 90, and the mistake was only provable because the source text was kept.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from decimal import Decimal

PRICE_UNITS = {
    "arab": 10**9,
    "crore": 10**7,
    "million": 10**6,
    "lakh": 10**5,
    "thousand": 10**3,
}
_PRICE = re.compile(
    r"PKR\s*([\d,]+(?:\.\d+)?)\s*(arab|crore|million|lakh|thousand)?", re.IGNORECASE
)

# Zameen's units -> square yards. Zameen's marla is 225 sq ft = 25 sq yd.
SQYD_PER_UNIT = {
    "sq. yd": Decimal(1),
    "sq. ft": Decimal(1) / 9,
    "sq. m": Decimal("1.19599"),
    "marla": Decimal(25),
    "kanal": Decimal(500),
}

PROPERTY_TYPES = {
    "house": "house", "farm house": "house", "room": "house",
    "flat": "flat", "penthouse": "flat",
    "residential plot": "plot", "plot file": "plot", "plot form": "plot",
    "agricultural land": "plot", "industrial land": "plot",
    "commercial plot": "commercial", "shop": "commercial", "office": "commercial",
    "warehouse": "commercial", "factory": "commercial", "building": "commercial",
    "upper portion": "portion", "lower portion": "portion",
}

# "Askari 6 Houses" -> "Askari 6": breadcrumb names carry the property type.
_TYPE_SUFFIX = re.compile(
    r"\s+(Houses|Flats|Homes|Residential Plots|Commercial Plots|Plots|Property|Apartments)$",
    re.IGNORECASE,
)
# The listing's description sits between the "Description" heading and the
# next section; "Read More" only appears when the text is long. Snapshots up to
# 2026-10-06 stored the 160-character meta summary for short descriptions, so
# the full text is re-read from the saved page text.
_DESCRIPTION = re.compile(
    r"\bDescription\s+([\s\S]*?)\s+(?:Read More\s+)?(?:Amenities|Location & Nearby)\b",
    re.IGNORECASE,
)
_CRUMB_ID = re.compile(r"-(\d+)-\d+\.html$")
_LISTING_URL = re.compile(r"-(\d+)-(\d+)-\d+\.html$")


def parse_price_pkr(text: str | None) -> int | None:
    """'PKR 7.9 Crore Bath(s) 5' -> 79_000_000. 'PKR 90 Thousand' -> 90_000."""
    m = _PRICE.search(text or "")
    if not m:
        return None
    amount = Decimal(m.group(1).replace(",", ""))
    unit = (m.group(2) or "").lower()
    value = int(amount * PRICE_UNITS.get(unit, 1))
    return value if value > 0 else None


def parse_size_sqyd(value, unit: str | None) -> Decimal | None:
    factor = SQYD_PER_UNIT.get((unit or "").strip().lower().rstrip("."))
    if factor is None or not value:
        return None
    return (Decimal(str(value)) * factor).quantize(Decimal("0.01"))


def parse_purpose(purpose: str | None, source: str | None) -> str | None:
    p = (purpose or "").lower()
    if "rent" in p:
        return "rent"
    if "sale" in p:
        return "sale"
    return {"rentals": "rent", "sales": "sale"}.get(source or "")


def parse_description(record: dict) -> str | None:
    m = _DESCRIPTION.search(record.get("rawPageText") or "")
    text = m.group(1) if m else record.get("description")
    return re.sub(r"\s+", " ", text).strip() or None if text else None


def parse_property_type(zameen_type: str | None) -> str:
    return PROPERTY_TYPES.get((zameen_type or "").strip().lower(), "other")


def strip_type(name: str) -> str:
    prev = None
    while prev != name:
        prev, name = name, _TYPE_SUFFIX.sub("", name).strip()
    return name


@dataclass(frozen=True)
class Location:
    id: int
    name: str
    parent_id: int | None
    path: tuple[int, ...]

    @property
    def depth(self) -> int:
        return len(self.path) - 1


def location_chain(record: dict) -> list[Location]:
    """Zameen breadcrumb -> [Karachi, Cantt, Malir Cantonment, Askari 6], root first.

    Location ids come from each breadcrumb's URL, so "Askari 6 Houses" and
    "Askari 6 Flats" are the same place (21109). The listing URL's trailing
    id is the listing's own location, appended if the breadcrumb stops short.
    """
    crumbs: list[tuple[int, str]] = []
    for block in record.get("structuredData") or []:
        if not isinstance(block, dict) or block.get("@type") != "BreadcrumbList":
            continue
        for item in block.get("itemListElement") or []:
            m = _CRUMB_ID.search(item.get("item") or "")
            name = strip_type(item.get("name") or "")
            if m and name and int(m.group(1)) not in {c[0] for c in crumbs}:
                crumbs.append((int(m.group(1)), name))

    m = _LISTING_URL.search(record.get("url") or "")
    if m and crumbs and int(m.group(2)) not in {c[0] for c in crumbs}:
        names = [strip_type(n) for n in (record.get("facts") or {}).get("locationHierarchy") or []]
        if names:
            crumbs.append((int(m.group(2)), names[-1]))

    chain, path = [], ()
    for loc_id, name in crumbs:
        parent = path[-1] if path else None
        path = (*path, loc_id)
        chain.append(Location(loc_id, name, parent, path))
    return chain


@dataclass(frozen=True)
class Listing:
    zameen_id: int
    url: str
    title: str
    description: str | None
    purpose: str
    price_pkr: int
    price_period: str | None
    price_text: str | None
    property_type: str
    zameen_type: str | None
    size_sqyd: Decimal | None
    size_text: str | None
    bedrooms: int | None
    bathrooms: int | None
    location_id: int | None
    photos: tuple[str, ...]          # full-size file names in media-store, gallery order

    def details(self) -> dict:
        """The descriptive fields: a change here is a details_changed event."""
        return {
            "title": self.title,
            "description": self.description,
            "purpose": self.purpose,
            "property_type": self.property_type,
            "size_sqyd": str(self.size_sqyd) if self.size_sqyd is not None else None,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "location_id": self.location_id,
        }

    @property
    def content_hash(self) -> str:
        blob = json.dumps(self.details(), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode()).hexdigest()

    def as_row(self) -> dict:
        row = asdict(self)
        row.pop("photos")
        row["content_hash"] = self.content_hash
        return row


class Unusable(ValueError):
    """A record without the facts a listing must have (price, purpose)."""


_FULL_SIZE_FILE = re.compile(r"^\d+-800x1200\.(?:jpe?g|png|webp)$", re.IGNORECASE)


def parse_listing(record: dict) -> Listing:
    facts = record.get("facts") or {}
    price_text = (facts.get("price") or {}).get("display")
    price = parse_price_pkr(price_text)
    purpose = parse_purpose(facts.get("purpose"), record.get("source"))
    if price is None or purpose is None:
        raise Unusable(f"listing {record.get('id')}: no usable price or purpose")

    area = facts.get("area") or {}
    m = _LISTING_URL.search(record.get("url") or "")
    # Only full-size photos that were actually downloaded; 120x90 strip
    # thumbnails from older snapshots are never carried forward.
    photos = tuple(
        media["file"]
        for media in record.get("downloadedMedia") or []
        if not media.get("error") and _FULL_SIZE_FILE.match(media.get("file") or "")
    )
    return Listing(
        zameen_id=int(record["id"]),
        url=record.get("canonicalUrl") or record.get("url"),
        title=(record.get("title") or "").strip(),
        description=parse_description(record),
        purpose=purpose,
        price_pkr=price,
        price_period="monthly" if purpose == "rent" else None,
        price_text=price_text,
        property_type=parse_property_type(facts.get("propertyType")),
        zameen_type=facts.get("propertyType"),
        size_sqyd=parse_size_sqyd(area.get("value"), area.get("unit")),
        size_text=f"{area.get('value')} {area.get('unit')}" if area.get("value") else None,
        bedrooms=facts.get("bedrooms"),
        bathrooms=facts.get("bathrooms"),
        location_id=int(m.group(2)) if m else None,
        photos=photos,
    )
