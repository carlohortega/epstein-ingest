"""S9 apply (Handoff §2) — the ONLY step that writes the live system. Consumes the S8 apply-package and, per
document, replays it against Postgres + Azure Storage. Restartable, idempotent, can run on a different host
from S8 (the package + the staged corpus are its only inputs).

Order (whole run): read package → connect DB (``DATABASE_URL``) + blob uploader (``AZURE_STORAGE_ACCOUNT``) →
tenancy preflight (read-only; under policy A, FAIL before any write if a case/dataset is missing) → seed the
shared placeholders → **bulk pre-load all vectors once** (text→embeddings, image→image_embeddings).
Per document: resolve tenancy → register rows (+occurrences) → upload blobs (§4a placeholders) → load
chunks.json → ``index_document`` VERBATIM with the guard provider (embeds nothing). Completed docs are skipped
on re-run unless ``--force``.

All sv-kb / psycopg / azure imports are lazy (here + the modules this calls), so the rest of ``src.ingest``
stays importable without creds or those libraries. The launcher (``C:\\epstein-ingest\\run_apply.py``) loads the
env before calling ``apply_package``.
"""

from __future__ import annotations

import json
import os
import time

from .config import IngestConfig
from .event import build_event, make_ids
from .tenancy import TenancyResolver, preflight, TenancyError
from .register import register_document
from .load import preload_vectors, insert_occurrences, index_document_verbatim
from .blobs import BlobUploader, group_manifest_by_document
from .discover import chunks_path
from .util import dump_json


