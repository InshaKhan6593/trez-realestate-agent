"""What the bot says. PLACEHOLDER until the turn pipeline (§17 step 3).

It proves the plumbing end to end: one reply per debounced burst, naming how
many messages it covered. It states no listing facts on purpose; facts only
ever come from tools.
"""

from __future__ import annotations


def placeholder_reply(messages: list[dict]) -> str:
    n = len(messages)
    covered = "aap ka message mil gaya" if n == 1 else f"aap ke {n} messages mil gaye"
    return f"Shukriya! {covered}. Hamari team jald jawab degi. (test reply)"
