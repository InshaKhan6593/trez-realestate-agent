"""LLM calls through OpenRouter (OpenAI-compatible chat completions).

No provider SDK and no model name in code: EXTRACTOR_MODEL / RESPONDER_MODEL
come from .env so models can be tested and swapped without code changes.

Structured output: we ask for the JSON schema (response_format json_schema).
Models that do not support it get the schema in the prompt instead. Either
way the answer is validated with Pydantic, with one repair attempt; nothing
unvalidated reaches the planner.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import TypeVar

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    """Per-call record kept on the turn (turns.usage)."""
    calls: list[dict] = field(default_factory=list)

    def add(self, role: str, model: str, body: dict) -> None:
        u = body.get("usage") or {}
        self.calls.append({
            "role": role, "model": body.get("model", model),
            "prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens"),
            "cost": u.get("cost"),
        })


def options_for(role: str) -> dict:
    """Per-role request options from .env, so they can be tuned per model:
    {ROLE}_REASONING   off (default) | low | medium | high
    {ROLE}_TEMPERATURE a number; default 0 for the extractor (read the same
                       message the same way), unset for the responder.
    Measured with deepseek-v4-flash: reasoning on took 12-21 s per extraction
    (and once misread the message); off took 2-3 s with the same answers."""
    load_dotenv()
    out: dict = {}
    reasoning = os.environ.get(f"{role.upper()}_REASONING", "off").strip().lower()
    out["reasoning"] = {"enabled": False} if reasoning == "off" else {"effort": reasoning}
    temperature = os.environ.get(f"{role.upper()}_TEMPERATURE", "0" if role == "extractor" else "")
    if temperature.strip():
        out["temperature"] = float(temperature)
    return out


def model_for(role: str) -> str:
    load_dotenv()
    name = os.environ.get(f"{role.upper()}_MODEL", "").strip()
    if not name:
        raise LLMError(f"{role.upper()}_MODEL is not set in .env; choose an OpenRouter model id")
    return name


class LLM:
    """Thin async client. One instance per worker; pass `usage` per turn."""

    def __init__(self, api_key: str | None = None, timeout: float = 30.0):
        load_dotenv()
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        if not self.api_key:
            raise LLMError("OPENROUTER_API_KEY is not set in .env")
        self.timeout = timeout

    async def _chat(self, model: str, messages: list[dict], **extra) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "X-Title": "Trez WhatsApp agent"},
                json={"model": model, "messages": messages, "usage": {"include": True}, **extra},
            )
        if resp.status_code >= 400:
            raise LLMError(f"OpenRouter HTTP {resp.status_code}: {resp.text[:400]}")
        body = resp.json()
        if not body.get("choices"):
            raise LLMError(f"OpenRouter returned no choices: {str(body)[:400]}")
        return body

    async def structured(self, role: str, messages: list[dict], schema: type[T], usage: Usage) -> T:
        model = model_for(role)
        opts = options_for(role)
        json_schema = schema.model_json_schema()
        try:
            body = await self._chat(model, messages, **opts, response_format={
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": False, "schema": json_schema},
            })
        except LLMError as err:
            if "response_format" not in str(err) and "json_schema" not in str(err):
                raise
            # The model does not do schema output: put the schema in the prompt.
            messages = [*messages, {"role": "system", "content":
                        "Answer with JSON only, matching this JSON schema:\n" + json.dumps(json_schema)}]
            body = await self._chat(model, messages, **opts)
        usage.add(role, model, body)
        raw = body["choices"][0]["message"].get("content") or ""
        try:
            return schema.model_validate_json(_strip_fences(raw))
        except ValidationError as err:
            repair = [*messages, {"role": "assistant", "content": raw},
                      {"role": "user", "content": f"That JSON is invalid: {err}. Reply with corrected JSON only."}]
            body = await self._chat(model, repair, **opts)
            usage.add(role + "_repair", model, body)
            raw = body["choices"][0]["message"].get("content") or ""
            try:
                return schema.model_validate_json(_strip_fences(raw))
            except ValidationError as err2:
                raise LLMError(f"{role} returned invalid JSON twice: {err2}") from err2


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ``` fences despite instructions."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()
