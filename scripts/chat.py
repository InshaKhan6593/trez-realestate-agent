"""Talk to the agent locally, as a buyer, with the real models. Nothing is sent
on WhatsApp: it runs the same agent turn the worker runs, against the local
database, and shows what the agent understood, decided and checked.

    uv run python -m scripts.chat                       # interactive
    uv run python -m scripts.chat "msg one" "msg two"   # scripted
    uv run python -m scripts.chat --phone 929955550001 --reset "..."

Each message is one turn. A test buyer (phone 9299…) is used; --reset starts
them fresh.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import psycopg
from dotenv import load_dotenv

from agent import trace
from agent.graph import agent_reply
from agent.llm import LLM
from app.turn import score_reply

TEST_PREFIX = "9299"


async def reset(conn, phone: str) -> None:
    lead = await (await conn.execute("SELECT id FROM leads WHERE phone = %s", (phone,))).fetchone()
    if lead:
        for table in ("handoffs", "lead_slots", "lead_listings", "open_questions", "messages", "turns"):
            await conn.execute(f"DELETE FROM {table} WHERE lead_id = %s", lead)
        await conn.execute("DELETE FROM leads WHERE id = %s", lead)


async def one_turn(conn, llm: LLM, phone: str, text: str) -> None:
    lead_id = (await (await conn.execute(
        """INSERT INTO leads (phone, name) VALUES (%s, 'Test Buyer')
           ON CONFLICT (phone) DO UPDATE SET last_inbound_at = now() RETURNING id""", (phone,))).fetchone())[0]
    mid = (await (await conn.execute(
        """INSERT INTO messages (lead_id, direction, wa_message_id, type, text, at)
           VALUES (%s, 'in', 'wamid.chat.' || gen_random_uuid(), 'text', %s, now()) RETURNING id""",
        (lead_id, text))).fetchone())[0]
    turn_id = (await (await conn.execute(
        "INSERT INTO turns (lead_id) VALUES (%s) RETURNING id", (lead_id,))).fetchone())[0]
    await conn.execute("UPDATE messages SET turn_id = %s WHERE id = %s", (turn_id, mid))

    started = time.monotonic()
    # Traced like a WhatsApp turn (tagged local-chat), when Langfuse keys are set.
    with trace.turn(lead_id, turn_id, [{"type": "text", "text": text}], tags=["local-chat"]) as root:
        tid = trace.trace_id()
        if tid:
            await conn.execute("UPDATE turns SET langfuse_trace_id = %s WHERE id = %s", (tid, turn_id))
        reply = await agent_reply(conn, llm, lead_id, turn_id, [mid])
        await conn.execute(
            """INSERT INTO messages (lead_id, direction, type, text, delivery_status, turn_id, at)
               VALUES (%s, 'out', 'text', %s, 'not_sent', %s, now())""", (lead_id, reply.text, turn_id))
        await reply.commit(conn)
        root.update(output=reply.text)
        score_reply(reply.audit)
        trace_url = trace.url()
    seconds = time.monotonic() - started
    await conn.execute("UPDATE turns SET status = 'not_sent', reply = %s, latency_ms = %s, finished_at = now() "
                       "WHERE id = %s", (reply.text, int(seconds * 1000), turn_id))

    extracted, plan, tools, validation, usage = await (await conn.execute(
        "SELECT extracted, plan, tool_calls, validation, usage FROM turns WHERE id = %s", (turn_id,))).fetchone()
    cost = sum(c.get("cost") or 0 for c in usage)
    print(f"\n👤 {text}")
    print(f"🤖 {reply.text}")
    print(f"   understood: {[i['type'] for i in extracted['intents']]}"
          f" {[(u['slot'], u['value']) for u in extracted['slot_updates']]}")
    print(f"   decided:    ask={plan['ask']} handoff={plan['handoff'] and plan['handoff']['reason']}"
          f" scores={plan['scores']} must={[m['kind'] for m in plan['must']]}")
    print(f"   tools:      {[t['tool'] for t in tools]}")
    if plan.get("search") is not None:
        s = next((t for t in tools if t["tool"] == "search_listings"), None)
        if s:
            print(f"   search:     {s['result']}")
    print(f"   checked:    {validation}")
    print(f"   media:      {reply.media_listing_id and ('photos' if reply.photos else '') + (' video' if reply.video else '')}")
    if reply.alert:
        print("   ⚠ agent alert:\n      " + reply.alert["text"].replace("\n", "\n      "))
    print(f"   {seconds:.1f}s, ${cost:.5f}, {len(usage)} model calls")
    if trace_url:
        print(f"   trace:      {trace_url}")


async def main() -> int:
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("messages", nargs="*")
    ap.add_argument("--phone", default=TEST_PREFIX + "55550001")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()
    if not args.phone.startswith(TEST_PREFIX):
        sys.exit(f"use a test phone starting with {TEST_PREFIX}")
    llm = LLM()
    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"], autocommit=True) as conn:
        if args.reset:
            await reset(conn, args.phone)
        if args.messages:
            for text in args.messages:
                await one_turn(conn, llm, args.phone, text)
            return 0
        print("Type as a buyer (empty line to quit).")
        while True:
            text = input("\n> ").strip()
            if not text:
                return 0
            await one_turn(conn, llm, args.phone, text)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        sys.exit(asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop))
    finally:
        trace.flush()        # send the traces before exiting
