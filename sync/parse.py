"""Map one scraped record to typed listing fields. Pure: no I/O.

The only source is Zameen's own listing object (record["zameen"], i.e.
window.state.property.data): no page text is parsed, so a wording change on
the page cannot silently corrupt a price or a size. The whole object is also
kept (Listing.zameen_data) so nothing the agent entered on Zameen is lost,
including fields not mapped to a column yet.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

# Zameen stores area in square metres; 1 sq yd = 0.83612736 m2 exactly.
SQM_PER_SQYD = Decimal("0.83612736")

# Our search types, from Zameen's category. This is a business mapping, not a
# parser: a category Zameen adds later becomes "other" (its exact name is kept
# in zameen_type) and is reported by the sync so it can be added here.
PROPERTY_TYPES = {
    "house": "house", "farm house": "house", "room": "house",
    "flat": "flat", "penthouse": "flat",
    "upper portion": "portion", "lower portion": "portion",
    "residential plot": "plot", "plot file": "plot", "plot form": "plot",
    "agricultural land": "plot", "industrial land": "plot",
    "commercial plot": "commercial", "shop": "commercial", "office": "commercial",
    "warehouse": "commercial", "factory": "commercial", "building": "commercial",
}
TOP_LEVEL_TYPES = {"plots": "plot", "commercial": "commercial"}


class Unusable(ValueError):
    """A record that cannot become a listing (no Zameen data, no price)."""


@dataclass(frozen=True)
class Location:
    id: int                      # Zameen location id (21109 = Askari 6)
    name: str
    parent_id: int | None
    path: tuple[int, ...]

    @property
    def depth(self) -> int:
        return len(self.path) - 1


@dataclass(frozen=True)
class Photo:
    asset_id: str
    file: str                    # file name in the media store / Storage bucket


@dataclass(frozen=True)
class Listing:
    zameen_id: int
    url: str
    title: str
    description: str | None
    purpose: str                 # sale | rent
    price_pkr: int
    price_period: str | None     # rent only, as Zameen states it (None = not stated)
    property_type: str
    zameen_type: str | None
    size_sqyd: Decimal | None
    bedrooms: int | None
    bathrooms: int | None
    location_id: int | None
    photos: tuple[Photo, ...]
    lat: float | None
    lng: float | None
    geo_exact: bool | None
    amenities: tuple[dict, ...]
    payment_plan: dict | None
    furnishing_status: str | None
    completion_status: str | None
    occupancy_status: str | None
    ownership_status: str | None
    zameen_verification: str | None
    contact_name: str | None
    video_urls: tuple[str, ...]
    zameen_created_at: datetime | None
    zameen_updated_at: datetime | None
    zameen_reactivated_at: datetime | None
    zameen_data: dict = field(compare=False, repr=False)

    def details(self) -> dict:
        """The descriptive fields: a change here is a details_changed event.

        None means not known; the diff treats unknown -> known as enrichment,
        not as a change on Zameen.
        """
        return {
            "title": self.title,
            "description": self.description,
            "purpose": self.purpose,
            "property_type": self.property_type,
            "size_sqyd": str(self.size_sqyd) if self.size_sqyd is not None else None,
            "bedrooms": self.bedrooms,
            "bathrooms": self.bathrooms,
            "location_id": self.location_id,
            "amenities": list(self.amenities) or None,
            "payment_plan": self.payment_plan,
            "furnishing_status": self.furnishing_status,
            "completion_status": self.completion_status,
        }

    @property
    def content_hash(self) -> str:
        blob = json.dumps(self.details(), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def as_row(self) -> dict:
        row = asdict(self)
        row.pop("photos")
        row["amenities"] = list(self.amenities)
        row["video_urls"] = list(self.video_urls)
        row["zameen_data"] = self.zameen_data
        row["content_hash"] = self.content_hash
        return row


# --------------------------------------------------------------------------
# Field helpers
# --------------------------------------------------------------------------

def html_to_text(value) -> str | None:
    """Zameen's text fields can carry HTML. Tags become spaces; entities are
    decoded whatever their case (title-cased text turns &nbsp; into &Nbsp;)."""
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", str(value))
    text = re.sub(r"&([A-Za-z]+);", lambda m: f"&{m.group(1).lower()};", text)
    text = html.unescape(text).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip() or None


def _status(value) -> str | None:
    """Zameen's enums; 'notSpecified' means the agent left it blank."""
    text = str(value or "").strip()
    return None if not text or text.lower().replace("-", "") == "notspecified" else text


def _epoch(value) -> datetime | None:
    return datetime.fromtimestamp(float(value), tz=timezone.utc) if value else None


def _purpose(value) -> str | None:
    value = str(value or "").lower()
    return "rent" if "rent" in value else "sale" if "sale" in value else None


def property_type(z: dict) -> tuple[str, str | None]:
    """-> (our type, Zameen's own name for it)."""
    categories = sorted(z.get("category") or [], key=lambda c: c.get("level", 0))
    if not categories:
        return "other", None
    leaf = categories[-1]
    name = leaf.get("nameSingular") or leaf.get("name")
    mapped = PROPERTY_TYPES.get((name or "").lower())
    if mapped is None:
        mapped = TOP_LEVEL_TYPES.get((categories[0].get("slug") or "").lower(), "other")
    return mapped, name


