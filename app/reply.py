"""What the bot says: the agent graph (agent.graph), adapted to app.turn."""

from __future__ import annotations

from psycopg import AsyncConnection

from agent.graph import AgentReply, agent_reply
from agent.llm import LLM


def make_reply_fn(llm: LLM):
    async def reply_fn(conn: AsyncConnection, lead_id: int, turn_id: int,
                       messages: list[dict]) -> AgentReply:
        return await agent_reply(conn, llm, lead_id, turn_id, [m["id"] for m in messages])
    return reply_fn
