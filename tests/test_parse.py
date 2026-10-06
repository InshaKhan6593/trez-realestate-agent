"""Zameen's listing object -> typed listing. Pure: no I/O."""

from decimal import Decimal

import pytest

from sync.parse import (
    Unusable,
    html_to_text,
    location_chain,
    parse_listing,
    property_type,
)


def _zameen(**over):
    """Shaped like window.state.property.data for a real Trez plot (54467403)."""
    z = {
        "externalID": "54467403", "price": 8_500_000, "purpose": "for-sale",
        "title": "120 Sq Yards Plot For Sale On Easy Installments",
        "description": "West open<br /> 4 beds &amp; lawn<br/>Call us &Nbsp;&Nbsp;",
        "area": 100.3352832,                         # m2 = exactly 120 sq yd
        "rooms": 0, "baths": 0, "isStudio": False,
        "category": [
            {"level": 0, "slug": "Plots", "name": "Plots", "nameSingular": "Plot"},
            {"level": 1, "slug": "Residential_Plots", "name": "Residential Plots",
             "nameSingular": "Residential Plot"},
        ],
        "locations": [
            {"externalID": "1521", "level": 0, "name": "Pakistan"},
            {"externalID": "1523", "level": 1, "name": "Sindh"},
            {"externalID": "2", "level": 2, "name": "Karachi"},
            {"externalID": "570", "level": 3, "name": "Gadap Town"},
            {"externalID": "1345", "level": 4, "name": "Memon Goth"},
        ],
        "geography": {"lat": 24.892159, "lng": 67.23263}, "hasExactGeography": True,
        "installments": {"advanceAmount": 1_700_000, "monthlyAmount": 60_000,
                         "remainingInstallments": 36},
        "paymentDetails": {"balloonPaymentAmount": 400_000, "balloonPaymentsCount": 6,
                           "possessionFee": 680_000, "developmentCharges": 0},
        "amenities": [
            {"text": "Main Features", "amenities": [
                {"slug": "built-in-year", "text": "Built in year", "format": "number", "value": "2023"},
                {"slug": "parking-spaces", "text": "Parking Spaces", "format": "number", "value": "2"},
                {"slug": "furnished", "text": "Furnished", "format": "checkbox", "value": ""},
                {"slug": "flooring", "text": "Flooring", "format": "select", "value": ""},
                {"slug": "electricity-backup", "text": "Electricity Backup", "format": "select",
                 "value": "Generator"},
                {"slug": "other-main-features", "text": "Other Main Features", "format": "text",
                 "value": "  "},
            ]},
        ],
        "furnishingStatus": "furnished", "completionStatus": "completed",
        "occupancyStatus": "notSpecified", "verification": {"status": "unverified"},
        "contactName": "Syed Fahad Hasan", "videos": [],
        "photos": [{"id": 303460770, "orderIndex": 1}, {"id": 303460756, "orderIndex": 0}],
        "createdAt": 1790491984, "updatedAt": 1791226825,
        "someFieldZameenAddsLater": {"kept": True},
    }
    z.update(over)
    return z


def _record(**over):
    rec = {
        "id": "54467403",
        "url": "https://www.zameen.com/Property/x-54467403-1345-1.html",
        "canonicalUrl": "https://www.zameen.com/Property/x-54467403-1345-1.html",
        "source": "sales",
        "zameen": _zameen(),
        "downloadedMedia": [
            {"assetId": "303460756", "file": "303460756-800x1200.webp"},
            {"assetId": "303460770", "file": "303460770-800x1200.webp", "error": "HTTP 404"},
        ],
    }
    rec.update(over)
    return rec


def test_fields_come_from_zameen_data():
    listing = parse_listing(_record())
    assert listing.zameen_id == 54467403
    assert (listing.purpose, listing.price_pkr, listing.price_period) == ("sale", 8_500_000, None)
    assert listing.size_sqyd == Decimal("120.00")            # m2 converted exactly
    assert (listing.property_type, listing.zameen_type) == ("plot", "Residential Plot")
    assert listing.location_id == 1345
    assert (listing.lat, listing.lng, listing.geo_exact) == (24.892159, 67.23263, True)
    assert listing.contact_name == "Syed Fahad Hasan"
    assert listing.zameen_updated_at.isoformat().startswith("2026-")


def test_whole_zameen_object_is_kept():
    # Nothing the agent entered is lost, even a field we do not map yet.
    assert parse_listing(_record()).zameen_data["someFieldZameenAddsLater"] == {"kept": True}


