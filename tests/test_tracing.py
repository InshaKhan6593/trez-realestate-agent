"""Tracing: what a turn looks like in Langfuse.

Spans go to an in-memory exporter instead of a Langfuse server, so these run
offline. The graph test needs local Supabase with listings (skips otherwise).
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import llm as llm_module
from agent import trace
from agent.llm import LLM, Usage
from agent.schemas import Extraction


@pytest.fixture
def spans(monkeypatch):
    """Record traces in memory; -> a function returning ({span name: span}, scores)."""
    exporter = InMemorySpanExporter()
    # Langfuse shares one span pipeline per public key: a fresh key per test
    # gives this test its own exporter instead of the first test's.
    client = Langfuse(public_key=f"pk-test-{uuid.uuid4().hex}", secret_key="sk-test", base_url="http://127.0.0.1:9",
                      span_exporter=exporter, mask=trace.mask)
    monkeypatch.setattr(trace, "_client", client)
    monkeypatch.setattr(trace, "_enabled", True)
    scores: list[tuple] = []
    # Scores go to Langfuse's HTTP API, not to the span exporter: record them here.
    monkeypatch.setattr(trace, "score", lambda name, value, comment=None: scores.append((name, value)))

    def finished():
        client.flush()
        return {s.name: s for s in exporter.get_finished_spans()}, scores
    return finished


def attr(span, key):
    value = span.attributes.get(f"langfuse.observation.{key}")
    return json.loads(value) if isinstance(value, str) and value[:1] in "[{\"" else value


def test_phone_numbers_are_masked_everywhere():
    data = {"to": "923001234567", "text": ["call 03001234567 or +923331234567"], "price": 85000000}
    assert trace.mask(data=data) == {"to": "92300•••4567",
                                     "text": ["call 0300•••4567 or +92333•••4567"], "price": 85000000}


def test_buyer_messages_read_as_a_conversation_and_voice_notes_play():
    msgs = trace.buyer_input([
        {"type": "text", "text": "DHA mein ghar"},
        {"type": "image", "text": "ye wala?"},
        {"type": "audio", "text": None},
        {"type": "audio", "text": "5 crore tak", "audio": (b"OggS....", "audio/ogg")},
    ])
    assert msgs[0] == {"role": "user", "content": "DHA mein ghar"}
    assert msgs[1]["content"] == "[image] ye wala?"
    assert msgs[2]["content"] == "[voice note, not transcribed]"
    text, audio = msgs[3]["content"]
    assert text["text"] == "[voice note] 5 crore tak"
    assert audio["audio"]._content_type == "audio/ogg"


def test_a_model_call_is_a_generation_with_prompt_answer_tokens_and_cost(spans, monkeypatch):
    async def fake_chat(self, model, messages, **extra):
        return {"model": "provider/model-x",
                "choices": [{"message": {"content": '```json\n{"language": "english", "intents": []}\n```'}}],
                "usage": {"prompt_tokens": 1200, "completion_tokens": 80, "cost": 0.00042}}
    monkeypatch.setattr(LLM, "_chat", fake_chat)
    monkeypatch.setattr(llm_module, "model_for", lambda role: "provider/model-x")
    client = LLM(api_key="test")
    prompt = [{"role": "system", "content": "read"}, {"role": "user", "content": "hi"}]

    async def go():
        with trace.turn(7, 70, [{"type": "text", "text": "hi"}]):
            return await client.structured("extractor", prompt, Extraction, Usage())
    asyncio.run(go())

    found, _ = spans()
    gen = found["extract-request"]
    assert attr(gen, "type") == "generation"
    assert attr(gen, "model.name") == "provider/model-x"
    assert attr(gen, "model.parameters")["reasoning"]  # the options it ran with
    assert attr(gen, "input") == prompt
    assert attr(gen, "output") == {"language": "english", "intents": []}      # parsed, fences stripped
    assert attr(gen, "usage_details") == {"input": 1200, "output": 80}
    assert attr(gen, "cost_details") == {"total": 0.00042}
    assert gen.parent.span_id == found["whatsapp-turn"].context.span_id


def test_a_graph_turn_shows_every_step_under_one_trace(spans):
    from tests.test_agent_graph import (
        ASKS_ABOUT_PLOT,
        PLOT,
        ScriptedLLM,
        _loaded,
        listing_id,
    )
    if not _loaded():
        pytest.skip("local Supabase with listings not available")
    import psycopg

    from agent.graph import agent_reply
    from agent.respond import ReplyDraft
    from tests.test_agent_graph import DB_URL, PHONE, _cleanup

    with psycopg.connect(DB_URL, autocommit=True) as conn:
        _cleanup(conn)
        lead = conn.execute("INSERT INTO leads (phone, name) VALUES (%s, 'Ahmed') RETURNING id", (PHONE,)).fetchone()[0]
    lid = listing_id(PLOT)
    # First draft states a price we never gave it; the second is right.
    llm = ScriptedLLM([ASKS_ABOUT_PLOT], [
        ReplyDraft(reply="Ye plot PKR 90 Lakh ka hai.", listing_ids_mentioned=[lid]),
        ReplyDraft(reply="Ye plot PKR 85 Lakh ka hai.", listing_ids_mentioned=[lid]),
    ])

    async def go():
        async with await psycopg.AsyncConnection.connect(DB_URL, autocommit=True) as conn:
            mid = (await (await conn.execute(
                """INSERT INTO messages (lead_id, direction, wa_message_id, type, text, at)
                   VALUES (%s, 'in', 'wamid.trace.' || gen_random_uuid(), 'text', 'installments? photos?', now())
                   RETURNING id""", (lead,))).fetchone())[0]
            turn_id = (await (await conn.execute(
                "INSERT INTO turns (lead_id) VALUES (%s) RETURNING id", (lead,))).fetchone())[0]
            with trace.turn(lead, turn_id, [{"type": "text", "text": "installments? photos?"}]):
                reply = await agent_reply(conn, llm, lead, turn_id, [mid])
                await reply.commit(conn)
    try:
        asyncio.run(go(), loop_factory=asyncio.SelectorEventLoop)
    finally:
        with psycopg.connect(DB_URL, autocommit=True) as conn:
            _cleanup(conn)

    found, _ = spans()
    root = found["whatsapp-turn"].context.span_id
    for name in ("load-context", "plan-turn", "run-tools", "check-reply", "save-memory"):
        assert found[name].parent.span_id == root, name
    # Tools sit under run-tools, with their arguments and results.
    tools = found["run-tools"].context.span_id
    for name in ("resolve-listing", "get-listing", "media-for-listing"):
        assert found[name].parent.span_id == tools and attr(found[name], "type") == "tool"
    assert attr(found["get-listing"], "output")[str(lid)]["price_pkr"] == 8_500_000
    # The planner's decision and what the agent knew before are readable.
    assert attr(found["plan-turn"], "output")["want_photos"] is True
    assert "wants" in attr(found["load-context"], "output")
    # The validator is a guardrail; the last check passed (the first one failed).
    check = found["check-reply"]
    assert attr(check, "type") == "guardrail" and attr(check, "output") == {"passed": True, "problems": []}
    assert attr(found["run-tools"], "output")["prices_the_reply_may_state"]
    assert {"session.id": f"lead-{lead}"}.items() <= dict(found["plan-turn"].attributes).items()


def test_a_whatsapp_burst_is_one_trace_with_what_was_sent(spans):
    from tests import test_whatsapp_flow as flow
    if not flow._reachable():
        pytest.skip("local Postgres/Redis not running")
    from fastapi.testclient import TestClient

    from tests.meta_payloads import text_message
    gen = flow.client.__wrapped__()            # the flow tests' app client, set up and torn down here
    client: TestClient = next(gen)
    try:
        phone = flow._phone()
        for i, text in enumerate(["salam", "DHA mein ghar chahiye"]):
            flow._post(client, text_message(phone, f"wamid.{phone}.{i}", text))
        lead = flow._lead(phone)
        assert flow._turn(lead, 2) == "not_sent"       # dry run
        stored = flow._q("SELECT langfuse_trace_id FROM turns WHERE lead_id = %s", lead)
    finally:
        next(gen, None)

    found, scores = spans()
    root = found["whatsapp-turn"]
    assert attr(root, "input") == [{"role": "user", "content": "salam"},
                                   {"role": "user", "content": "DHA mein ghar chahiye"}]
    assert attr(root, "output") == "received 2 messages"
    send = found["send-reply"]
    assert send.parent.span_id == root.context.span_id
    assert attr(send, "output")["status"] == "not_sent" and "dry run" in attr(send, "output")["error"]
    assert ("outcome", "not_sent") in scores and ("handoff", False) in scores
    # The turn row links to its trace, for the dashboard.
    assert stored == [(format(root.context.trace_id, "032x"),)]
