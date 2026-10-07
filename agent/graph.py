"""One turn of the agent as a LangGraph graph.

    load -> extract -> plan -> tools -> respond -> check --ok--------------> finalize
                                          ^          |--problems, 1st----^
                                          +----------+--problems again--> template -> finalize

The graph state lives for ONE turn only and is thrown away: the buyer's
memory is in Postgres (agent.context loads it, agent.memory stores it), never
in a checkpointer. Sending is done by app.turn, which still checks for a newer
message and for an agent takeover right before anything goes out.
"""

from __future__ import annotations

import asyncio

from dataclasses import asdict, dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from . import handoff as handoffs
from . import trace
from .context import TurnContext, has_urdu_script, load_context, said_in, unheard
from .extract import build_prompt as extract_prompt
from .extract import extract
from .llm import LLM, Usage, readings
from .locations import load_tree, place_choices
from .memory import remember
from .money import CRORE, LAKH, amounts_in, bare_numbers, pkr
from .planner import Plan, add_unanswered, after_tools, apply_scores, plan
from .respond import ReplyDraft, respond
from .respond import build_prompt as respond_prompt
from .runner import Facts, run_tools
from .schemas import Extraction
from .validate import check, prompt_words, template

MAX_ATTEMPTS = 2


@dataclass
class AgentReply:
    """What app.turn sends, then commits. commit() stores the buyer's memory
    and, if needed, opens the handoff: built after the memory is written, so
    the agent's alert includes what this very turn was about."""
    text: str
    media_listing_id: int | None = None
    photos: bool = False
    video: bool = False
    alert: dict | None = None            # set by commit(): {"phone", "text", "handoff_id"}
    audit: dict = field(default_factory=dict)
    _commit: Any = None

    async def commit(self, conn: AsyncConnection) -> None:
        if self._commit:
            self.alert = await self._commit(conn)


class TurnState(TypedDict, total=False):
    lead_id: int
    turn_id: int
    burst_ids: list[int]
    ctx: TurnContext
    places: list[dict]
    discussed: list[dict]
    last_list: list[dict]
    unsaid: list[dict]
    ext: Extraction
    plan: Plan
    facts: Facts
    prompt: list[dict]
    draft: ReplyDraft
    problems: list[str]
    attempts: int
    text: str
    used_template: bool
    template_listing_ids: list[int]
    prompt_words: set[str]
    usage: Usage


def _deps(config) -> tuple[AsyncConnection, LLM]:
    c = config["configurable"]
    return c["conn"], c["llm"]


async def n_load(state: TurnState, config) -> TurnState:
    conn, _ = _deps(config)
    with trace.step("load-context") as obs:
        ctx = await load_context(conn, state["lead_id"], state["burst_ids"])
        tree = await load_tree(conn)
        rows = await (await conn.cursor(row_factory=dict_row).execute(
            """SELECT l.zameen_id, l.title, l.location_id, l.price_pkr, ll.relation
               FROM lead_listings ll JOIN listings l ON l.id = ll.listing_id
               WHERE ll.lead_id = %s ORDER BY ll.last_at DESC LIMIT 10""", (state["lead_id"],))).fetchall()
        discussed = [{"zameen_id": r["zameen_id"], "title": r["title"], "relation": r["relation"],
                      "area": tree.label(r["location_id"]).split(", ")[0] if r["location_id"] in tree.places
                      else None}
                     for r in rows]
        last_list = await _our_last_list(conn, state["lead_id"], state["turn_id"])
        # The short, decisive parts first: the message history is long.
        known = _known_before(ctx)
        history = known.pop("earlier_messages")
        obs.update(output={**known, "our_last_list": last_list, "earlier_messages": history})
    return {"ctx": ctx, "places": await place_choices(conn), "discussed": discussed,
            "last_list": last_list, "usage": Usage(), "attempts": 0}


