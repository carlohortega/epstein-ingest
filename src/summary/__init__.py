"""text-first-extract — dataset summary (read-only progress / cost / volume / safety / routing dashboard).

This is the cross-stage reporting sibling of ``src/stageN``: a **read-only scanner** that pruned-walks the
per-PDF ``<name>.json`` sidecars Stages 1–6 emit, extracts a compact per-doc *digest* via an extensible
collector registry (``collectors.py``), and rolls those up into per-dataset + corpus-wide reports — JSON
(canonical), Markdown (terminal/PR dashboard), and a self-contained HTML dashboard. See
``docs/HANDOFF-dataset-summary.md`` for the full requirements.

Hard rules (acceptance §9):
  * **Read-only / never mutate.** Sidecars are opened ``"r"`` and never written back; the only writes are
    the report artifacts + the incremental cache, which go to a reports location, never on top of a sidecar.
  * **Source of truth = the per-sidecar blocks, NOT the run-manifests.** Progress/cost/volume are derived
    from the blocks actually present in each sidecar (manifests are stale/partial/inconsistently located).
  * **Pure stdlib.** No LLM, no Azure, no PyMuPDF, no sv-kb. Crucially this means we DO NOT import
    ``src.stage1`` (it ``import fitz``) or ``src.stage6`` (it imports sv-kb at module load): the three tiny
    helpers we need (atomic write, the pruned walk, the dataset/case derivation) are reimplemented in
    ``util.py``/``walk.py`` so a process-pool worker never drags PyMuPDF/sv-kb into every child.

The dashboard's job is to keep growing, so metrics are a registry of per-stage collectors (``collectors.py``)
and the report carries a ``schema_version`` — adding a metric or a Stage 7 is a localized change.
"""

from __future__ import annotations

SUMMARY_TOOL_NAME = "text-first-extract-summary"
SUMMARY_TOOL_VERSION = "1.1.0"   # 1.1: S7 (embed) + S8 (ingest) grid, vision-embed coverage, source_kind

# Bumped whenever the digest/aggregate shape changes — also invalidates the on-disk incremental cache so a
# stale digest from an older shape can never be reused (acceptance §9). 2.0.0: added S7 (embed)/S8 (ingest),
# vision-embed coverage (embedding_ref), and source_kind.
SCHEMA_VERSION = "2.0.0"
CACHE_VERSION = "2.0.0"

# The pipeline stages we report on, in pipeline order. Drives the status grid. After Stage 6 the pipeline
# forks into PDF/media tracks then rejoins at the shared embed (S7) + ingest (S8) tail (Handoff §7).
STAGES = ("stage1", "stage2", "stage3", "stage4", "stage5", "stage6", "stage7", "stage8")

# Per-stage label + the sidecar block whose *presence* is the done-signal (Handoff §2). Stage 1's done-signal
# is the sidecar existing with a ``status`` set (it has no ``stage1`` block — it IS the base document). S7
# (embed) and S8 (ingest) are NOT sidecar blocks: S7 is the file vector store (store-aware coverage), S8 is
# live DB rows (online cross-check only).
STAGE_LABELS = {
    "stage1": "S1 extract",
    "stage2": "S2 stage",
    "stage3": "S3 text",
    "stage4": "S4 vision",
    "stage5": "S5 finalize",
    "stage6": "S6 chunk",
    "stage7": "S7 embed",
    "stage8": "S8 ingest",
}

# Report artifact filenames (written to a reports location, never onto a sidecar).
DATASET_REPORT_STEM = "dataset-summary"
CORPUS_REPORT_STEM = "corpus-summary"
CACHE_NAME = ".summary-cache.json"

__all__ = [
    "SUMMARY_TOOL_NAME", "SUMMARY_TOOL_VERSION", "SCHEMA_VERSION", "CACHE_VERSION",
    "STAGES", "STAGE_LABELS", "DATASET_REPORT_STEM", "CORPUS_REPORT_STEM", "CACHE_NAME",
]
