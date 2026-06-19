"""Azure AI Vision multimodal IMAGE embeddings (Handoff §5).

A separate Azure AI Vision (Computer Vision) endpoint/key from Azure OpenAI. We POST the raw image bytes
to ``retrieval:vectorizeImage`` and get a dense vector back. Cheap relative to the gpt-5-mini call, and
only produced for ``has_visual_content=true`` assets (Handoff §5 downstream rule).

Optional: when the AI Vision env vars are absent the embedder is None and Stage 4 records ``embedding_ref:
null`` (the run still produces captions/verdicts/safety). The vector is stored in a sibling file by the
process layer; this module only fetches it. Retries 429/5xx with backoff (same posture as the LLM client).
"""

from __future__ import annotations

import time

import requests

from .client import VisionCallError
from .config import Stage4Config
from .connection import AIVisionConnection


class ImageEmbedder:
    """Thread-safe AI Vision multimodal embedder (a fresh requests call per invocation)."""

    def __init__(self, conn: AIVisionConnection, cfg: Stage4Config) -> None:
        self._conn = conn
        self._cfg = cfg

    def embed(self, image_bytes: bytes) -> tuple[list[float], str]:
        """Return (vector, model_version). Raises VisionCallError on a permanent failure / exhausted
        retries so the caller records an embedding error on that asset without aborting the run."""
        headers = {"Ocp-Apim-Subscription-Key": self._conn.api_key,
                   "Content-Type": "application/octet-stream"}
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                r = requests.post(self._conn.vectorize_image_url, headers=headers, data=image_bytes,
                                  timeout=self._cfg.request_timeout_seconds)
            except requests.RequestException as exc:
                last = VisionCallError(f"network error: {exc}", "network")
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = VisionCallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                elif r.status_code >= 400:
                    raise VisionCallError(f"embed http {r.status_code}: {r.text[:300]}", "bad_request")
                else:
                    body = r.json()
                    vec = body.get("vector")
                    if not isinstance(vec, list):
                        raise VisionCallError("embed response missing 'vector'", "bad_output")
                    return [float(x) for x in vec], str(body.get("modelVersion")
                                                        or self._conn.model_version)
            self._sleep(attempt, None)
        raise last or VisionCallError("embed exhausted retries", "throttled")

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        delay = min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt))
        time.sleep(delay)
