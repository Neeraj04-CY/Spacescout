"""Thin async client for OpenAI-compatible chat-completions APIs (Groq by default).

Responsibilities: timeouts, bounded retries, structured-output mode, JSON
decoding, and returning call metadata for tracing. It knows nothing about
coworking; business logic lives in the parser/explainer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.config import Settings

log = logging.getLogger("spacescout.llm")


class LLMError(Exception):
    """Base class for LLM failures. Callers fall back to deterministic code."""


class LLMUnavailable(LLMError):
    pass


class LLMBadOutput(LLMError):
    pass


@dataclass
class CallMeta:
    purpose: str
    model: str
    attempts: int = 0
    latency_ms: float = 0.0
    mode: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    outcome: str = "pending"
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self._transport = transport  # injectable for tests
        # Circuit breaker: after the provider rate-limits us, skip LLM calls until this
        # monotonic time, so users get the rule-based fallback instantly instead of
        # waiting on retries that will also be refused.
        self._cooldown_until = 0.0

    @property
    def enabled(self) -> bool:
        return self.s.llm_enabled

    async def chat_json(
        self,
        *,
        purpose: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        schema_name: str,
        max_tokens: int = 1200,
        max_attempts: int = 2,
    ) -> tuple[dict[str, Any], CallMeta]:
        """Return the model's JSON object and call metadata, or raise LLMError."""
        meta = CallMeta(purpose=purpose, model=self.s.llm_model)
        if not self.enabled:
            meta.outcome = "skipped: no API key"
            raise LLMUnavailable("LLM_API_KEY not set")

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            meta.outcome = f"skipped: provider rate limit, cooling down {remaining:.0f}s"
            raise LLMUnavailable(f"rate-limited, cooling down {remaining:.0f}s")

        strict = self.s.llm_strict_schema
        started = time.perf_counter()
        last_err: Exception | None = None
        async with httpx.AsyncClient(
            base_url=self.s.llm_base_url,
            timeout=self.s.llm_timeout_s,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self.s.llm_api_key}"},
        ) as client:
            while meta.attempts < max_attempts:
                meta.attempts += 1
                body: dict[str, Any] = {
                    "model": self.s.llm_model,
                    "messages": messages,
                    "temperature": 0,
                    "max_completion_tokens": max_tokens,
                }
                if strict:
                    meta.mode = "json_schema(strict)"
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
                    }
                else:
                    meta.mode = "json_object"
                    body["response_format"] = {"type": "json_object"}
                if self.s.llm_reasoning_effort and self.s.llm_model.startswith("openai/gpt-oss"):
                    body["reasoning_effort"] = self.s.llm_reasoning_effort
                    body["include_reasoning"] = False
                try:
                    resp = await client.post("/chat/completions", json=body)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = e
                    meta.errors.append(f"transport: {type(e).__name__}")
                    await asyncio.sleep(0.4)
                    continue

                if resp.status_code == 400 and strict:
                    # Model/provider rejected strict schema mode: degrade to JSON mode once.
                    meta.errors.append(f"400 in strict mode: {resp.text[:160]}")
                    strict = False
                    continue
                if resp.status_code in (401, 403):
                    meta.outcome = f"auth error {resp.status_code}"
                    meta.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                    raise LLMUnavailable(f"authentication failed ({resp.status_code})")
                if resp.status_code in RETRYABLE_STATUS:
                    meta.errors.append(f"http {resp.status_code}")
                    last_err = LLMUnavailable(f"http {resp.status_code}")
                    ra = resp.headers.get("retry-after", "")
                    retry_after = float(ra) if ra.replace(".", "", 1).isdigit() else None
                    if resp.status_code == 429 and retry_after is not None and retry_after > 2.0:
                        # Waiting would stall the user: open the breaker and fall back now.
                        self._cooldown_until = time.monotonic() + retry_after
                        break
                    await asyncio.sleep(min(retry_after, 2.0) if retry_after is not None else 0.6)
                    continue
                if resp.status_code >= 400:
                    meta.errors.append(f"http {resp.status_code}: {resp.text[:160]}")
                    last_err = LLMUnavailable(f"http {resp.status_code}")
                    break

                data = resp.json()
                usage = data.get("usage") or {}
                meta.prompt_tokens = usage.get("prompt_tokens")
                meta.completion_tokens = usage.get("completion_tokens")
                try:
                    content = data["choices"][0]["message"]["content"] or ""
                    parsed = json.loads(content)
                    if not isinstance(parsed, dict):
                        raise ValueError("top-level JSON is not an object")
                except (KeyError, IndexError, ValueError) as e:
                    meta.errors.append(f"bad json: {e}")
                    last_err = LLMBadOutput(str(e))
                    continue
                meta.outcome = "ok"
                meta.latency_ms = round((time.perf_counter() - started) * 1000, 1)
                log.info("llm_call", extra={"llm": meta.as_dict()})
                return parsed, meta

        if isinstance(last_err, LLMUnavailable) and "429" in str(last_err) and self._cooldown_until < time.monotonic():
            self._cooldown_until = time.monotonic() + 10.0
        meta.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        meta.outcome = f"failed: {last_err}"
        log.warning("llm_call_failed", extra={"llm": meta.as_dict()})
        if isinstance(last_err, LLMError):
            raise last_err
        raise LLMUnavailable(str(last_err))