async def _our_last_list(conn: AsyncConnection, lead_id: int, turn_id: int) -> list[dict]:
    """The listings our most recent reply named, numbered in the order it named
    them, so "pehla wala" / "the second one" can be matched to a listing."""
    row = await (await conn.execute(
        """SELECT plan->'listings_mentioned' FROM turns
           WHERE lead_id = %s AND id <> %s AND jsonb_array_length(coalesce(plan->'listings_mentioned', '[]')) > 0
           ORDER BY id DESC LIMIT 1""", (lead_id, turn_id))).fetchone()
    ids = row[0] if row else []
    if not ids:
        return []
    found = {r["id"]: r for r in await (await conn.cursor(row_factory=dict_row).execute(
        "SELECT id, zameen_id, title, price_pkr FROM listings WHERE id = ANY(%s)", (ids,))).fetchall()}
    return [{"n": n, "listing_id": i, "zameen_id": found[i]["zameen_id"], "title": found[i]["title"],
             "price": pkr(found[i]["price_pkr"])}
            for n, i in enumerate((i for i in ids if i in found), start=1)]


def _known_before(ctx: TurnContext) -> dict:
    """What the agent knew about the buyer before this turn, for the trace."""
    st = ctx.state
    return {
        "buyer": ctx.name, "language": ctx.language, "handoff_state": st.handoff_state,
        "wants": {k: f"{v['value']} ({v['source']})" for k, v in st.slots.items()},
        "open_questions": st.open_questions, "times_asked": st.asked,
        "listings_seen": [{"zameen_id": k.zameen_id, "relation": k.relation, "price_shown": k.price_shown,
                           "price_now": k.price_now, "now": k.availability_now} for k in st.listings],
        "hours_since_last_message": round(st.hours_since_last_message, 1)
        if st.hours_since_last_message is not None else None,
        "earlier_messages": ctx.recent,
    }


def _filled(obj) -> dict:
    """A dataclass as a dict without its empty fields: shorter to read."""
    return {k: v for k, v in asdict(obj).items() if v not in (None, [], {}, False)}


async def n_extract(state: TurnState, config) -> TurnState:
    _, llm = _deps(config)
    ctx = state["ctx"]
    last_list = [{k: v for k, v in x.items() if k != "listing_id"} for x in state.get("last_list") or []]
    messages = extract_prompt(ctx, state["places"], state["discussed"], last_list)
    texts = [m["text"] for m in ctx.burst if m.get("text")]
    # Several readings at once (no extra wait), combined: replaying one live turn, a single
    # reading dropped "plot" (and most details) 2 times in 10.
    got = await asyncio.gather(*[extract(llm, state["usage"], messages) for _ in range(readings())],
                               return_exceptions=True)
    ok = [g for g in got if not isinstance(g, BaseException)]
    if not ok:
        raise got[0]
    ext, kept, unsaid = _combine(ok, texts)
    # Language, where code can tell: Urdu script is read from the letters; a
    # message with no words (voice note, location, image) keeps the language
    # the buyer has been writing in rather than a guess.
    words = [m["text"] for m in ctx.burst if m.get("text") and m.get("type") in ("text", "image", "video", "document")]
    if has_urdu_script(words):
        ext.language = "urdu"
    elif words and ext.language == "urdu":
        # "urdu" is Urdu script; with none of its letters it is Urdu in English letters
        # (live run: "Assalam o alaikum" was answered in Urdu script).
        ext.language = "roman_urdu"
    elif not words and ctx.language:
        ext.language = ctx.language
    if not kept and "search" in {i.type for i in ext.intents} and texts:
        # It says they are searching but reports no detail at all (live run: a message with a
        # place, size, type and budget came back empty): one second look, then the same check.
        retry = [*messages, {"role": "user", "content":
                 "Your `wants` came back empty although you marked a search. Read BUYER_MESSAGES_NOW "
                 "again and fill every field these messages state; leave a field null only if it is not said."}]
        again = await extract(llm, state["usage"], retry)
        again.language = ext.language
        ext = again
        kept, unsaid = _with_evidence(ext, texts)
    ext.slot_updates = kept
    return {"ext": ext, "unsaid": unsaid}


def _combine(readings: list[Extraction], texts: list[str]) -> tuple[Extraction, list, list[dict]]:
    """Readings of the same messages, combined. The one with the most values backed by the
    buyer's words is the base (its intents and signals are used as they are); a value another
    reading found, also backed by their words, fills a field the base left empty. Nothing
    unsaid can come in this way: every value still quotes the buyer."""
    checked = sorted(((r, *_with_evidence(r, texts)) for r in readings), key=lambda x: -len(x[1]))
    base, kept, unsaid = checked[0]
    have = {u.slot for u in kept}
    for _, more, _ in checked[1:]:
        for u in more:
            if u.slot not in have:
                kept.append(u)
                have.add(u.slot)
    return base, kept, [x for x in unsaid if x["slot"] not in have]


