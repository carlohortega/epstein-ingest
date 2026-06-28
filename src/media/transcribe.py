"""Transcription windowing + the Azure OpenAI ``gpt-4o-mini-transcribe`` client (HANDOFF-media-track §2).

Mirrors the PDF track's Stage-3 network client (``src/stage3/client.py``): raw ``requests`` (the venv has
no ``openai`` SDK), exponential backoff + jitter on 429/5xx honouring ``Retry-After``, permanent 4xx
raises. The api KEY lives only in the Connection and is sent as the ``api-key`` header — never logged or
written to disk.

The model returns **plain text only** (no word timestamps), so timestamps are **window-level and
deterministic**: each ``window_seconds`` window yields one :class:`TranscriptWindow(index, start_s,
end_s, text)`. ``speaker_label`` is ``None`` (diarization deferred — matches sv-kb).
"""

from __future__ import annotations

import math
import os
import random
import time
from dataclasses import dataclass

import requests

from .config import TranscribeConfig
from .connection import TranscribeConnection

try:  # reuse the PDF track's stdlib rate limiter (no heavy deps); fall back to a no-op if unavailable
    from ..stage3.ratelimit import RateLimiter
except Exception:  # pragma: no cover - defensive
    class RateLimiter:  # type: ignore
        def __init__(self, *_a, **_k) -> None: ...
        def acquire(self, *_a, **_k) -> None: ...


@dataclass
class TranscriptWindow:
    index: int
    start_s: float
    end_s: float
    text: str
    speaker_label: str | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "start_s": round(self.start_s, 3),
                "end_s": round(self.end_s, 3), "text": self.text,
                "speaker_label": self.speaker_label}


class TranscribeCallError(RuntimeError):
    """A transcription call that failed after retries (or a permanent 4xx). The window is marked errored;
    the asset/run continues (fault isolation)."""

    def __init__(self, message: str, code: str = "call_failed") -> None:
        super().__init__(message)
        self.code = code


def build_windows(duration_seconds: float, window_seconds: int) -> list[tuple[int, float, float]]:
    """Deterministic ``(index, start_s, end_s)`` windows covering ``[0, duration]``; the last window is
    clipped to the true duration. A non-positive/unknown duration yields a single open window."""
    if duration_seconds <= 0 or window_seconds <= 0:
        return [(0, 0.0, max(0.0, float(duration_seconds)))]
    n = int(math.ceil(duration_seconds / window_seconds))
    out = []
    for i in range(n):
        start = i * window_seconds
        end = min(duration_seconds, (i + 1) * window_seconds)
        out.append((i, float(start), float(end)))
    return out


def estimate_cost_usd(duration_seconds: float, cfg: TranscribeConfig) -> float:
    """Transcription cost is ∝ audio MINUTES (HANDOFF §8), not file count."""
    return (max(0.0, duration_seconds) / 60.0) * cfg.cost_per_audio_minute


class AzureTranscribeClient:
    """Realtime Azure OpenAI transcription client. Thread-safe (a fresh requests call per invocation);
    share one instance across the worker pool."""

    def __init__(self, conn: TranscribeConnection, cfg: TranscribeConfig,
                 limiter: RateLimiter | None = None) -> None:
        self._conn = conn
        self._cfg = cfg
        self._limiter = limiter or RateLimiter(cfg.requests_per_minute, cfg.tokens_per_minute)

    def transcribe_file(self, audio_path: str) -> str:
        """Transcribe one audio segment file → plain text. Retries transient failures; raises
        :class:`TranscribeCallError` on a permanent 4xx or exhausted retries."""
        headers = {"api-key": self._conn.api_key}
        data = {"response_format": "json"}
        if self._cfg.language:
            data["language"] = self._cfg.language
        name = os.path.basename(audio_path)
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            self._limiter.acquire(0)
            try:
                with open(audio_path, "rb") as fh:
                    files = {"file": (name, fh, "audio/mpeg")}
                    r = requests.post(self._conn.transcription_url, headers=headers, data=data,
                                      files=files, timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:
                last = TranscribeCallError(f"network error: {exc}", "network")
                self._sleep(attempt, None)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = TranscribeCallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                self._sleep(attempt, r.headers.get("Retry-After"))
                continue
            if r.status_code >= 400:
                raise TranscribeCallError(f"http {r.status_code}: {r.text[:300]}", "bad_request")
            return _parse_text(r)
        raise last or TranscribeCallError("exhausted retries", "throttled")

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                time.sleep(min(self._cfg.retry_max_seconds, float(retry_after)))
                return
            except (TypeError, ValueError):
                pass
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay + random.uniform(0, delay / 2))


def _parse_text(r: "requests.Response") -> str:
    """Extract transcript text from a transcription response (json ``{"text": ...}`` or raw text)."""
    ctype = (r.headers.get("Content-Type") or "").lower()
    if "application/json" in ctype:
        try:
            return (r.json().get("text") or "").strip()
        except ValueError:
            pass
    return (r.text or "").strip()
