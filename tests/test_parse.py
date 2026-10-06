from decimal import Decimal

import pytest

from sync.parse import (
    Unusable,
    location_chain,
    parse_listing,
    parse_price_pkr,
    parse_property_type,
    parse_size_sqyd,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("PKR 7.9 Crore Bath(s) 5", 79_000_000),
        # The price runs into the next field on the page; the number must not.
        ("PKR 11 Crore Bath(s) 4", 110_000_000),
        ("PKR 16.64 Crore Initia", 166_400_000),
        ("PKR 85 Lakh Initial Am", 8_500_000),
        ("PKR 1.35 Lakh Bath(s)", 135_000),
        # An earlier scraper read this as 90.
        ("PKR 90 Thousand Bath(s", 90_000),
        ("PKR 1,250,000", 1_250_000),
        ("Price on request", None),
        (None, None),
    ],
)
def test_price(text, expected):
    assert parse_price_pkr(text) == expected


@pytest.mark.parametrize(
    "value, unit, expected",
    [
        (375, "Sq. Yd", Decimal("375.00")),
        (10, "Marla", Decimal("250.00")),
        (1, "Kanal", Decimal("500.00")),
        (900, "Sq. Ft", Decimal("100.00")),
        (375, "Acre", None),
        (None, "Sq. Yd", None),
    ],
)
def test_size(value, unit, expected):
    assert parse_size_sqyd(value, unit) == expected


def test_property_types():
    assert parse_property_type("Residential Plot") == "plot"
    assert parse_property_type("Upper Portion") == "portion"
    assert parse_property_type("Flat") == "flat"
    assert parse_property_type("Something New") == "other"


def _record(**over):
    rec = {
        "id": "50459886",
        "url": "https://www.zameen.com/Property/malir_cantonment_askari_6_x-50459886-21109-1.html",
        "source": "sales",
        "title": "Corner Brigadier House",
        "description": "West open",
        "facts": {
            "propertyType": "House",
            "purpose": "For Sale",
            "price": {"display": "PKR 8.7 Crore Bath(s) 5"},
            "area": {"value": 375, "unit": "Sq. Yd"},
            "bedrooms": 5,
            "bathrooms": 5,
            "locationHierarchy": ["Karachi", "Cantt", "Malir Cantonment", "Askari 6"],
        },
        "structuredData": [
            {
                "@type": "BreadcrumbList",
                "itemListElement": [
                    {"name": "Zameen", "item": "https://www.zameen.com/"},
                    {"name": "Karachi Houses", "item": "https://www.zameen.com/Houses_Property/Karachi-2-1.html"},
                    {"name": "Cantt Houses", "item": "https://www.zameen.com/Houses_Property/Karachi_Cantt-525-1.html"},
                    {"name": "Malir Cantonment Houses", "item": "https://www.zameen.com/Houses_Property/Karachi_Cantt_Malir_Cantonment-6654-1.html"},
                    {"name": "Askari 6 Houses", "item": "https://www.zameen.com/Houses_Property/Karachi_Cantt_Malir_Cantonment_Askari_6-21109-1.html"},
                    {"name": "House 50459886"},
                ],
            }
        ],
        "downloadedMedia": [
            {"file": "303460756-800x1200.jpeg"},
            {"file": "303460764.jpeg"},                 # old-style thumbnail: dropped
            {"file": "303460770-800x1200.jpeg", "error": "HTTP 404"},
        ],
    }
    rec.update(over)
    return rec


def test_listing_fields():
    listing = parse_listing(_record())
    assert listing.zameen_id == 50459886
    assert listing.purpose == "sale" and listing.price_period is None
    assert listing.price_pkr == 87_000_000
    assert listing.size_sqyd == Decimal("375.00")
    assert listing.location_id == 21109
    assert listing.photos == ("303460756-800x1200.jpeg",)


def test_rent_is_monthly():
    rec = _record(source="rentals")
    rec["facts"] = {**rec["facts"], "purpose": "For Rent", "price": {"display": "PKR 1.35 Lakh"}}
    listing = parse_listing(rec)
    assert (listing.purpose, listing.price_period, listing.price_pkr) == ("rent", "monthly", 135_000)


def test_no_price_is_unusable():
    rec = _record()
    rec["facts"] = {**rec["facts"], "price": {"display": "Call for price"}}
    with pytest.raises(Unusable):
        parse_listing(rec)


@pytest.mark.parametrize(
    "page, expected",
    [
        # Short description: no "Read More", runs straight into the next heading.
        ("Overview Description A Brigadier House Measuring 300 Yards Amenities Main Features x",
         "A Brigadier House Measuring 300 Yards"),
        ("Description Long text here Read More Location & Nearby Map", "Long text here"),
        ("Description Sea facing flat Read More Amenities Main Features", "Sea facing flat"),
    ],
)
def test_full_description_is_read_from_page_text(page, expected):
    listing = parse_listing(_record(rawPageText=page, description="Meta summary only"))
    assert listing.description == expected


def test_description_falls_back_when_page_text_missing():
    assert parse_listing(_record(description="West  open\n")).description == "West open"


def test_details_hash_ignores_price():
    a = parse_listing(_record())
    rec = _record()
    rec["facts"] = {**rec["facts"], "price": {"display": "PKR 9 Crore"}}
    assert parse_listing(rec).content_hash == a.content_hash
    assert parse_listing(_record(title="East Open Brigadier House")).content_hash != a.content_hash


def test_location_chain_uses_zameen_ids_and_strips_type():
    chain = location_chain(_record())
    assert [(l.id, l.name) for l in chain] == [
        (2, "Karachi"), (525, "Cantt"), (6654, "Malir Cantonment"), (21109, "Askari 6"),
    ]
    assert chain[-1].path == (2, 525, 6654, 21109)
    assert chain[-1].parent_id == 6654 and chain[0].parent_id is None
