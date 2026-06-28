"""text-first-extract Stage 4 — image-asset vision (gpt-5-mini) + image embeddings + content safety.

For every entry in a sidecar's ``image_assets[]`` (image pages ∪ photo artifacts ∪ escalated
``low_quality_text`` pages, per the 2026-06-18 over-inclusive routing decision), this stage:

  1. runs a cheap LOCAL **Tier-1 pre-gate** (no API call) that classifies the provably-blank scans as
     ``content_class="blank"`` / ``has_visual_content=false`` and skips the paid call for them;
  2. makes **one bundled gpt-5-mini vision call** for everything else, returning ``caption`` +
     ``description`` + a content verdict (``content_class`` + ``has_visual_content`` — *the gate*) +
     ``vision_confidence`` + image ``safety`` tags;
  3. produces an **image embedding** (Azure AI Vision multimodal) for ``has_visual_content=true`` assets;
  4. runs **Azure Content Safety** on photo artifacts (the CSAM net) when configured.

It extends the sidecar JSON in place (per-asset fields + a ``stage4`` block), writes a run manifest, and
appends content-filter-blocked assets to ``content-filter-failures.json``. Vision only — text is Stage 3.

Local staging only: no Azure Storage, no DB (that is Stage 7). Idempotent (re-run is a no-op on assets
that already carry a verdict), resumable (per-asset granularity), fault-isolated (a bad PNG / API error
skips that asset with a manifest entry, never aborts the run). The Azure api KEY is env-only and is never
written to a sidecar or manifest.

The headline feature is NOT the caption — it is the ``has_visual_content`` gate, the single downstream
source of truth that keeps blanks / broken placeholders / slip-sheets from ever becoming image assets.

Spec: docs/HANDOFF-stage4-image-vision.md
"""

STAGE4_TOOL_NAME = "text-first-extract-stage4"
STAGE4_TOOL_VERSION = "1.0.1"  # 1.0.1: detect the image content-filter 400 (content_policy_violation)

# Bumped whenever the vision system prompt / response schema changes. Stamped into every verdict's
# provenance so a sidecar is self-describing (outputs are LLM-generated => provenance over byte
# determinism, mirroring Stage 3).
PROMPT_VERSION = "stage4-2026-06-19.2"  # .2: redaction rule hardened — don't infer the subject under a bar

__all__ = ["STAGE4_TOOL_NAME", "STAGE4_TOOL_VERSION", "PROMPT_VERSION"]
