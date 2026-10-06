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

from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from . import handoff as handoffs
from .context import TurnContext, load_context
from .extract import build_prompt as extract_prompt
from .extract import extract
from .llm import LLM, Usage
from .locations import load_tree, place_choices
from .memory import remember
from .planner import Plan, add_unanswered, apply_scores, plan
from .respond import ReplyDraft, respond
from .respond import build_prompt as respond_prompt
from .runner import Facts, run_tools
from .schemas import Extraction
from .validate import check, template

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
    ext: Extraction
    plan: Plan
    facts: Facts
    prompt: list[dict]
    draft: ReplyDraft
    problems: list[str]
    attempts: int
    text: str
    used_template: bool
    usage: Usage


def _deps(config) -> tuple[AsyncConnection, LLM]:
    c = config["configurable"]
    return c["conn"], c["llm"]


async def n_load(state: TurnState, config) -> TurnState:
    conn, _ = _deps(config)
    ctx = await load_context(conn, state["lead_id"], state["burst_ids"])
    tree = await load_tree(conn)
    rows = await (await conn.cursor(row_factory=dict_row).execute(
        """SELECT l.zameen_id, l.title, l.location_id, l.price_pkr, ll.relation
           FROM lead_listings ll JOIN listings l ON l.id = ll.listing_id
           WHERE ll.lead_id = %s ORDER BY ll.last_at DESC LIMIT 10""", (state["lead_id"],))).fetchall()
    discussed = [{"zameen_id": r["zameen_id"], "title": r["title"], "relation": r["relation"],
                  "area": tree.label(r["location_id"]).split(", ")[0] if r["location_id"] in tree.places else None}
                 for r in rows]
    return {"ctx": ctx, "places": await place_choices(conn), "discussed": discussed,
            "usage": Usage(), "attempts": 0}


async def n_extract(state: TurnState, config) -> TurnState:
    _, llm = _deps(config)
    messages = extract_prompt(state["ctx"], state["places"], state["discussed"])
    return {"ext": await extract(llm, state["usage"], messages)}


async def n_plan(state: TurnState, config) -> TurnState:
    return {"plan": plan(state["ctx"].state, state["ext"])}


async def n_tools(state: TurnState, config) -> TurnState:
    conn, _ = _deps(config)
    p, ctx = state["plan"], state["ctx"]
    facts = await run_tools(conn, ctx.state, p, " ".join(m["text"] or "" for m in ctx.burst))
    if facts.search is not None:
        apply_scores(p, ctx.state, state["ext"], facts.search["total"])
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
    return {"draft": draft, "attempts": state.get("attempts", 0) + 1}


async def n_check(state: TurnState, config) -> TurnState:
    slots = {**state["ctx"].state.slots, **state["plan"].slot_updates}
    buyer_amounts = {int(v["value"]) for k, v in slots.items()
                     if k in ("budget_min", "budget_max") and str(v.get("value", "")).isdigit()}
    return {"problems": check(state["draft"], state["plan"], state["facts"], buyer_amounts)}


def route_after_check(state: TurnState) -> str:
    if not state["problems"]:
        return "finalize"
    return "respond" if state["attempts"] < MAX_ATTEMPTS else "template"


async def n_template(state: TurnState, config) -> TurnState:
    text = template(state["plan"], state["facts"], state["ext"].language)
    return {"text": text, "used_template": True}


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

    async def commit(c: AsyncConnection) -> dict | None:
        await remember(c, lead_id, turn_id, ext=ext, plan=p, facts=facts,
                       mentioned=draft.listing_ids_mentioned if draft else [],
                       asked=asked, validation=validation, usage=s["usage"].calls)
        if not p.handoff:
            return None
        req = await handoffs.request_handoff(c, lead_id, p.handoff["reason"], p.handoff["open_items"])
        return {"phone": req.agent["phone"], "text": req.alert, "handoff_id": req.handoff_id}             if req.agent else None

    media_id = facts.media["listing_id"] if facts.media else None
    return AgentReply(text=s["text"], media_listing_id=media_id, photos=p.want_photos and bool(media_id),
                      video=p.want_video and bool(media_id),
                      audit={"validation": validation, "usage": s["usage"].calls}, _commit=commit)
