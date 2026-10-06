"""Carry out a Plan with the tools. Returns the facts the reply may use.

Every fact the responder sees comes from here, and every price it may state
is collected in `allowed_prices` for the validator.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from psycopg import AsyncConnection

from .listings import Criteria, get_listing, resolve_listing, search_listings
from .locations import decide, find_location
from .media import media_for_listing
from .planner import LeadState, Plan

MAX_DETAILED = 3


@dataclass
class Facts:
    listings: dict[int, dict] = field(default_factory=dict)       # listing_id -> get_listing()
    search: dict | None = None
    location: dict | None = None        # {"status": "match"|"ask"|"none", "text", "options"}
    unresolved_refs: list[dict] = field(default_factory=list)     # "which one?" candidates
    media: dict | None = None           # {"listing_id", "photo_count", "video_urls"}
    stock_preview: dict | None = None   # {"for_sale": n, "for_rent": n, "asked_location": ...}
    tool_calls: list[dict] = field(default_factory=list)

    @property
    def allowed_prices(self) -> set[int]:
        prices: set[int] = set()
        for l in self.listings.values():
            prices.add(l["price_pkr"])
            prices.update(v for v in (l.get("payment_plan") or {}).values() if isinstance(v, int) and v > 999)
        for r in (self.search or {}).get("results", []):
            prices.add(r["price_pkr"])
        return prices


def _log(facts: Facts, name: str, args: dict, result) -> None:
    facts.tool_calls.append({"tool": name, "args": args, "result": result})


async def run_tools(conn: AsyncConnection, state: LeadState, plan: Plan, burst_text: str) -> Facts:
    facts = Facts()

    # Place: a name the extractor could not map to an id.
    if plan.location_text:
        matches = await find_location(conn, plan.location_text)
        status = decide(matches)
        facts.location = {"status": status, "text": plan.location_text,
                          "options": [{"place_id": m.place_id, "name": m.name, "in": m.label} for m in matches]}
        _log(facts, "find_location", {"text": plan.location_text}, facts.location)
        if status == "match":
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = matches[0].place_id
            # Remember the place by its id from now on.
            plan.slot_updates["location_id"] = {"value": matches[0].place_id, "source": "stated",
                                                "confidence": 1.0}
            plan.clear_slots = [s for s in plan.clear_slots if s != "location_id"]

    # Listings they mean.
    ids: list[int] = []
    for ref in plan.listing_refs:
        if "zameen_id" in ref:
            r = await resolve_listing(conn, state.lead_id, text=str(ref["zameen_id"]))
        elif "history" in ref:
            r = await resolve_listing(conn, state.lead_id, text=ref["history"])
        else:   # the listing being discussed: the one touched most recently
            current = next((k.listing_id for k in state.listings if k.relation != "rejected"), None)
            r = {"match": current, "how": "current", "candidates": []}
        _log(facts, "resolve_listing", ref, {"match": r["match"], "how": r.get("how"),
                                            "candidates": [c["zameen_id"] for c in r["candidates"]]})
        if r["match"]:
            ids.append(r["match"])
        elif r["candidates"]:
            facts.unresolved_refs.append({"ref": ref, "candidates": r["candidates"]})
    # Listings a returning buyer must hear about.
    ids += [m["listing_id"] for m in plan.must if "listing_id" in m]
    for listing_id in list(dict.fromkeys(ids))[:MAX_DETAILED + len(plan.must)]:
        detail = await get_listing(conn, listing_id)
        if detail:
            facts.listings[listing_id] = detail
    if facts.listings:
        _log(facts, "get_listing", {"listing_ids": list(facts.listings)}, "ok")

    # Search.
    if plan.search is not None:
        criteria = Criteria(**{k: v for k, v in plan.search.items() if v not in (None, [])})
        facts.search = await search_listings(conn, criteria)
        _log(facts, "search_listings", plan.search,
             {"stage": facts.search["stage"], "total": facts.search["total"],
              "results": [r["zameen_id"] for r in facts.search["results"]]})

    # Buy or rent not said yet: how much stock is there each way.
    if plan.preview is not None:
        counts = {}
        for purpose in ("sale", "rent"):
            criteria = Criteria(purpose=purpose, **{k: v for k, v in plan.preview.items() if v not in (None, [])})
            result = await search_listings(conn, criteria)
            counts[f"for_{purpose}"] = result["total"] if result["stage"] == "exact" else 0
            counts["asked_location"] = result.get("asked_location")
        facts.stock_preview = counts
        _log(facts, "stock_preview", plan.preview, counts)

    # Media for the one listing they asked about.
    if (plan.want_photos or plan.want_video) and len(facts.listings) >= 1:
        target = ids[0] if ids else next(iter(facts.listings))
        facts.media = await media_for_listing(conn, target)
        _log(facts, "media_for_listing", {"listing_id": target}, facts.media)
    return facts
