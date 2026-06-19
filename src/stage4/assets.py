"""Stage-4 domain logic, independent of transport so the realtime scheduler and the offline tests share
it: per-asset routing/pre-gate, the bundled vision call, image embedding, content safety, response
normalization, and a token/cost accumulator.

Everything that calls a model takes injected ``client`` / ``embedder`` / ``scanner`` objects, so fake
implementations drive the offline tests (outputs are model-generated => provenance over byte-determinism,
mirroring Stage 3).

The unit of work is the IMAGE ASSET (image page ∪ photo artifact ∪ escalated low_quality_text page), not
the page. ``process_asset`` runs the whole chain for one asset — pre-gate, then (if not pre-gated) the
vision verdict, then an embedding for kept assets, then content safety — and returns an ``AssetOutcome``
the process layer merges into the sidecar.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field

from .client import LLMResult, VisionCallError
from .config import Stage4Config
from .pregate import pregate_asset, SOURCE_PREGATE, SOURCE_VISION
from .prompts import (SAFETY_CATEGORIES, SAFETY_FLAGS, CONTENT_CLASSES, ASSET_SCHEMA,
                      build_asset_messages)

# content_class values that, by definition, carry NO visual content — the gate is forced false for these
# so content_class and has_visual_content can never disagree (downstream single source of truth, Handoff §2).
NO_CONTENT_CLASSES = {"blank", "black_or_redacted", "broken_placeholder", "text_only"}
_MIME_BY_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


# --- token / cost accounting (Handoff §6) ----------------------------------------------------------
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    def add(self, r: LLMResult) -> "Usage":
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.cached_tokens += r.cached_tokens
        self.calls += 1
        return self

    def merge(self, other: "Usage") -> "Usage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls
        return self


@dataclass
class AssetOutcome:
    index: int
    status: str                       # "pregated" | "vision" | "content_filtered" | "error"
    verdict: dict | None = None       # per-asset fields to merge (caption/description/.../safety)
    source: str = SOURCE_VISION       # how the verdict was produced (pregate | vision)
    prefilter_hint: str | None = None
    usage: Usage = field(default_factory=Usage)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_error: str | None = None
    content_safety: dict | None = None
    content_filter_entry: dict | None = None
    error_code: str | None = None
    error_message: str | None = None


# --- paths / IO ------------------------------------------------------------------------------------
def asset_abs_path(base_dir: str, asset: dict) -> str:
    return os.path.join(base_dir, str(asset.get("path") or "").replace("/", os.sep))


def asset_mime(asset: dict) -> str:
    ext = os.path.splitext(str(asset.get("path") or ""))[1].lower()
    return _MIME_BY_EXT.get(ext, "image/png")


def embedding_ref_for(asset: dict) -> str:
    """Relative sibling path for an asset's image embedding vector (kept next to images/artifacts)."""
    parts = str(asset.get("path") or "").split("/")
    name = parts[0] if parts else "asset"
    fname = parts[-1] if parts else "asset.png"
    return f"{name}/embeddings/{fname}.emb.json"


def image_token_estimate(asset: dict, cfg: Stage4Config) -> int:
    """Rate-limiter / dry-run image token budget: a full page bills ~2,490 tokens; artifacts are smaller."""
    if asset.get("kind") == "artifact":
        return cfg.dryrun_artifact_input_tokens
    return cfg.dryrun_image_input_tokens


# --- response normalization (defensive even though the schema is strict) ----------------------------
def normalize_verdict(data: dict) -> dict:
    """Clamp/validate the model object to the documented bounds and enforce gate consistency."""
    caption = str(data.get("caption") or "").strip()
    description = str(data.get("description") or "").strip()
    cls = data.get("content_class")
    if cls not in CONTENT_CLASSES:
        cls = "indeterminate"
    has = bool(data.get("has_visual_content"))
    if cls in NO_CONTENT_CLASSES:            # gate can never disagree with a no-content class
        has = False
    try:
        conf = float(data.get("vision_confidence"))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))

    s = data.get("safety") or {}
    flag = s.get("flag") if s.get("flag") in SAFETY_FLAGS else "ok"
    cats = [c for c in (s.get("categories") or []) if c in SAFETY_CATEGORIES] if flag == "review" else []
    safety = {"flag": flag, "categories": cats,
              "minor_indicator": bool(s.get("minor_indicator")),
              "explicit": bool(s.get("explicit"))}
    return {"caption": caption, "description": description, "content_class": cls,
            "has_visual_content": has, "vision_confidence": round(conf, 4), "safety": safety}


