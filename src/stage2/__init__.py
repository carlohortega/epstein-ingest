"""text-first-extract Stage 2 — render pages, classify ``page_kind``, register image assets.

For every PDF already processed by Stage 1 (a ``status:"ok"`` sidecar), this stage:
  * renders **every** page to PNG on local disk (image pages -> ``<name>/images/``, the rest ->
    ``<name>/pages/``),
  * deterministically classifies each page as ``text | image | mixed | blank | redacted``,
  * extracts embedded raster photo artifacts (deduped by content hash) to ``<name>/artifacts/``,
  * generates one ``<name>/thumbnail.png`` per PDF,
  * extends the Stage-1 sidecar JSON with per-page ``page_kind`` + ``render_path`` and a top-level
    ``image_assets[]`` manifest + ``thumbnail_path`` + a ``stage2`` block.

100% local: reads the PDF + its sidecar, writes PNGs and the extended sidecar. No network, no Azure,
no database. Idempotent, resumable, deterministic. Only ``page_kind == image`` pages (plus kept photo
artifacts) make up the downstream **vision workload** (Stage 4).

Spec: docs/HANDOFF-stage2-render-classify-register.md
"""

STAGE2_TOOL_NAME = "text-first-extract-stage2"
STAGE2_TOOL_VERSION = "1.0.0"

__all__ = ["STAGE2_TOOL_NAME", "STAGE2_TOOL_VERSION"]
