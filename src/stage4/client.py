"""Azure OpenAI gpt-5-mini VISION client for the realtime path (Handoff §4).

Mirrors the Stage-3 ``NanoClient`` and REUSES its pure helpers (request-shape builder, reasoning-model
auto-detect, usage/cost parsing, structured-output parsing, content-filter detection, the ``LLMResult`` /
``Stage3CallError`` types). The only real difference is that the user message carries an IMAGE content
block, so the rate-limiter token estimate accounts for image patches (Handoff §6: a full page bills
~2,490 tokens) instead of just the text length.

One bundled chat/responses call per asset using ``response_format`` = json_schema structured outputs.
Retries 429/5xx with exponential backoff + jitter (honouring ``Retry-After``); a permanent 4xx raises.
A content-filter block (400 ``content_filter`` or a 200 ``finish_reason=content_filter``) raises with code
``content_filter`` — the asset is flagged ``review`` + ``content_filtered`` and logged, not lost (Handoff
§5). The api KEY lives only in the Connection and is sent as the ``api-key`` header — never logged.
"""

from __future__ import annotations

import random
import time

import requests

from ..stage3.connection import Connection
from ..stage3.client import (LLMResult, Stage3CallError, build_request_body, is_reasoning_model,
                             usage_from_response, parse_structured_content, _is_content_filter_400)
from ..stage3.ratelimit import RateLimiter
from .config import Stage4Config

# Re-export under Stage-4 names so callers don't import from stage3 directly.
VisionCallError = Stage3CallError


def estimate_request_tokens(messages: list[dict], max_output_tokens: int,
                            image_tokens: int) -> int:
    """Cheap pre-call token estimate for the rate limiter's TPM window: constant prompt text (~4
    chars/token) + the image patch budget + the output cap."""
    chars = 0
    for m in messages:
        c = m.get("content")
        if isinstance(c, str):
            chars += len(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") == "text":
                    chars += len(part.get("text") or "")
    return chars // 4 + image_tokens + max_output_tokens


class VisionClient:
    """Realtime Azure OpenAI vision client. Thread-safe (a fresh requests call per invocation); share one
    instance across the worker pool."""

    def __init__(self, conn: Connection, cfg: Stage4Config, limiter: RateLimiter | None = None) -> None:
        self._conn = conn
        self._cfg = cfg
        self._limiter = limiter or RateLimiter(cfg.requests_per_minute, cfg.tokens_per_minute)

    def complete_json(self, messages: list[dict], schema: dict, max_output_tokens: int,
                      *, image_tokens: int = 0) -> LLMResult:
        reasoning = is_reasoning_model(self._cfg, self._conn.deployment)
        body = build_request_body(messages, schema, max_output_tokens, self._cfg, reasoning=reasoning)
        est = estimate_request_tokens(messages, max_output_tokens, image_tokens)
        headers = {"api-key": self._conn.api_key, "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.acquire(est)
            try:
                r = requests.post(self._conn.chat_url, headers=headers, json=body,
                                  timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:        # connection/timeout -> transient
                last = VisionCallError(f"network error: {exc}", "network")
                self._sleep(attempt, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:  # throttle / server -> retry
                last = VisionCallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                self._sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:                          # bad request / auth / content-filter
                code = "content_filter" if _is_content_filter_400(r.text) else "bad_request"
                raise VisionCallError(f"http {r.status_code}: {r.text[:300]}", code)
            resp = r.json()
            choice = (resp.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "content_filter":
                raise VisionCallError("response content-filtered (finish_reason=content_filter)",
                                      "content_filter")
            prompt, completion, cached = usage_from_response(resp)
            return LLMResult(parse_structured_content(resp), prompt, completion, cached)
        raise last or VisionCallError("exhausted retries", "throttled")

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(self._cfg.retry_max_seconds, float(retry_after)))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay + random.uniform(0, delay / 2))
