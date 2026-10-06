"""Run the webhook server locally.

    uv run python -m app.serve                 # http://127.0.0.1:8000

Same as `uvicorn app.webhook:app`, except that on Windows it forces the
selector event loop psycopg needs; uvicorn picks the Proactor loop there.
On the Linux server, plain `uvicorn app.webhook:app --host 0.0.0.0` is fine.
"""

from __future__ import annotations

import asyncio
import os
import sys

import uvicorn


def main() -> None:
    config = uvicorn.Config(
        "app.webhook:app",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "8000")),
    )
    loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
    asyncio.run(uvicorn.Server(config).serve(), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
