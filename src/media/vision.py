"""Image vision (Azure AI Vision ``vectorizeImage``, 1024-d) + Content Safety (image:analyze) clients
for keyframes and standalone images (HANDOFF-media-track §3).

Same staged vector format as the PDF track's Stage 4 (``azure-ai-vision-multimodal``, 1024-d,
``.emb.json``, keyed by image ``content_hash = sha256(webp_bytes)``) so S8 unifies PDF + media image
vectors into one ``image_embeddings`` table, deduped by ``content_hash``. Content Safety is the CSAM net:
keyframes/images that score high-risk are flagged so Stage 8's §4a redaction gate replaces the bytes with
a placeholder and omits the vector.

``requests`` at module top mirrors Stage 3; the api KEY is sent as ``Ocp-Apim-Subscription-Key`` and is
never logged or written to disk.
"""

from __future__ import annotations

import base64
import random
import time
from dataclasses import dataclass

import requests

from .config import FramesConfig
from .connection import VisionConnection, ContentSafetyConnection

# Azure Content Safety severity scale is 0/2/4/6 across categories.
SAFETY_CATEGORIES = ("Hate", "SelfHarm", "Sexual", "Violence")


class VisionCallError(RuntimeError):
    def __init__(self, message: str, code: str = "call_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class EmbedResult:
    content_hash: str          # sha256(webp/image bytes) — the dedup/occurrence key (S8 image_embeddings)
    embedding_model: str       # "azure-ai-vision-multimodal"
    model_version: str         # the API's modelVersion (provenance)
    dim: int
    vector: list[float]

    def emb_json(self) -> dict:
        """The staged ``.emb.json`` object — IDENTICAL keys to the PDF track's Stage 4 image vectors
        (``write_embedding``), so S8 unifies PDF + media image vectors uniformly. The content_hash is
        carried on the occurrence row (the dedup key), not in the vector file — matching Stage 4."""
        return {"embedding_model": self.embedding_model, "model_version": self.model_version,
                "dim": self.dim, "vector": self.vector}


@dataclass
class SafetyVerdict:
    flag: str                  # "ok" | "review" | "block"
    max_severity: int
    categories: list[dict]     # [{"category","severity"}] for categories >= review threshold

    def to_dict(self) -> dict:
        return {"flag": self.flag, "max_severity": self.max_severity, "categories": self.categories}


def _retry_sleep(attempt: int, retry_after: str | None, cfg: FramesConfig) -> None:
    if retry_after:
        try:
            time.sleep(min(cfg.retry_max_seconds, float(retry_after)))
            return
        except (TypeError, ValueError):
            pass
    delay = min(cfg.retry_max_seconds, cfg.retry_base_seconds * (2 ** attempt))
    time.sleep(delay + random.uniform(0, delay / 2))


def _post_with_retry(url: str, *, headers: dict, cfg: FramesConfig,
                     data: bytes | None = None, json_body: dict | None = None,
                     what: str = "vision") -> "requests.Response":
    last: Exception | None = None
    for attempt in range(cfg.max_retries + 1):
        try:
            r = requests.post(url, headers=headers, data=data, json=json_body,
                              timeout=cfg.request_timeout_seconds)
        except requests.RequestException as exc:
            last = VisionCallError(f"{what} network error: {exc}", "network")
            _retry_sleep(attempt, None, cfg)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            last = VisionCallError(f"{what} http {r.status_code}: {r.text[:200]}", "throttled")
            _retry_sleep(attempt, r.headers.get("Retry-After"), cfg)
            continue
        if r.status_code >= 400:
            raise VisionCallError(f"{what} http {r.status_code}: {r.text[:300]}", "bad_request")
        return r
    raise last or VisionCallError(f"{what} exhausted retries", "throttled")


class AzureVisionClient:
    """``vectorizeImage`` → 1024-d float vector for WebP image bytes."""

    def __init__(self, conn: VisionConnection, cfg: FramesConfig) -> None:
        self._conn = conn
        self._cfg = cfg

    def embed_image(self, image_bytes: bytes, content_hash: str) -> EmbedResult:
        headers = {"Ocp-Apim-Subscription-Key": self._conn.api_key,
                   "Content-Type": "application/octet-stream"}
        r = _post_with_retry(self._conn.vectorize_image_url, headers=headers, cfg=self._cfg,
                             data=image_bytes, what="vectorizeImage")
        try:
            body = r.json()
        except ValueError as exc:
            raise VisionCallError(f"vectorizeImage non-JSON: {exc}", "bad_output") from exc
        vector = body.get("vector")
        if not isinstance(vector, list) or not vector:
            raise VisionCallError("vectorizeImage missing 'vector'", "bad_output")
        return EmbedResult(content_hash=content_hash, embedding_model=self._cfg.vision_model,
                           model_version=str(body.get("modelVersion") or self._conn.model_version),
                           dim=len(vector), vector=[float(x) for x in vector])


class ContentSafetyClient:
    """``image:analyze`` → a :class:`SafetyVerdict` (the CSAM net)."""

    def __init__(self, conn: ContentSafetyConnection, cfg: FramesConfig) -> None:
        self._conn = conn
        self._cfg = cfg

    def analyze_image(self, image_bytes: bytes) -> SafetyVerdict:
        headers = {"Ocp-Apim-Subscription-Key": self._conn.api_key,
                   "Content-Type": "application/json"}
        body = {"image": {"content": base64.b64encode(image_bytes).decode("ascii")}}
        r = _post_with_retry(self._conn.analyze_image_url, headers=headers, cfg=self._cfg,
                             json_body=body, what="image:analyze")
        try:
            analysis = r.json().get("categoriesAnalysis") or []
        except ValueError as exc:
            raise VisionCallError(f"image:analyze non-JSON: {exc}", "bad_output") from exc
        return verdict_from_analysis(analysis, self._cfg)


def verdict_from_analysis(analysis: list[dict], cfg: FramesConfig) -> SafetyVerdict:
    """Map Content Safety category severities to an ok/review/block verdict.

    HANDOFF §3: high-confidence high-severity => block (Stage 8 redacts the bytes + omits the vector);
    moderate => review; else ok. Stricter than a bare "review" so we only withhold genuinely high-risk
    imagery."""
    max_sev = 0
    flagged: list[dict] = []
    for item in analysis:
        sev = int(item.get("severity") or 0)
        cat = item.get("category")
        max_sev = max(max_sev, sev)
        if sev >= cfg.safety_review_severity:
            flagged.append({"category": cat, "severity": sev})
    if max_sev >= cfg.safety_block_severity:
        flag = "block"
    elif max_sev >= cfg.safety_review_severity:
        flag = "review"
    else:
        flag = "ok"
    flagged.sort(key=lambda x: (-int(x["severity"]), str(x["category"])))
    return SafetyVerdict(flag=flag, max_severity=max_sev, categories=flagged)
