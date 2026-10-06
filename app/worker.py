"""arq worker that answers debounced turns.

    uv run arq app.worker.WorkerSettings
"""

from __future__ import annotations

import logging
import sys

from arq import Retry, func
from arq.connections import RedisSettings
from psycopg_pool import AsyncConnectionPool

from agent.llm import LLM, model_for
from agent.media import send_listing_media

from .config import get_settings
from .reply import make_reply_fn
from .sender import send_text
from .turn import run_turn

log = logging.getLogger(__name__)


async def process_turn(ctx: dict, lead_id: int, seq: int) -> str:
    outcome = await run_turn(
        ctx["redis"], ctx["db"], ctx["settings"], lead_id, seq,
        reply_fn=ctx["reply_fn"], send_fn=send_text, media_fn=send_listing_media,
    )
    if outcome == "locked":
        # Another turn for this lead is still running; try again shortly.
        raise Retry(defer=1)
    log.info("lead %s seq %s: %s", lead_id, seq, outcome)
    return outcome


async def startup(ctx: dict) -> None:
    settings = get_settings()
    ctx["settings"] = settings
    # Fails at startup, not on a buyer's message, if the key or models are missing.
    for role in ("extractor", "responder"):
        model_for(role)
    ctx["reply_fn"] = make_reply_fn(LLM())
    ctx["db"] = AsyncConnectionPool(
        settings.database_url, kwargs={"autocommit": True}, min_size=1, max_size=5, open=False
    )
    await ctx["db"].open()


async def shutdown(ctx: dict) -> None:
    await ctx["db"].close()


class WorkerSettings:
    functions = [func(process_turn, max_tries=60)]   # ~1 min of lock retries
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Windows' event loop has no add_signal_handler.
    handle_signals = sys.platform != "win32"
