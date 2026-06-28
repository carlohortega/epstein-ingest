"""Build the ``svkb_pipeline.models.IngestionEvent`` for one document at apply time (Handoff §3).

The event carries the tenant + identifiers + the ids ``index_document`` needs. It is the ONE place we mint the
per-document ``ingestion_id`` / ``correlation_id`` UUIDs; everything else comes from the tenant-agnostic
apply-plan record. Pure (no DB/Azure); the only sv-kb import is the dataclass itself, done lazily so the
offline core stays importable without the shared package on the path.
"""

from __future__ import annotations

import uuid


# Stable per-document id namespace so a re-apply reuses the SAME ingestion/correlation ids (idempotent,
# audit-stable) instead of minting fresh randoms each run.
_INGEST_NS = uuid.UUID("b7c4d2e0-0000-5000-8000-0000000000a8")


def make_ids(tenant_id: str, case_key: str, dataset_key: str, document_name: str) -> tuple[str, str]:
    """(ingestion_id, correlation_id) — deterministic uuid5 over the document identity so re-apply is stable."""
    base = f"{tenant_id}/{case_key}/{dataset_key}/{document_name}"
    return (str(uuid.uuid5(_INGEST_NS, "ingest/" + base)),
            str(uuid.uuid5(_INGEST_NS, "corr/" + base)))


def build_event(plan: dict, *, tenant_id: str, document_id: str, source_kind: str,
                user_sub: str | None = None):
    """Assemble the IngestionEvent from a plan record. ``document_id`` is the UUID register obtained."""
    from svkb_pipeline.models import IngestionEvent          # lazy: sv-kb on path only at apply

    ident = plan["identifiers"]
    ingestion_id, correlation_id = make_ids(tenant_id, ident["case_key"], ident["dataset_key"],
                                            ident["document_name"])
    return IngestionEvent(
        tenant_id=tenant_id,
        case_key=ident["case_key"],
        dataset_key=ident["dataset_key"],
        document_name=ident["document_name"],
        document_id=document_id,
        ingestion_id=ingestion_id,
        source_kind=source_kind,
        content_type=(plan.get("document") or {}).get("content_type"),
        source="upload",
        source_url=None,
        blob_container=None,
        blob_path=None,
        correlation_id=correlation_id,
        ingested_by_user_sub=user_sub,
    )
