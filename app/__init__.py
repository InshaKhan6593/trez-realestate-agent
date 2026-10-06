"""WhatsApp side of the agent: webhook, per-lead inbox, turn worker (ARCHITECTURE.md §5)."""

import asyncio
import sys

# psycopg's async mode cannot run on Windows' default ProactorEventLoop. The
# server runs on Linux; this only keeps local development on Windows working.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
