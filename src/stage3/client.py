"""Azure OpenAI nano client for the realtime path + the shared usage/cost helpers (Handoff §3/§8).

Raw ``requests`` (matching svkb_pipeline/vision.py and content_safety.py — the venv has no ``openai``
SDK). One bundled chat-completions call per page using structured outputs (``response_format`` =
json_schema) so the result parses deterministically. Retries 429/5xx with exponential backoff + jitter,
honouring ``Retry-After``; a non-retryable 4xx (bad request / auth) raises immediately.

The api KEY lives only in the Connection and is sent as the ``api-key`` header — it is never logged,
returned, or written to disk.
"""

from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass

import requests

from .config import Stage3Config
from .connection import Connection
from .ratelimit import RateLimiter


@dataclass
class LLMResult:
    data: dict           # the parsed structured-output object
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int   # subset of prompt_tokens served from cache (cheaper rate)


class Stage3CallError(RuntimeError):
    """A call that failed after retries (or a permanent 4xx). The page is marked summary_error; the run
    continues (fault isolation, Handoff §9)."""

    def __init__(self, message: str, code: str = "call_failed") -> None:
        super().__init__(message)
        self.code = code


def estimate_request_tokens(messages: list[dict], max_output_tokens: int) -> int:
    """Cheap pre-call token estimate (~4 chars/token) for the rate limiter's TPM window."""
    chars = sum(len(m.get("content") or "") for m in messages if isinstance(m.get("content"), str))
    return chars // 4 + max_output_tokens


def usage_from_response(body: dict) -> tuple[int, int, int]:
    """(prompt_tokens, completion_tokens, cached_tokens) from a chat-completions response."""
    u = body.get("usage") or {}
    prompt = int(u.get("prompt_tokens") or 0)
    completion = int(u.get("completion_tokens") or 0)
    cached = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return prompt, completion, min(cached, prompt)


def estimate_cost_usd(prompt_tokens: int, completion_tokens: int, cached_tokens: int,
                      cfg: Stage3Config, *, batch: bool = False) -> float:
    """USD from configurable rates (Handoff §8). Cached prompt tokens bill at the cheaper cached rate;
    Batch API applies the ~50% discount."""
    uncached = max(0, prompt_tokens - cached_tokens)
    cost = (uncached / 1_000_000) * cfg.input_cost_per_million \
        + (cached_tokens / 1_000_000) * cfg.cached_input_cost_per_million \
        + (completion_tokens / 1_000_000) * cfg.output_cost_per_million
    if batch:
        cost *= cfg.batch_discount
    return cost


_REASONING_HINTS = ("gpt-5", "o1", "o3", "o4-mini")


def is_reasoning_model(cfg: Stage3Config, deployment: str | None) -> bool:
    """Whether to use the reasoning request shape. ``param_style`` forces it; ``auto`` infers from the
    model/deployment name (gpt-5 / o-series), so Stage 3 adapts to whatever the deployment actually is."""
    if cfg.param_style == "reasoning":
        return True
    if cfg.param_style == "chat":
        return False
    name = f"{cfg.model or ''} {deployment or ''}".lower()
    return any(h in name for h in _REASONING_HINTS)


def build_request_body(messages: list[dict], schema: dict, max_output_tokens: int,
                       cfg: Stage3Config, *, reasoning: bool) -> dict:
    """The chat-completions request body — identical shape for realtime and for each Batch JSONL line so
    pricing/caching behave the same in both modes. Reasoning models (gpt-5/o-series) take
    ``max_completion_tokens`` + ``reasoning_effort`` and reject ``temperature``/``seed``; chat models
    (gpt-4.1-nano) take ``max_tokens`` + ``temperature`` + ``seed`` for determinism."""
    body = {"messages": messages, "response_format": {"type": "json_schema", "json_schema": schema}}
    if reasoning:
        body["max_completion_tokens"] = max_output_tokens
        body["reasoning_effort"] = cfg.reasoning_effort
    else:
        body["max_tokens"] = max_output_tokens
        body["temperature"] = cfg.temperature
        body["seed"] = cfg.seed
    return body


