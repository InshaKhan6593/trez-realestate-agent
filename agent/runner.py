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
from .context import has_urdu_script
from .locations import (RESEMBLES, decide, find_location, load_tree, place_choices, places_in_text,
                        places_named, resemblance)
from .media import media_for_listing
from .money import pkr
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

    # A search where the model recorded no place, but the buyer wrote the full
    # name of one of today's places (live run: "Askari 6 villa rate?" searched
    # their earlier area in half the runs): use the place they named.
    if (plan.search is not None or plan.preview is not None) and "location_id" not in plan.slot_updates \
            and not plan.location_text:
        tree, offered = await load_tree(conn), {c["id"] for c in await place_choices(conn)}
        named = places_in_text(tree, " ".join(t for t in texts if t), offered)
        if len(named) == 1:
            with _tool(facts, "place_in_message", {"text": " ".join(t for t in texts if t)}) as call:
                call["result"] = {"place": tree.places[named[0]].name, "place_id": named[0]}
            plan.slot_updates["location_id"] = {"value": named[0], "source": "stated", "confidence": 1.0}
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = named[0]

    # Place the model picked by id, checked against the buyer's own words and
    # today's places: words that fit several ("Emaar" = two towers) are asked
    # about; words that name one place outright overrule a different pick;
    # words that name none ("Askari V", "malir cantt") leave the model's reading.
    if plan.place_words and "location_id" in plan.slot_updates:
        chosen = plan.slot_updates["location_id"]["value"]
        with _tool(facts, "check_place", {"words": plan.place_words, "chosen": chosen}) as call:
            tree, offered = await load_tree(conn), {c["id"] for c in await place_choices(conn)}
            named = places_named(tree, plan.place_words, offered)
            # Their words are the exact name of one place: that place, even when the model
            # picked a part of it (live run: "Askari 5" searched only Sector F / Sector G).
            verdict = ("ask" if len(named) > 1 else "keep" if not named or named[0] == chosen else "use_named")
            likeness = None
            if verdict == "keep" and not named and chosen in tree.places and not has_urdu_script([plan.place_words]):
                # Words that name none of our places: the model's reading is kept only if
                # its place resembles them (a short form, a typo, a numeral). A different
                # place it knows to be nearby is a guess (live run: "Clifton" -> Zamzama).
                likeness = await resemblance(conn, plan.place_words, tree.places[chosen].name)
                if likeness < RESEMBLES:
                    verdict = "not_our_place"
            call["result"] = {"places_named": [tree.places[i].name for i in named], "verdict": verdict,
                              **({"resemblance": round(likeness, 2)} if likeness is not None else {})}
        if verdict == "ask":
            facts.location = {"status": "ask", "text": plan.place_words, "options": _options(tree, named)}
            del plan.slot_updates["location_id"]
            plan.search = plan.preview = None
        elif verdict == "use_named":
            plan.slot_updates["location_id"]["value"] = named[0]
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = named[0]
        elif verdict == "not_our_place":
            # Treated like a place we do not have: looked up, said honestly, nearest shown.
            said = plan.slot_updates.pop("location_id")
            plan.slot_updates["location_text"] = {**said, "value": plan.place_words}
            plan.location_text = plan.place_words
            plan.clear_slots = [*plan.clear_slots, "location_id"]
            for target in (plan.search, plan.preview):
                if target is not None:
                    target["location_id"] = None

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
            if plan.more_options:
                # Said, so "nothing more here" is not read as "nothing here".
                facts.search["already_shown_left_out"] = len(plan.search["exclude_listing_ids"])
            call["result"] = _search_brief(facts.search)

    # Buy or rent not said yet: what matches everything they DID say, each way.
    if plan.preview is not None:
        wanted = {k: v for k, v in plan.preview.items() if k != "show_both"}
        with _tool(facts, "stock_preview", wanted) as call:
            found, searched, asked = {}, {}, None
            for purpose in ("sale", "rent"):
                result = await search_listings(conn, Criteria(
                    purpose=purpose, **{k: v for k, v in wanted.items() if v not in (None, [])}))
                asked = asked or result.get("asked_location")
                searched[purpose] = result
                found[purpose] = result if result["stage"] == "exact" and result["total"] else None
            counts = {"for_sale": found["sale"]["total"] if found["sale"] else 0,
                      "for_rent": found["rent"]["total"] if found["rent"] else 0,
                      "asked_location": asked,
                      # Where these were counted, in words, so a count is never put on the wrong place.
                      "where": asked or ("all of Trez's areas" + (" (the place they named is not one of ours)"
                                                                  if plan.location_text else "")),
                      # What these counts match, so a count is never read as more or less than it is.
                      "counted": _counted(wanted)}
            # Listings, not just counts: when only one way has any (nothing to
            # choose), or both ways, labelled, once they have asked again to see.
            show = [p for p, r in found.items() if r]
            if len(show) == 2 and not plan.preview.get("show_both"):
                show = []
            if show:
                per = {"sale": 3, "rent": 3} if len(show) == 1 else {"sale": 2, "rent": 1}
                facts.search = {"stage": "exact", "asked_location": counts["asked_location"],
                                "total": sum(found[p]["total"] for p in show), "purposes_shown": show,
                                "buyer_has_not_said_buy_or_rent": True,
                                "results": [r for p in show for r in found[p]["results"][:per[p]]]}
            elif not any(found.values()):
                # Nothing that fits in the asked place either way. If only one way has anything
                # near (a wider area, or the closest), show that, as a search would (live run:
                # plots asked after Askari 6 got "agent will contact you" instead of Gadap's plots).
                near = [p for p, r in searched.items() if r["results"]]
                if len(near) == 1:
                    show = near
                    facts.search = {**searched[near[0]], "buyer_has_not_said_buy_or_rent": True}
            counts["listings_shown_for"] = show
            facts.stock_preview = call["result"] = counts

    # Media for the one listing they asked about.
    if (plan.want_photos or plan.want_video) and len(facts.listings) >= 1:
        target = ids[0] if ids else next(iter(facts.listings))
        with _tool(facts, "media_for_listing", {"listing_id": target}) as call:
            facts.media = call["result"] = await media_for_listing(conn, target)
    return facts


def _search_brief(search: dict) -> dict:
    out = {k: search.get(k) for k in ("stage", "total", "widened_to", "levels_up", "asked_location",
                                      "already_shown_left_out", "no_exact_match") if k in search}
    out["results"] = [r["zameen_id"] for r in search["results"]]
    return out


def _counted(criteria: dict) -> dict:
    """The buyer's criteria a count was made with, readable (place: see asked_location)."""
    out = {}
    if criteria.get("property_types"):
        out["property_types"] = criteria["property_types"]
    for k in ("budget_min", "budget_max"):
        if criteria.get(k):
            out[k] = pkr(criteria[k])
    for k in ("size_min_sqyd", "size_max_sqyd", "bedrooms_min"):
        if criteria.get(k):
            out[k] = criteria[k]
    return out or {"anything": "no type, size or budget given"}


def _options(tree, place_ids: list[int]) -> list[dict]:
    return [{"place_id": i, "name": tree.places[i].name, "in": tree.label(i)} for i in place_ids]


def _plain(listing: dict) -> dict:
    """A listing's facts, all of them, as plain JSON (kept on the turn and shown
    in the trace). Empty fields are left out; dates become text."""
    return {k: v.isoformat() if isinstance(v, datetime) else v
            for k, v in listing.items() if v not in (None, [], {}, "")}