def _with_evidence(ext: Extraction, texts: list[str]) -> tuple[list, list[dict]]:
    """Keep the values whose quoted words are in the buyer's messages; the rest were not
    said now (live run: "sale" invented from "jo hai woh dikha dein")."""
    kept, unsaid = [], []
    for u in ext.slot_updates:
        if said_in(u.said, texts):
            kept.append(u)
        else:
            unsaid.append({"slot": u.slot, "value": u.value, "said": u.said, "why": "not in their message"})
    return kept, unsaid


async def n_plan(state: TurnState, config) -> TurnState:
    with trace.step("plan-turn", as_type="chain", input=state["ext"].model_dump(exclude_defaults=True)) as obs:
        p = plan(state["ctx"].state, state["ext"])
        p.ignored_slots += state.get("unsaid", [])
        # A voice note or picture we cannot take in goes to a person, who can.
        kinds = unheard(state["ctx"].burst)
        if kinds:
            add_unanswered(p, state["ctx"].state, [f"the buyer sent a {'voice note' if k == 'audio' else k}"
                                                   " the bot cannot read" for k in dict.fromkeys(kinds)])
        obs.update(output=_filled(p))
    return {"plan": p}


async def n_tools(state: TurnState, config) -> TurnState:
    conn, _ = _deps(config)
    p, ctx = state["plan"], state["ctx"]
    with trace.step("run-tools") as obs:
        facts = await run_tools(conn, ctx.state, p, [m.get("text") or "" for m in ctx.burst],
                                [x["listing_id"] for x in state.get("last_list") or []])
        if facts.search is not None:
            apply_scores(p, ctx.state, state["ext"], facts.search["total"])
        after_tools(p, unclear_listing=bool(facts.unresolved_refs) and not facts.listings,
                    listing_not_found=bool(p.listing_refs) and not facts.listings and not facts.unresolved_refs,
                    unclear_place=facts.location is not None and facts.location["status"] == "ask")
        obs.update(output={"tools": [c["tool"] for c in facts.tool_calls], "scores": p.scores, "ask": p.ask,
                           "prices_the_reply_may_state": sorted(facts.allowed_prices)})
    return {"facts": facts, "plan": p}


async def n_respond(state: TurnState, config) -> TurnState:
    _, llm = _deps(config)
    ctx = state["ctx"]
    messages = respond_prompt(state["plan"], state["facts"], ctx.burst, ctx.recent,
                              state["ext"].language, ctx.name)
    if state.get("problems"):
        messages.append({"role": "user", "content":
                         "Your previous reply was rejected: " + "; ".join(state["problems"])
                         + ". Write it again, fixing exactly these problems."})
    draft = await respond(llm, state["usage"], messages)
    return {"draft": draft, "attempts": state.get("attempts", 0) + 1, "prompt_words": prompt_words(messages)}


async def n_check(state: TurnState, config) -> TurnState:
    slots = {**state["ctx"].state.slots, **state["plan"].slot_updates}
    buyer_amounts = {int(v["value"]) for k, v in slots.items()
                     if k in ("budget_min", "budget_max") and str(v.get("value", "")).isdigit()}
    # The buyer's own amounts may be repeated back (their offer), with or without a
    # unit: a bare number in a property chat is crore or lakh (live run: "8.5 pe de do").
    texts = [m.get("text") or "" for m in state["ctx"].burst]
    buyer_amounts |= {a for t in texts for a in amounts_in(t)}
    offering = set((state["plan"].handoff or {}).get("reasons", [])) & {"negotiation", "ready_to_pay"}
    if offering:   # only when they are making an offer: elsewhere "Askari 5" or "3 bed" are not prices
        buyer_amounts |= {round(n * unit) for t in texts for n in bare_numbers(t) for unit in (CRORE, LAKH)}
    draft = state["draft"]
    with trace.step("check-reply", as_type="guardrail", input=draft.model_dump(),
                    metadata={"attempt": state["attempts"]}) as obs:
        problems = check(draft, state["plan"], state["facts"], buyer_amounts, state.get("prompt_words", set()),
                         handoff_open=state["ctx"].state.handoff_state != "none")
        obs.update(output={"passed": not problems, "problems": problems},
                   level="WARNING" if problems else None,
                   status_message="; ".join(problems)[:500] if problems else None)
    return {"problems": problems}


