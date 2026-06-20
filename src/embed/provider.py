"""The Azure OpenAI embeddings client + the bulk-run rate limiter (Handoff §6).

The embedding VECTOR is computed server-side and does not depend on this client code (Handoff §6), so —
unlike the chunker — Stage 7 need not reuse ``svkb_pipeline.embeddings`` verbatim for fidelity. We reuse its
**URL / dimensions / api-version / batch=32** conventions (see connection.py) but wrap a Stage 3/4-style
concurrency + RPM/TPM limiter and a deeper retry budget: sv-kb's built-in retry is only 4 attempts, thin for
a multi-hour run over ~1.25M children.

Raw ``requests`` (the venv has no ``openai`` SDK), matching every other stage. Thread-safe — a fresh request
per call — so one instance is shared across the worker pool. The api KEY lives only in the Connection and is
sent as the ``api-key`` header; it is never logged, returned, or written to disk. Implements the
``svkb_pipeline.embeddings.EmbeddingProvider`` protocol shape (``model``/``dimensions``/``embed``) so the
same provider could drive sv-kb code paths directly.
"""

from __future__ import annotations

import random
import time

import requests

from .config import EmbedConfig
from .connection import EmbedConnection
from ..stage3.ratelimit import RateLimiter


class EmbedCallError(RuntimeError):
    """A batch that failed after retries (or a permanent 4xx). The scheduler records it and continues; the
    batch's content_shas are simply not written to the store, so a later re-run retries them (fault
    isolation — one bad batch never aborts a multi-hour run)."""

    def __init__(self, message: str, code: str = "call_failed") -> None:
        super().__init__(message)
        self.code = code


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token), matching ``svkb_pipeline.chunking.estimate_tokens`` so the
    dry-run projection and the live run agree and so cost tracks sv-kb's own budgeting."""
    return max(1, len(text) // 4)


class AzureEmbedProvider:
    """Realtime Azure OpenAI embeddings client with a shared RPM/TPM limiter + exponential backoff."""

    def __init__(self, conn: EmbedConnection, cfg: EmbedConfig, limiter: RateLimiter | None = None) -> None:
        self._conn = conn
        self._cfg = cfg
        self.model = cfg.model
        self.dimensions = cfg.dimensions
        self._limiter = limiter or RateLimiter(cfg.requests_per_minute, cfg.tokens_per_minute)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of strings -> one float vector each, in input order. Raises EmbedCallError on a
        permanent failure or exhausted retries."""
        if not texts:
            return []
        body: dict = {"input": texts}
        if self.dimensions != 1536:                     # send ``dimensions`` only when != 1536 (sv-kb conv.)
            body["dimensions"] = self.dimensions
        est = sum(len(t) for t in texts) // 4
        headers = {"api-key": self._conn.api_key, "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.acquire(est)
            try:
                r = requests.post(self._conn.embeddings_url, headers=headers, json=body,
                                  timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:    # connection/timeout -> transient
                last = EmbedCallError(f"network error: {exc}", "network")
                self._sleep(attempt, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:   # throttle / server -> retry
                last = EmbedCallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                self._sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:                            # bad request / auth -> permanent
                raise EmbedCallError(f"http {r.status_code}: {r.text[:300]}", "bad_request")
            vecs = self._parse(r.json(), len(texts))
            return vecs
        raise last or EmbedCallError("exhausted retries", "throttled")

    def _parse(self, body: dict, n: int) -> list[list[float]]:
        data = body.get("data")
        if not isinstance(data, list) or len(data) != n:
            raise EmbedCallError(f"embeddings response had {len(data) if isinstance(data, list) else 'no'} "
                                 f"vectors for {n} inputs", "bad_output")
        vecs = [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]
        for v in vecs:
            if not isinstance(v, list) or len(v) != self.dimensions:
                raise EmbedCallError(f"vector dimensionality {len(v) if isinstance(v, list) else '?'} "
                                     f"!= configured {self.dimensions}", "bad_output")
        return [[float(x) for x in v] for v in vecs]

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(self._cfg.retry_max_seconds, float(retry_after)))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay + random.uniform(0, delay / 2))     # full-ish jitter
