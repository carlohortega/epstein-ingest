"""The §4a content-safety gate — STAGED-ingest conformance to docs/SAFETY-staged-vs-svkb.md (sv-kb repo).

Staged ingest **removes** violations pre-ingest (the corpus is company-curated); sv-kb gates at serving. Both
write the SAME data model (a row is indistinguishable to the gateway/orchestrator). Three outcomes:

  1. **CSAM-suspected** (vision ``minor_indicator`` or a ``csam``/``csam_suspected`` category) → the WHOLE
     document is **NOT ingested** into sv-kb; preserved + reported in the staging system of record (never
     deleted — §6 of the safety doc). ``safety_status='quarantined'`` is the sv-kb CSAM analog.
  2. **Withhold** (sexual / violence / hate / self-harm ≥ threshold, vision ``explicit``, an Azure
     content-filter block, or — fail-safe — an un-vetted image): the asset is removed and the page gets a
     placeholder; per-unit ``review_status='withheld'`` (TERMINAL, not ``pending_review``); the vector is
     absent; ``documents.source_withheld=true``. (Per 2026-06-25: hate/self-harm ARE withheld in both systems.)
  3. **Clean** → ingested normally.

Terminology: "withhold/suppress" = the SYSTEM's safety action; "redaction" = the source document's own
``[REDACTED]`` black bars — never overload it. Pure / dependency-free (offline-testable, dry-run-projectable).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ≥ threshold (or vision-flagged) → WITHHOLD the asset (document still ingested).
_WITHHOLD_CATEGORIES = {"sexual", "violence", "hate", "self_harm", "self-harm", "selfharm"}
# → CSAM tier: the whole document is NOT ingested (preserve + report upstream).
_CSAM_CATEGORIES = {"csam", "csam_suspected", "minor"}

_ASSETS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "assets", "redaction")
_PLACEHOLDER_PNG = {"withheld": "content-redacted.png", "blank": "page-blank.png"}


@dataclass(frozen=True)
class Verdict:
    csam: bool                  # True => the WHOLE document must NOT be ingested (preserve+report upstream)
    withhold: bool              # True => remove this asset; page gets a placeholder; review_status='withheld'
    reasons: tuple[str, ...]
    tier: str | None            # "csam" | "sexual" | "violence" | "hate" | "self_harm" | "content_filter" | "unvetted"


def _cs_over_threshold(asset: dict, threshold: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for c in ((asset.get("content_safety") or {}).get("categories") or []):
        try:
            if int(c.get("severity") or 0) >= threshold:
                cat = str(c.get("category") or "").lower()
                out.append((cat, f"cs:{cat}={c.get('severity')}"))
        except (TypeError, ValueError):
            continue
    return out


def image_asset_verdict(asset: dict, *, threshold: int = 4, withhold_on_missing: bool = True) -> Verdict:
    """Classify a Stage-4 image asset. CSAM signals win (document-level skip); otherwise the withhold set;
    else clean. HIGH-CONFIDENCE only — a bare ``safety.flag=='review'`` does NOT trigger."""
    reasons: list[str] = []
    safety = asset.get("safety") or {}
    cats = {str(c).lower() for c in (safety.get("categories") or [])}
    cs = _cs_over_threshold(asset, threshold)
    cs_cats = {c for c, _ in cs}

    # --- CSAM tier (document not ingested) ---
    csam = bool(safety.get("minor_indicator") is True or (cats | cs_cats) & _CSAM_CATEGORIES)
    if csam:
        if safety.get("minor_indicator") is True:
            reasons.append("minor_indicator")
        reasons += [f"category:{c}" for c in sorted((cats | cs_cats) & _CSAM_CATEGORIES)]
        return Verdict(True, True, tuple(reasons), "csam")

    # --- withhold tier (asset removed, document ingested) ---
    tier: str | None = None
    if asset.get("content_filtered") is True or (asset.get("stage4_error") or {}).get("code") == "content_filter":
        reasons.append("content_filter"); tier = "content_filter"
    if safety.get("explicit") is True:
        reasons.append("explicit"); tier = tier or "sexual"
    for hit in sorted((cats | cs_cats) & _WITHHOLD_CATEGORIES):
        reasons.append(f"category:{hit}"); tier = tier or hit.replace("-", "_").replace("selfharm", "self_harm")
    reasons += [r for _, r in cs if _ not in _CSAM_CATEGORIES and _ in _WITHHOLD_CATEGORIES]

    # fail-safe: an image with visual content but an errored/absent verdict is un-vetted -> withhold.
    if not reasons and withhold_on_missing and asset.get("has_visual_content") is True:
        if asset.get("stage4_error") or not safety or safety.get("flag") is None:
            reasons.append("unvetted_verdict"); tier = "unvetted"

    return Verdict(False, bool(reasons), tuple(dict.fromkeys(reasons)), tier)


def page_is_blank(page: dict) -> bool:
    """A blank / effectively-blank PDF page → ``page-blank`` placeholder (staged-only; no sv-kb analog)."""
    if (page.get("page_kind") or "").lower() == "blank":
        return True
    text = ((page.get("text") or {}).get("content") or "").strip()
    return not text and page.get("has_visual_content") is False


def media_doc_verdict(media_sidecar: dict, *, threshold: int = 4) -> Verdict:
    """Standalone media (image/audio/video): a flagged asset is WITHHELD (not uploaded — no document); a
    CSAM-suspected one is NOT ingested + preserved/reported. Same predicate against the media safety rollup."""
    saf = (media_sidecar.get("document_summary_final") or media_sidecar.get("media") or {}).get("safety") or {}
    pseudo = {"safety": {"flag": saf.get("flag"), "categories": saf.get("categories"),
                         "minor_indicator": saf.get("minor_indicator"), "explicit": saf.get("explicit")},
              "content_filtered": saf.get("content_filtered"),
              "content_safety": media_sidecar.get("content_safety"), "has_visual_content": True}
    return image_asset_verdict(pseudo, threshold=threshold, withhold_on_missing=False)


def placeholder_bytes(kind: str) -> bytes:
    with open(os.path.join(_ASSETS_DIR, _PLACEHOLDER_PNG[kind]), "rb") as f:
        return f.read()


def placeholder_content_hash(kind: str) -> str:
    import hashlib
    return hashlib.sha256(placeholder_bytes(kind)).hexdigest()