def test_description_html_and_entities_in_any_case():
    assert parse_listing(_record()).description == "West open 4 beds & lawn Call us"
    assert html_to_text("A<br>B &AMP; C") == "A B & C"
    assert html_to_text(None) is None


def test_plot_has_no_rooms_but_a_studio_has_zero():
    assert parse_listing(_record()).bedrooms is None
    assert parse_listing(_record(zameen=_zameen(isStudio=True))).bedrooms == 0
    assert parse_listing(_record(zameen=_zameen(rooms=5, baths=6))).bathrooms == 6


def test_only_ticked_or_filled_amenities():
    amenities = {a["slug"]: a["value"] for a in parse_listing(_record()).amenities}
    assert amenities == {"built-in-year": 2023, "parking-spaces": 2, "furnished": True,
                         "electricity-backup": "Generator"}   # empty select/text dropped


def test_payment_plan_keeps_every_amount_set():
    plan = parse_listing(_record()).payment_plan
    assert plan == {"advanceAmount": 1_700_000, "monthlyAmount": 60_000, "remainingInstallments": 36,
                    "balloonPaymentAmount": 400_000, "balloonPaymentsCount": 6, "possessionFee": 680_000}
    # A key Zameen adds later is carried, not dropped.
    z = _zameen(paymentDetails={"newZameenFee": 5000})
    assert parse_listing(_record(zameen=z)).payment_plan["newZameenFee"] == 5000
    assert parse_listing(_record(zameen=_zameen(installments=None, paymentDetails=None))).payment_plan is None


def test_not_specified_statuses_are_unknown():
    listing = parse_listing(_record())
    assert listing.furnishing_status == "furnished" and listing.occupancy_status is None


def test_rent_frequency_is_never_assumed():
    rent = _zameen(purpose="for-rent", price=135_000)
    assert parse_listing(_record(source="rentals", zameen=rent)).price_period is None
    yearly = _zameen(purpose="for-rent", price=900_000, rentFrequency="Yearly")
    assert parse_listing(_record(source="rentals", zameen=yearly)).price_period == "yearly"


def test_only_downloaded_photos_in_gallery_order():
    photos = parse_listing(_record()).photos
    assert [(p.asset_id, p.file) for p in photos] == [("303460756", "303460756-800x1200.webp")]


def test_a_picture_not_in_zameens_gallery_is_not_a_listing_photo():
    rec = _record(downloadedMedia=[
        {"assetId": "999", "file": "999-800x1200.webp"},                   # a logo, say
        {"assetId": "303460770", "file": "303460770-800x1200.webp"},
        {"assetId": "303460756", "file": "303460756-800x1200.webp"},
    ])
    assert [p.asset_id for p in parse_listing(rec).photos] == ["303460756", "303460770"]
    no_gallery = _record(zameen=_zameen(photos=[]),
                         downloadedMedia=[{"assetId": "999", "file": "999-800x1200.webp"}])
    assert parse_listing(no_gallery).photos == ()


def test_unusable_records():
    with pytest.raises(Unusable, match="no Zameen listing data"):
        parse_listing({"id": "1", "url": "x"})                     # pre-structured snapshot
    with pytest.raises(Unusable):
        parse_listing(_record(zameen=_zameen(hidePrice=True)))       # price on request


def test_location_chain_root_first_by_zameen_ids():
    chain = location_chain(_zameen())
    assert [(l.id, l.name) for l in chain] == [
        (1521, "Pakistan"), (1523, "Sindh"), (2, "Karachi"), (570, "Gadap Town"), (1345, "Memon Goth")]
    assert chain[-1].path == (1521, 1523, 2, 570, 1345) and chain[-1].parent_id == 570
    assert chain[0].parent_id is None


def test_unknown_category_falls_back_without_breaking():
    z = _zameen(category=[{"level": 0, "slug": "Commercial"},
                          {"level": 1, "nameSingular": "Co-working Space"}])
    assert property_type(z) == ("commercial", "Co-working Space")
    z = _zameen(category=[{"level": 0, "slug": "Homes"}, {"level": 1, "nameSingular": "Tiny Home"}])
    assert property_type(z) == ("other", "Tiny Home")


def test_details_hash_ignores_price_but_not_title():
    a = parse_listing(_record())
    assert parse_listing(_record(zameen=_zameen(price=9_000_000))).content_hash == a.content_hash
    assert parse_listing(_record(zameen=_zameen(title="Corner Plot"))).content_hash != a.content_hash
