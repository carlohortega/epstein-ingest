"""text-first-extract Stage 8 — unified ingest (`src/ingest`): load a fully-staged corpus into the live
sv-kb system (Postgres + Azure Storage). This is the ONLY stage that writes to the live system; Stages 1–7
(PDF) and the media track are file-only.

Design (spec: docs/HANDOFF-stage8-ingest.md):
  * **Reuse `svkb_pipeline.indexing.index_document` VERBATIM** via pre-load + a guard provider — the staging
    pipeline exists so this stage is a thin loader (it embeds nothing).
  * **Source-agnostic** — one loader for PDF and (built) media; `index_document` is `source_kind`-blind.
  * **Owns registration** — creates tenant/case/dataset (resolve §3a), documents/pages/asset_context rows.
  * **Safety redaction (§4a) is non-negotiable** — high-risk image bytes are NEVER uploaded (placeholder
    substituted, vector omitted); quarantined standalone media is not uploaded at all; blank pages get a
    blank placeholder. See ``safety.py``.
  * **Walk the IMAGES dir per dataset** — at the dataset root (DS-1..8) or under VOL* (DS-9..12); never the
    dataset root (excludes the DATA/NATIVES/CORRUPTED/... junk).

Layering for offline testability: the discovery / safety-gate / store-reader / dry-run core imports **no**
psycopg/azure/PyMuPDF. The live writers (``register``/``blobs``/``load``) lazy-import sv-kb's db/blob/indexing
only when actually writing, so ``--dry-run`` and the unit tests run without creds or those libraries.
"""

INGEST_TOOL_NAME = "text-first-extract-ingest"
INGEST_TOOL_VERSION = "1.0.0"

__all__ = ["INGEST_TOOL_NAME", "INGEST_TOOL_VERSION"]
