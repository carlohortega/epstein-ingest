"""Azure AI Content Safety — IMAGE analysis (Handoff §5/§7), the independent CSAM net on photo artifacts.

A separate Azure Content Safety endpoint/key. We POST base64 image content to ``image:analyze`` and get
per-category severities back (Hate / SelfHarm / Sexual / Violence). This is the authoritative image-safety
signal that complements the gpt-5-mini ``safety`` triage tags — non-negotiable in this domain.

Optional: when the Content Safety env vars are absent the scanner is None and Stage 4 records
``content_safety: {"status": "not_configured"}`` on assets (a loud one-time warning is printed at
startup). Per sv-kb policy we LABEL, we do not block: a non-zero severity sets ``flag="review"``; we never
abort. A scan FAILURE (API error) is recorded as ``{"status": "error"}`` on the asset and never aborts the
run. Content Safety runs regardless of the local/vision content verdict (a "blank" is not proof).
"""

from __future__ import annotations

import base64
import time

import requests

from .client import VisionCallError
from .config import Stage4Config
from .connection import ContentSafetyConnection

# Azure Content Safety severities are 0/2/4/6 (four-level) — any non-zero severity is worth a review.
REVIEW_MIN_SEVERITY = 1


class ContentSafetyScanner:
    """Thread-safe Content Safety image scanner (a fresh requests call per invocation)."""

    def __init__(self, conn: ContentSafetyConnection, cfg: Stage4Config) -> None:
        self._conn = conn
        self._cfg = cfg

    def analyze(self, image_bytes: bytes) -> dict:
        """Return a normalized result: {flag, max_severity, categories:[{category,severity}], status:'ok'}.
        Raises VisionCallError on a permanent failure / exhausted retries (the caller records status
        'error' on the asset and continues)."""
        payload = {"image": {"content": base64.b64encode(image_bytes).decode("ascii")}}
        headers = {"Ocp-Apim-Subscription-Key": self._conn.api_key, "Content-Type": "application/json"}
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                r = requests.post(self._conn.analyze_image_url, headers=headers, json=payload,
                                  timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:
                last = VisionCallError(f"network error: {exc}", "network")
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = VisionCallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                elif r.status_code >= 400:
                    raise VisionCallError(f"content-safety http {r.status_code}: {r.text[:300]}",
                                          "bad_request")
                else:
                    return _normalize(r.json())
            self._sleep(attempt)
        raise last or VisionCallError("content-safety exhausted retries", "throttled")

    def _sleep(self, attempt: int) -> None:
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay)


def _normalize(body: dict) -> dict:
    """Content Safety image:analyze response -> our normalized shape (label-not-block)."""
    cats = []
    max_sev = 0
    for c in (body.get("categoriesAnalysis") or []):
        sev = int(c.get("severity") or 0)
        cats.append({"category": c.get("category"), "severity": sev})
        max_sev = max(max_sev, sev)
    return {
        "status": "ok",
        "flag": "review" if max_sev >= REVIEW_MIN_SEVERITY else "ok",
        "max_severity": max_sev,
        "categories": cats,
    }
