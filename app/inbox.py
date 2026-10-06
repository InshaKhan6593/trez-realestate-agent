"""Per-lead inbox in Redis: debounce sequence and lock (ARCHITECTURE.md §5, box 2).

Redis only coordinates the next few seconds. Nothing here is the record of
truth: if Redis is wiped, the unanswered messages are still in Postgres and
the next message for that lead picks them all up.

Debounce, without timers:
  every new message      -> seq += 1, queue turn(lead, seq) to run in N seconds
  turn(lead, seq) starts -> if seq moved on, a newer job covers it: exit
So "hi" + "available?" + a link sent within N seconds become one turn.
"""

from __future__ import annotations

import secrets

from redis.asyncio import Redis

# Longest a turn may hold the lock. Kept well above the 8 s reply budget so a
# slow turn is not run twice, but short enough that a crashed worker cannot
# block a buyer for long.
LOCK_TTL_MS = 60_000

_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""


def _seq_key(lead_id: int) -> str:
    return f"inbox:{lead_id}:seq"


def _lock_key(lead_id: int) -> str:
    return f"inbox:{lead_id}:lock"


async def bump_seq(redis: Redis, lead_id: int) -> int:
    return int(await redis.incr(_seq_key(lead_id)))


async def current_seq(redis: Redis, lead_id: int) -> int:
    return int(await redis.get(_seq_key(lead_id)) or 0)


async def acquire_lock(redis: Redis, lead_id: int) -> str | None:
    """-> a token if this worker now owns the lead, else None."""
    token = secrets.token_hex(8)
    ok = await redis.set(_lock_key(lead_id), token, nx=True, px=LOCK_TTL_MS)
    return token if ok else None


async def release_lock(redis: Redis, lead_id: int, token: str) -> None:
    """Delete the lock only if we still own it (it may have expired and moved on)."""
    await redis.eval(_RELEASE, 1, _lock_key(lead_id), token)