def location_chain(z: dict) -> list[Location]:
    """Zameen's hierarchy for the listing, root first: Pakistan > Sindh >
    Karachi > Cantt > Malir Cantonment > Askari 6, by Zameen's location ids."""
    chain, path = [], ()
    for loc in sorted(z.get("locations") or [], key=lambda l: l.get("level", 0)):
        loc_id = int(loc["externalID"])
        chain.append(Location(loc_id, html_to_text(loc.get("name")) or str(loc_id),
                              path[-1] if path else None, (*path, loc_id)))
        path = (*path, loc_id)
    return chain


def parse_amenities(z: dict) -> tuple[dict, ...]:
    """Every amenity the agent entered, as Zameen sends it. A checkbox appears
    only when ticked (its value is empty), so it is True; a number or text
    keeps its value; an empty select/text field carries nothing and is skipped.
    Absent never means "no": it means the listing does not say."""
    out = []
    for group in z.get("amenities") or []:
        for item in group.get("amenities") or []:
            raw = str(item.get("value") or "").strip()
            if item.get("format") == "checkbox":
                value = True
            elif raw:
                value = int(raw) if raw.isdigit() else raw
            else:
                continue
            out.append({"group": html_to_text(group.get("text")), "slug": item.get("slug"),
                        "label": html_to_text(item.get("text")), "value": value})
    return tuple(out)


def parse_payment_plan(z: dict) -> dict | None:
    """Every amount set in the installment plan: whatever keys Zameen sends
    under installments / paymentDetails, zero and empty ones dropped."""
    plan = {**(z.get("installments") or {}), **(z.get("paymentDetails") or {})}
    plan = {k: v for k, v in plan.items() if isinstance(v, (int, float)) and v > 0}
    return plan or None


def _photos(z: dict, record: dict) -> tuple[Photo, ...]:
    """Downloaded photos that are in Zameen's own gallery for this listing, in
    the agent's order. Anything else a scraper picked up (a logo, another
    listing's image) is not this property's photo."""
    order = {str(p.get("id")): p.get("orderIndex", i) for i, p in enumerate(z.get("photos") or [])}
    downloaded = [
        m for m in record.get("downloadedMedia") or []
        if not m.get("error") and m.get("file") and str(m.get("assetId")) in order
    ]
    downloaded.sort(key=lambda m: order[str(m["assetId"])])
    return tuple(Photo(str(m["assetId"]), m["file"]) for m in downloaded)


def _video_urls(z: dict) -> tuple[str, ...]:
    urls = []
    for video in z.get("videos") or []:
        if isinstance(video, dict):
            urls.extend(v for v in video.values() if isinstance(v, str) and v.startswith("http"))
    return tuple(dict.fromkeys(urls))


# --------------------------------------------------------------------------

def parse_listing(record: dict) -> Listing:
    z = record.get("zameen")
    if not z:
        raise Unusable(f"listing {record.get('id')}: no Zameen listing data "
                       "(snapshot taken before structured capture; re-scrape)")
    purpose = _purpose(z.get("purpose"))
    price = int(z["price"]) if z.get("price") and not z.get("hidePrice") else None
    if price is None or purpose is None:
        raise Unusable(f"listing {z.get('externalID')}: no usable price or purpose")

    ptype, zameen_type = property_type(z)
    chain = location_chain(z)
    geo = z.get("geography") or {}
    rooms = z.get("rooms")
    return Listing(
        zameen_id=int(z["externalID"]),
        url=record.get("canonicalUrl") or record.get("url"),
        title=html_to_text(z.get("title")) or "",
        description=html_to_text(z.get("description")),
        purpose=purpose,
        price_pkr=price,
        price_period=(str(z["rentFrequency"]).lower() if z.get("rentFrequency") else None)
        if purpose == "rent" else None,
        property_type=ptype,
        zameen_type=zameen_type,
        size_sqyd=(Decimal(str(z["area"])) / SQM_PER_SQYD).quantize(Decimal("0.01"))
        if z.get("area") else None,
        # 0 rooms is a studio only when Zameen says so; otherwise it was not given.
        bedrooms=rooms if rooms else (0 if z.get("isStudio") else None),
        bathrooms=z.get("baths") or None,
        location_id=chain[-1].id if chain else None,
        photos=_photos(z, record),
        lat=geo.get("lat"),
        lng=geo.get("lng"),
        geo_exact=z.get("hasExactGeography"),
        amenities=parse_amenities(z),
        payment_plan=parse_payment_plan(z),
        furnishing_status=_status(z.get("furnishingStatus")),
        completion_status=_status(z.get("completionStatus")),
        occupancy_status=_status(z.get("occupancyStatus")),
        ownership_status=_status(z.get("ownershipStatus")),
        zameen_verification=_status((z.get("verification") or {}).get("status")),
        contact_name=html_to_text(z.get("contactName")),
        video_urls=_video_urls(z),
        zameen_created_at=_epoch(z.get("createdAt")),
        zameen_updated_at=_epoch(z.get("updatedAt")),
        zameen_reactivated_at=_epoch(z.get("reactivatedAt")),
        zameen_data=z,
    )
