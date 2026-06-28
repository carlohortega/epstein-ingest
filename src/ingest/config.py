"""Stage-8 tunable knobs (Handoff §6/§7). The DB DSN, Storage connection, and the chosen ``tenant_id`` are
the load identity and come from the CLI/env (connection.py); they are NOT in here. This holds the behavioral
tunables, echoed into the run manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class IngestConfig:
    # --- identity (set at ingest; the only stage that writes the live system) --------------------------
    source_kind: str = "pdf"          # 'pdf' for the PDF track; media stages set audio/video/image
    embedding_model: str = "text-embedding-3-small"   # must match the staged store + chunks' content_sha
    dimensions: int = 1536            # 1536 -> embedding_small ; 3072 -> embedding_large
    # case_key is the CASE (the whole matter) — CONSTANT for this corpus and baked into every chunk's
    # content_sha context_prefix as "epstein/DataSet-NN". dataset_key is derived from the path per document.
    case_key: str = "epstein"

    # --- tenancy resolution policy (Handoff §3a) ------------------------------------------------------
    ensure_tenancy: bool = False      # default (a): require pre-created cases/datasets, fail loud.
    #                                   --ensure-tenancy flips to (b): upsert cases/datasets on first sight.

    # --- safety redaction (Handoff §4a) — fail-safe, non-negotiable -----------------------------------
    content_safety_threshold: int = 4  # CS category severity >= this redacts (matches sv-kb DEFAULT_FLAG_THRESHOLD)
    redact_on_missing_verdict: bool = True   # fail-safe: an image with no/!ambiguous safety verdict is redacted

    # --- throughput --------------------------------------------------------------------------------------
    concurrency: int = 4              # documents in flight (each is one transactional unit)
    batch_size: int = 256            # rows per bulk insert (pre-load embeddings / image vectors)

    def echo(self) -> dict:
        return asdict(self)