def _extract_json_object(text: str) -> str | None:
    """Return the first balanced top-level ``{...}`` object in ``text`` (brace-matched, string-aware), or
    None. A conservative repair for a reply wrapped in stray prose/markdown around otherwise-valid JSON: it
    never edits inside the object, so it cannot fabricate or alter content."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def parse_structured_content(body: dict) -> dict:
    """Extract + JSON-parse the assistant message. Structured outputs return valid JSON; we still guard
    against a stray code fence, and — when the raw reply won't parse — make ONE conservative repair attempt
    (extract the first balanced ``{...}`` object) before declaring ``bad_output``. The repair only rescues
    prose/markdown wrapped around valid JSON; a genuinely malformed object still raises ``bad_output`` (and
    ``complete_json`` then re-requests, since unparseable output is usually a transient sampling glitch)."""
    content = (body["choices"][0]["message"]["content"] or "").strip()
    if content.startswith("```"):
        inner = content[3:]
        inner = inner.split("```", 1)[0]
        content = inner.removeprefix("json").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        candidate = _extract_json_object(content)
        if candidate is not None and candidate != content:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
        raise Stage3CallError(f"unparseable structured output: {exc}", "bad_output") from exc


# Azure flags blocked content with several shapes: the TEXT content-management policy (error.code
# "content_filter" / "content management policy"), and — for VISION inputs — a 400 with error.code
# "content_policy_violation" and message "...content that is not allowed by our content safety system."
# Catch all of them so an image block is quarantined as a decisive safety signal (Handoff §5), not dropped
# as a generic bad_request (which is what mis-logged 2,133 DS-10 CSAM/explicit images on 2026-06-21).
_CONTENT_FILTER_CODES = {"content_filter", "content_policy_violation"}
_CONTENT_FILTER_MARKERS = ("content management policy", "content safety system",
                           "responsibleaipolicyviolation", "content_filter")


def _is_content_filter_400(text: str) -> bool:
    """True when a 4xx body is an Azure content-policy block — the text OR the image (vision) variant."""
    try:
        err = (json.loads(text) or {}).get("error") or {}
    except (json.JSONDecodeError, TypeError):
        blob = (text or "").lower()
        return any(m in blob for m in _CONTENT_FILTER_MARKERS)
    if str(err.get("code") or "").lower() in _CONTENT_FILTER_CODES:
        return True
    if str((err.get("innererror") or {}).get("code") or "").lower() == "responsibleaipolicyviolation":
        return True
    msg = str(err.get("message") or "").lower()
    return any(m in msg for m in _CONTENT_FILTER_MARKERS)


class NanoClient:
    """Realtime Azure OpenAI client. Thread-safe (a fresh requests call per invocation); share one
    instance across the worker pool."""

    def __init__(self, conn: Connection, cfg: Stage3Config, limiter: RateLimiter | None = None) -> None:
        self._conn = conn
        self._cfg = cfg
        self._limiter = limiter or RateLimiter(cfg.requests_per_minute, cfg.tokens_per_minute)

    def complete_json(self, messages: list[dict], schema: dict, max_output_tokens: int) -> LLMResult:
        reasoning = is_reasoning_model(self._cfg, self._conn.deployment)
        body = build_request_body(messages, schema, max_output_tokens, self._cfg, reasoning=reasoning)
        est = estimate_request_tokens(messages, max_output_tokens)
        headers = {"api-key": self._conn.api_key, "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.acquire(est)
            try:
                r = requests.post(self._conn.chat_url, headers=headers, json=body,
                                  timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:        # connection/timeout -> transient
                last = Stage3CallError(f"network error: {exc}", "network")
                self._sleep(attempt, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:  # throttle / server -> retry
                last = Stage3CallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                self._sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:                          # bad request / auth -> permanent
                # Azure's content-management policy blocks the PROMPT on disallowed material with a 400
                # whose error.code is "content_filter". This is the dominant failure on a redaction-heavy
                # evidence corpus, so it gets its own code: a content-filter block is itself a decisive
                # safety signal (the page is auto-escalated to review, not lost as a generic error).
                code = "content_filter" if _is_content_filter_400(r.text) else "bad_request"
                raise Stage3CallError(f"http {r.status_code}: {r.text[:300]}", code)
            resp = r.json()
            # Output-side filtering: a 200 whose choice finish_reason is content_filter (empty content).
            choice = (resp.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "content_filter":
                raise Stage3CallError("response content-filtered (finish_reason=content_filter)",
                                      "content_filter")
            prompt, completion, cached = usage_from_response(resp)
            try:
                data = parse_structured_content(resp)
            except Stage3CallError as exc:                     # bad_output (after repair attempt)
                # A 200 with unparseable JSON is almost always a transient sampling glitch; re-request with
                # fresh sampling within the same retry budget rather than failing the doc. A genuinely
                # pathological reply exhausts retries and surfaces as bad_output for the caller to flag.
                last = exc
                self._sleep(attempt, None)
                continue
            return LLMResult(data, prompt, completion, cached)
        raise last or Stage3CallError("exhausted retries", "throttled")

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(self._cfg.retry_max_seconds, float(retry_after)))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay + random.uniform(0, delay / 2))     # full-ish jitter
