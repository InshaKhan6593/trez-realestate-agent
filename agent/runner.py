"""Carry out a Plan with the tools. Returns the facts the reply may use.

Every fact the responder sees comes from here, and every price it may state
is collected in `allowed_prices` for the validator.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from psycopg import AsyncConnection

from . import trace
from .listings import Criteria, get_listing, resolve_listing, search_listings, zameen_ids_in
from .locations import decide, find_location, load_tree, place_choices, places_named
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
    def candidates(self) -> list[dict]:
        """Listings offered as 'which one do you mean?': real rows, shown to the responder."""
        return [c for u in self.unresolved_refs for c in u["candidates"]]

    @property
    def allowed_prices(self) -> set[int]:
        prices: set[int] = set()
        for l in self.listings.values():
            prices.add(l["price_pkr"])
            prices.update(v for v in (l.get("payment_plan") or {}).values() if isinstance(v, int) and v > 999)
        for r in [*(self.search or {}).get("results", []), *self.candidates]:
            prices.add(r["price_pkr"])
        return prices

    def availability(self) -> dict[int, str]:
        """listing_id -> availability, for every listing the responder was shown."""
        out = {c["listing_id"]: c["availability"] for c in self.candidates}
        out.update({r["listing_id"]: r["availability"] for r in (self.search or {}).get("results", [])})
        out.update({l["listing_id"]: l["availability"] for l in self.listings.values()})
        return out


@contextmanager
def _tool(facts: Facts, name: str, args) -> Iterator[dict]:
    """Run one tool: kept on the turn (turns.tool_calls) and shown in the
    trace as a `tool` with its arguments and result. Set call["result"]."""
    call = {"tool": name, "args": args, "result": None}
    with trace.step(name.replace("_", "-"), as_type="tool", input=args) as obs:
        yield call
        obs.update(output=call["result"])
    facts.tool_calls.append(call)


async def run_tools(conn: AsyncConnection, state: LeadState, plan: Plan, texts: list[str] = ()) -> Facts:
    """`texts`: the buyer's messages this turn. A link to one of our listings in
    them is found here, in code: the model's reading of it is not relied on."""
    facts = Facts()

    linked = await zameen_ids_in(conn, " ".join(t for t in texts if t))
    if linked:
        named = {r["zameen_id"] for r in plan.listing_refs if "zameen_id" in r}
        # The link says which listing; a vague "this one" alongside it means the link.
        plan.listing_refs = [*({"zameen_id": z} for z in linked if z not in named),
                             *(r for r in plan.listing_refs if "zameen_id" in r)]

    # Place the model picked by id, checked against the buyer's own words and
    # today's places: words that fit several ("Emaar" = two towers) are asked
    # about; words that name one place outright overrule a different pick;
    # words that name none ("Askari V", "malir cantt") leave the model's reading.
    if plan.place_words and "location_id" in plan.slot_updates:
        chosen = plan.slot_updates["location_id"]["value"]
        with _tool(facts, "check_place", {"words": plan.place_words, "chosen": chosen}) as call:
            tree, offered = await load_tree(conn), {c["id"] for c in await place_choices(conn)}
            named = places_named(tree, plan.place_words, offered)
            inside = len(named) == 1 and chosen in tree.places and named[0] in tree.places[chosen].path
            verdict = ("ask" if len(named) > 1 else "keep" if not named or inside else "use_named")
            call["result"] = {"places_named": [tree.places[i].name for i in named], "verdict": verdict}
        if verdict == "ask":
            facts.location = {"status": "ask", "text": plan.place_words, "options": _options(tree, named)}
            del plan.slot_updates["location_id"]
            plan.search = plan.preview = None
        elif verdict == "use_named":
            plan.slot_updates["location_id"]["value"] = named[0]
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = named[0]

    # Place: a name the extractor could not map to an id.
    if plan.location_text:
        with _tool(facts, "find_location", {"text": plan.location_text}) as call:
            tree, offered = await load_tree(conn), {c["id"] for c in await place_choices(conn)}
            named = places_named(tree, plan.location_text, offered)
            if named:           # their words name our place(s) outright
                status, options = ("match" if len(named) == 1 else "ask"), _options(tree, named)
            else:               # a typo, perhaps: the conservative similarity backup
                matches = await find_location(conn, plan.location_text)
                status = decide(matches)
                options = [{"place_id": m.place_id, "name": m.name, "in": m.label} for m in matches]
            facts.location = call["result"] = {"status": status, "text": plan.location_text, "options": options}
        if status == "match":
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = options[0]["place_id"]
            # Remember the place by its id from now on.
            plan.slot_updates["location_id"] = {"value": options[0]["place_id"], "source": "stated",
                                                "confidence": 1.0}
            plan.clear_slots = [s for s in plan.clear_slots if s != "location_id"]
        elif status == "ask":
            # Their words fit several places: ask which, rather than show stock
            # from anywhere as if it answered them.
            plan.search = plan.preview = None

    # Listings they mean.
    ids: list[int] = []
    for ref in plan.listing_refs:
        with _tool(facts, "resolve_listing", ref) as call:
            if "zameen_id" in ref:
                r = await resolve_listing(conn, state.lead_id, text=str(ref["zameen_id"]))
            elif "history" in ref:
                r = await resolve_listing(conn, state.lead_id, text=ref["history"])
            else:   # the listing being discussed: the one touched most recently
                current = next((k.listing_id for k in state.listings if k.relation != "rejected"), None)
                r = {"match": current, "how": "current", "candidates": []}
            call["result"] = {"match": r["match"], "how": r.get("how"),
                              "candidates": [_plain(c) for c in r["candidates"]] if not r["match"]
                              else [c["zameen_id"] for c in r["candidates"]]}
        if r["match"]:
            ids.append(r["match"])
        elif r["candidates"]:
            facts.unresolved_refs.append({"ref": ref, "candidates": r["candidates"]})
    # Listings a returning buyer must hear about.
    ids += [m["listing_id"] for m in plan.must if "listing_id" in m]
    wanted = list(dict.fromkeys(ids))[:MAX_DETAILED + len(plan.must)]
    if wanted:
        with _tool(facts, "get_listing", {"listing_ids": wanted}) as call:
            for listing_id in wanted:
                detail = await get_listing(conn, listing_id)
                if detail:
                    facts.listings[listing_id] = detail
            # The trace shows every fact the reply was allowed to use, as the responder sees it.
            call["result"] = {lid: _plain(d) for lid, d in facts.listings.items()} or "not found"

    # Search.
    if plan.search is not None:
        with _tool(facts, "search_listings", plan.search) as call:
            criteria = Criteria(**{k: v for k, v in plan.search.items() if v not in (None, [])})
            facts.search = await search_listings(conn, criteria)
            call["result"] = {k: facts.search.get(k) for k in
                              ("stage", "total", "widened_to", "levels_up", "asked_location") if k in facts.search}
            call["result"]["results"] = [r["zameen_id"] for r in facts.search["results"]]

    # Buy or rent not said yet: how much stock is there each way.
    if plan.preview is not None:
        with _tool(facts, "stock_preview", plan.preview) as call:
            counts = {}
            for purpose in ("sale", "rent"):
                criteria = Criteria(purpose=purpose, **{k: v for k, v in plan.preview.items() if v not in (None, [])})
                result = await search_listings(conn, criteria)
                counts[f"for_{purpose}"] = result["total"] if result["stage"] == "exact" else 0
                counts["asked_location"] = result.get("asked_location")
            facts.stock_preview = call["result"] = counts

    # Media for the one listing they asked about.
    if (plan.want_photos or plan.want_video) and len(facts.listings) >= 1:
        target = ids[0] if ids else next(iter(facts.listings))
        with _tool(facts, "media_for_listing", {"listing_id": target}) as call:
            facts.media = call["result"] = await media_for_listing(conn, target)
    return facts


def _options(tree, place_ids: list[int]) -> list[dict]:
    return [{"place_id": i, "name": tree.places[i].name, "in": tree.label(i)} for i in place_ids]


def _plain(listing: dict) -> dict:
    """A listing's facts, all of them, as plain JSON (kept on the turn and shown
    in the trace). Empty fields are left out; dates become text."""
    return {k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in listing.items() if v not in (None, [], {}, "")}