def _read_plan(pkg_dir: str) -> list:
    out = []
    with open(os.path.join(pkg_dir, "apply-plan.jsonl"), "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def _load_parents(plan: dict):
    """Load the document's chunks.json into ``ParentChunk`` objects for ``index_document`` (§2 step 6).
    The path is carried explicitly (``staged_chunks``) because the PDF and media tracks name it differently
    (``<stem>.chunks.json`` vs ``<asset>.chunks.json``)."""
    from svkb_pipeline.chunking import parents_from_dicts     # lazy: sv-kb on path only at apply
    cpath = plan.get("staged_chunks") or (chunks_path(plan["staged_pdf"]) if plan.get("staged_pdf") else None)
    if not cpath or not os.path.exists(cpath):
        return []
    with open(cpath, "r", encoding="utf-8") as f:
        return parents_from_dicts(json.load(f))


def _doc_prefix(path_builder, case_key: str, dataset_key: str, document_name: str) -> str:
    return path_builder.source_pdf(case_key, dataset_key, document_name).path[: -len("source.pdf")]


def apply_package(pkg_dir: str, cfg: IngestConfig, *, tenant_id: str, workers: int = 1, force: bool = False,
                  dsn: str | None = None, user_sub: str | None = None, skip_blobs: bool = False) -> dict:
    """Apply a prepared package to the live system. Returns the apply manifest (also written to the package).

    ``skip_blobs`` writes only the database rows (registration + vectors + chunks via index_document) and does
    NOT upload any blobs — a DB-only integration test that needs neither azcopy nor Storage creds. Blob PATHS
    are still recorded on the rows (computed from the tenant path builder); only the bytes upload is skipped."""
    started = time.monotonic()
    plans = _read_plan(pkg_dir)
    by_doc_blobs = group_manifest_by_document(pkg_dir)
    dsn = dsn or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL not set — required for a live apply")
    try:
        from svkb_pipeline.db import Database                 # lazy: sv-kb/psycopg on path only at apply
        from svkb_pipeline.blob_paths import TenantPathBuilder
    except ImportError as exc:
        raise RuntimeError(
            "svkb_pipeline not importable — put sv-kb's pipeline/shared on PYTHONPATH for a live apply "
            f"(the launcher run_apply.py does this). Underlying: {exc}") from exc
    db = Database(dsn)
    path_builder = TenantPathBuilder(tenant_id)               # blob-path strings only (no creds)
    uploader = None if skip_blobs else BlobUploader(tenant_id)
    resolver = TenancyResolver(ensure=cfg.ensure_tenancy)

    # --- tenancy preflight (read-only). Policy A: fail loud BEFORE any write if a pair is missing. ---
    pairs = sorted({(p["identifiers"]["case_key"], p["identifiers"]["dataset_key"]) for p in plans})
    with db.tenant(tenant_id, user_sub) as cur:
        pre = preflight(cur, tenant_id, pairs, ensure=cfg.ensure_tenancy)
    if pre["would_fail"]:
        raise TenancyError(
            f"tenancy preflight failed (policy A): missing cases/datasets {pre['would_fail']}. "
            f"Pre-create them or re-run with --ensure-tenancy.")

    # --- seed placeholders + bulk pre-load all vectors once ---
    if uploader is not None:
        uploader.seed_placeholders()
    preload = preload_vectors(db, tenant_id, pkg_dir, cfg, user_sub=user_sub)

    ingested = skipped = failed = 0
    errors: list = []
    occ_total = blob_uploads = 0
    for plan in plans:
        ident = plan["identifiers"]
        ck, dk, dn = ident["case_key"], ident["dataset_key"], ident["document_name"]
        try:
            # resumability: skip a document already marked complete (unless --force)
            if not force:
                with db.tenant(tenant_id, user_sub) as cur:
                    cur.execute(
                        "SELECT status FROM pipeline.documents WHERE case_key=%s AND dataset_key=%s "
                        "AND document_name=%s", (ck, dk, dn))
                    row = cur.fetchone()
                if row and row[0] == "complete":
                    skipped += 1
                    continue

            ingestion_id, correlation_id = make_ids(tenant_id, ck, dk, dn)
            processed_prefix = _doc_prefix(path_builder, ck, dk, dn)
            sk = plan.get("source_kind") or cfg.source_kind        # per-document (pdf | audio | video | image)

            # register (own transaction) — tenancy resolve + documents/pages/asset_context/findings +
            # media_metadata (A/V) + image_occurrences (keyframes/images)
            with db.tenant(tenant_id, user_sub) as cur:
                case_id, dataset_id = resolver.resolve(cur, tenant_id, ck, dk)
                document_id = register_document(
                    cur, plan, tenant_id=tenant_id, case_id=case_id, dataset_id=dataset_id,
                    ingestion_id=ingestion_id, correlation_id=correlation_id,
                    source_kind=sk, path_builder=path_builder)
                occ_total += insert_occurrences(cur, plan, tenant_id=tenant_id, document_id=document_id,
                                                processed_prefix=processed_prefix)

            # upload blobs (§4a placeholders for withheld/blank) — outside the DB transaction
            if uploader is not None:
                up = uploader.upload_document(by_doc_blobs.get((ck, dk, dn), []))
                blob_uploads += up["uploaded"] + up["placeholders"]

            # index chunks VERBATIM with the guard provider (pre-load made to_embed empty)
            parents = _load_parents(plan)
            event = build_event(plan, tenant_id=tenant_id, document_id=document_id,
                                source_kind=sk, user_sub=user_sub)
            index_document_verbatim(db, event, parents, model=cfg.embedding_model,
                                    dimensions=cfg.dimensions, page_count=(plan.get("document") or {}).get(
                                        "page_count"), batch_size=cfg.batch_size)
            ingested += 1
        except Exception as exc:                              # per-document isolation; keep going
            failed += 1
            errors.append({"document": f"{ck}/{dk}/{dn}", "error": f"{type(exc).__name__}: {exc}"[:300]})

    manifest = {
        "tool": "text-first-extract-ingest", "mode": "apply", "tenant_id": tenant_id,
        "package": os.path.abspath(pkg_dir), "skip_blobs": skip_blobs,
        "tenancy_preflight": pre, "preload": preload,
        "documents": {"ingested": ingested, "skipped_complete": skipped, "failed": failed,
                      "total": len(plans)},
        "image_occurrences": occ_total, "blob_objects": blob_uploads,
        "errors": errors[:500],
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(pkg_dir, "apply-manifest.json"))
    db.close()
    return manifest