def route_after_check(state: TurnState) -> str:
    if not state["problems"]:
        return "finalize"
    return "respond" if state["attempts"] < MAX_ATTEMPTS else "template"


async def n_template(state: TurnState, config) -> TurnState:
    p, ctx = state["plan"], state["ctx"]
    with trace.step("use-template", input={"rejected_twice": state["problems"]}) as obs:
        t = template(p, state["facts"], state["ext"].language)
        if t.needs_agent:
            # Nothing safe to say from facts: the agent answers the message itself.
            add_unanswered(p, ctx.state, [m["text"] for m in ctx.burst if m.get("text")] or ["(message)"])
        obs.update(output={"text": t.text, "listings": t.listing_ids, "handed_to_agent": t.needs_agent},
                   level="WARNING", status_message="reply failed the check twice")
    return {"text": t.text, "used_template": True, "template_listing_ids": t.listing_ids}


async def n_finalize(state: TurnState, config) -> TurnState:
    if state.get("used_template"):
        return {}
    draft = state["draft"]
    add_unanswered(state["plan"], state["ctx"].state, draft.unanswered)
    return {"text": draft.reply, "used_template": False}


def build_graph():
    g = StateGraph(TurnState)
    for name, fn in [("load", n_load), ("extract", n_extract), ("plan", n_plan), ("tools", n_tools),
                     ("respond", n_respond), ("check", n_check), ("template", n_template),
                     ("finalize", n_finalize)]:
        g.add_node(name, fn)
    g.add_edge(START, "load")
    g.add_edge("load", "extract")
    g.add_edge("extract", "plan")
    g.add_edge("plan", "tools")
    g.add_edge("tools", "respond")
    g.add_edge("respond", "check")
    g.add_conditional_edges("check", route_after_check,
                            {"finalize": "finalize", "respond": "respond", "template": "template"})
    g.add_edge("template", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


GRAPH = build_graph()


async def agent_reply(conn: AsyncConnection, llm: LLM, lead_id: int, turn_id: int,
                      burst_ids: list[int]) -> AgentReply:
    """Run the graph for one turn. Nothing is written here: AgentReply.commit
    stores the memory and opens any handoff once the reply has been sent."""
    s: TurnState = await GRAPH.ainvoke(
        {"lead_id": lead_id, "turn_id": turn_id, "burst_ids": burst_ids},
        config={"configurable": {"conn": conn, "llm": llm}},
    )
    p, facts, ext = s["plan"], s["facts"], s["ext"]
    draft = s.get("draft")
    asked = bool(p.ask) and not s.get("used_template")
    validation = {"attempts": s.get("attempts", 0), "problems": s.get("problems", []),
                  "used_template": s.get("used_template", False)}

    # What the buyer actually read: the template's listings, not a rejected draft's.
    mentioned = s.get("template_listing_ids", []) if s.get("used_template") else (
        draft.listing_ids_mentioned if draft else [])

    async def commit(c: AsyncConnection) -> dict | None:
        with trace.step("save-memory", input={
                "wants": p.slot_updates, "forget": p.clear_slots, "asked": p.ask if asked else None,
                "listings_mentioned": mentioned, "scores": p.scores}):
            await remember(c, lead_id, turn_id, ext=ext, plan=p, facts=facts, mentioned=mentioned,
                           asked=asked, validation=validation, usage=s["usage"].calls)
        if not p.handoff:
            return None
        with trace.step("open-handoff", input=p.handoff) as obs:
            req = await handoffs.request_handoff(c, lead_id, p.handoff["reason"], p.handoff["open_items"],
                                                 also=p.handoff.get("reasons", [])[1:])
            obs.update(output={"handoff_id": req.handoff_id, "agent": req.agent and req.agent["name"],
                               "alert": req.alert},
                       level=None if req.agent else "WARNING",
                       status_message=None if req.agent else "no active agent to alert")
        if not req.agent:
            return None
        return {"phone": req.agent["phone"], "text": req.alert, "handoff_id": req.handoff_id}

    media_id = facts.media["listing_id"] if facts.media else None
    return AgentReply(text=s["text"], media_listing_id=media_id, photos=p.want_photos and bool(media_id),
                      video=p.want_video and bool(media_id),
                      audit={"validation": validation, "usage": s["usage"].calls, "scores": p.scores,
                             "handoff": p.handoff}, _commit=commit)