# --- the per-asset chain ---------------------------------------------------------------------------
def _content_safety_applies(asset: dict, cfg: Stage4Config) -> bool:
    if cfg.content_safety_artifacts_only:
        return asset.get("kind") == "artifact"
    return True


def process_asset(index: int, asset: dict, *, base_dir: str, page_dark: dict, cfg: Stage4Config,
                  client, embedder, scanner) -> AssetOutcome:
    """Run the full chain for one asset. Fault-isolated: any failure becomes an outcome (status 'error' or
    a per-step error field), never an exception that aborts the run."""
    out = AssetOutcome(index=index, status="vision")
    path = asset_abs_path(base_dir, asset)

    # (1) Tier-1 local pre-gate — may decide the asset for free (no vision call).
    pg = pregate_asset(asset, page_dark, base_dir, cfg)
    if pg is not None:
        out.status = "pregated"
        out.source = SOURCE_PREGATE
        out.verdict = pg["verdict"]
        out.prefilter_hint = pg["prefilter_hint"]
    else:
        # (2) load the image + the bundled vision verdict.
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError as exc:
            out.status = "error"
            out.error_code, out.error_message = "image_read", f"{type(exc).__name__}: {exc}"
            return out
        b64 = base64.b64encode(raw).decode("ascii")
        messages = build_asset_messages(b64, mime=asset_mime(asset), detail=cfg.image_detail)
        try:
            res = client.complete_json(messages, ASSET_SCHEMA, cfg.max_output_tokens,
                                       image_tokens=image_token_estimate(asset, cfg))
        except VisionCallError as exc:
            if exc.code == "content_filter":
                out.status = "content_filtered"
                out.verdict = {
                    "caption": None, "description": None, "content_class": "indeterminate",
                    "has_visual_content": False, "vision_confidence": 0.0,
                    "safety": {"flag": "review", "categories": [], "minor_indicator": False,
                               "explicit": False, "content_filtered": True},
                }
                out.content_filter_entry = {"file": None, "asset_path": asset.get("path"),
                                            "error_code": exc.code, "error_message": str(exc)[:300]}
            else:
                out.status = "error"
                out.error_code, out.error_message = exc.code, str(exc)[:300]
            return out
        out.usage.add(res)
        out.verdict = normalize_verdict(res.data)

    raw_bytes = None  # lazily loaded for embed/safety

    def _load_raw() -> bytes | None:
        nonlocal raw_bytes
        if raw_bytes is None:
            try:
                with open(path, "rb") as f:
                    raw_bytes = f.read()
            except OSError:
                raw_bytes = b""
        return raw_bytes or None

    # (3) image embedding — only for assets the gate kept (Handoff §5).
    if embedder is not None and out.verdict and out.verdict.get("has_visual_content"):
        data = _load_raw()
        if data is not None:
            try:
                vec, model_version = embedder.embed(data)
                out.embedding = vec
                out.embedding_model = model_version
            except VisionCallError as exc:
                out.embedding_error = f"{exc.code}: {str(exc)[:200]}"

    # (4) content safety — the CSAM net; runs regardless of the content verdict (a 'blank' is not proof).
    if scanner is not None and _content_safety_applies(asset, cfg):
        data = _load_raw()
        if data is not None:
            try:
                out.content_safety = scanner.analyze(data)
            except VisionCallError as exc:
                out.content_safety = {"status": "error", "error": f"{exc.code}: {str(exc)[:200]}"}

    return out
