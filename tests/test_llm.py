"""The model client: a call that hangs ends at its deadline, as an LLMError."""

import asyncio

import httpx
import pytest

from agent.llm import LLM, LLMError


def test_a_call_that_hangs_ends_at_the_deadline(monkeypatch):
    # Live run: a provider kept the connection open while sending little; httpx's per-read
    # timeout never fired and the turn was held for 300 s.
    async def hang(self, *args, **kwargs):
        await asyncio.sleep(30)

    monkeypatch.setattr(httpx.AsyncClient, "post", hang)
    llm = LLM(api_key="test", timeout=0.2)

    async def go():
        await llm._chat("some/model", [{"role": "user", "content": "hi"}])

    with pytest.raises(LLMError, match="no answer within 0.2 s"):
        asyncio.run(go())
