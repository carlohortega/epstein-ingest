"""S8 prepare — emit the reviewable **apply-package** (Handoff §0a). Offline, NO creds, deterministic.

Walks each dataset's IMAGES dir, builds a tenant-agnostic ``ApplyRecord`` per document (``record.py``), and
writes under ``<out>/``:

  * ``apply-plan.jsonl``        — one record per ingestable document (identifiers, the documents row + metadata
                                  bag, document_pages, asset_context, content_safety_findings, the child
                                  content_sha list + any missing, the §4a blob plan, and image-vector refs /
                                  occurrences). The big vectors are NOT inlined here — they live in the COPY files.
  * ``embeddings.copy``         — global, deduped text vectors: ``content_sha \\t dim \\t [v,v,...]``.
  * ``image_embeddings.copy``   — global, deduped image vectors: ``content_hash \\t dim \\t [v,v,...]``.
  * ``blobs.manifest.jsonl``    — every file to place: ``{case_key,dataset_key,document_name,artifact,action,
                                  source_path,content_type}``. S9 resolves the tenant blob path + azcopy/placeholder.
  * ``prepare-manifest.json``   — counts, CSAM-skipped list, withheld-by-tier, tenancy pairs, vectors_missing.

Tenant-agnostic by construction (§6): no tenant_id, no embedding_model choice, no tenant blob paths — S9
projects all three at apply. The COPY files are keyed by content (sha/hash), so they dedup corpus-wide and a
re-prepare is idempotent. Self-contained: imports no psycopg/azure/PyMuPDF.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import IngestConfig
from .discover import find_pdfs_for_dataset, find_media_for_dataset, default_store_dir
from .record import build_apply_record, ApplyRecord
from .media import build_media_record
from .store_reader import VectorStore
from .util import dump_json

APPLY_PLAN = "apply-plan.jsonl"
EMB_COPY = "embeddings.copy"
IMG_EMB_COPY = "image_embeddings.copy"
BLOBS_MANIFEST = "blobs.manifest.jsonl"
PREPARE_MANIFEST = "prepare-manifest.json"

_STATUSES = ("plan", "skip_status", "skip_csam", "no_sidecar", "error")


def _vector_literal(vec) -> str:
    """pgvector text form — MUST read back identically to ``indexing._vector_literal`` (repr(float(x)))."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def _record_to_plan(r: ApplyRecord) -> dict:
    """The apply-plan.jsonl line for one ingestable document (vectors live in the COPY files)."""
    return {
        "rel": r.rel,
        "source_kind": r.source_kind,
        "staged_pdf": r.staged_pdf,
        "staged_chunks": r.staged_chunks,
        "identifiers": r.identifiers,
        "document": r.document,
        "metadata": r.metadata,
        "media_metadata": r.media_metadata,
        "pages": r.pages,
        "asset_context": r.asset_context,
        "content_safety_findings": r.safety_findings,
        "content_shas": r.content_shas,
        "missing_shas": r.missing_shas,
        "source_withheld": r.source_withheld,
        "blobs": [{"artifact": b.artifact, "action": b.action, "source_path": b.source_path,
                   "content_type": b.content_type} for b in r.blobs],
        "image_occurrences": [{"content_hash": iv.content_hash, "artifact_kind": iv.artifact_kind,
                               "page_number": iv.page_number, "timestamp_ms": iv.timestamp_ms,
                               "frame_index": iv.frame_index, "image_artifact": iv.artifact,
                               "embedding_model": iv.embedding_model} for iv in r.image_vectors],
        "rollups": {"images_withheld": r.images_withheld, "images_embed": r.images_embed,
                    "blank_pages": r.blank_pages, "withheld_tiers": r.withheld_tiers},
    }


def _build_records(root: str, cfg: IngestConfig, store: VectorStore, workers: int,
                   *, include_media: bool = True) -> list[ApplyRecord]:
    """Build apply records for both tracks: PDFs (under IMAGES) + media (non-IMAGES siblings). Both share
    the dataset's text-vector store (media transcript text was embedded into the same store by Stage 7)."""
    thunks = [(lambda p=p: build_apply_record(p, root, cfg, store)) for p in find_pdfs_for_dataset(root)]
    if include_media:
        thunks += [(lambda a=p, k=k: build_media_record(a, k, root, cfg, store))
                   for p, k in find_media_for_dataset(root)]
    if workers > 1 and len(thunks) > 1:
        recs: list[ApplyRecord] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = [pool.submit(t) for t in thunks]
            for fut in as_completed(futs):
                recs.append(fut.result())
        recs.sort(key=lambda r: r.rel)
        return recs
    return [t() for t in thunks]


def _agg(records: list[ApplyRecord]) -> dict:
    statuses = {s: 0 for s in _STATUSES}
    tiers: dict = {}
    tenancy: set = set()
    errors: list = []
    csam_docs: list = []
    totals = {"parents_children_shas": 0, "vectors_present": 0, "vectors_missing": 0,
              "images_embed": 0, "images_withheld": 0, "blank_pages": 0, "blobs": 0}
    source_withheld_docs = 0
    for r in records:
        statuses[r.status] = statuses.get(r.status, 0) + 1
        if r.status == "skip_csam":
            csam_docs.append(r.rel)
        if r.status == "error":
            errors.append({"file": r.rel, "error": r.error})
        if r.status != "plan":
            continue
        totals["parents_children_shas"] += len(r.content_shas)
        totals["vectors_present"] += r.vectors_present
        totals["vectors_missing"] += r.vectors_missing
        totals["images_embed"] += r.images_embed
        totals["images_withheld"] += r.images_withheld
        totals["blank_pages"] += r.blank_pages
        totals["blobs"] += len(r.blobs)
        if r.source_withheld:
            source_withheld_docs += 1
        for t, n in r.withheld_tiers.items():
            tiers[t] = tiers.get(t, 0) + n
        if r.identifiers:
            tenancy.add((r.identifiers["case_key"], r.identifiers["dataset_key"]))
    return {"documents_to_ingest": statuses["plan"], "by_status": statuses, "totals": totals,
            "withheld_tiers": tiers, "source_withheld_docs": source_withheld_docs,
            "csam_documents_NOT_ingested": len(csam_docs), "csam_doc_list": sorted(csam_docs)[:500],
            "tenancy_pairs": sorted(map(list, tenancy)), "errors": errors[:500]}


def prepare(dataset_roots: list[str], cfg: IngestConfig, out_dir: str, *, store_override: str | None = None,
            workers: int = 1) -> dict:
    """Emit the apply-package for one or more dataset roots. Returns the prepare-manifest dict."""
    started = time.monotonic()
    os.makedirs(out_dir, exist_ok=True)
    plan_fp = open(os.path.join(out_dir, APPLY_PLAN), "w", encoding="utf-8")
    blobs_fp = open(os.path.join(out_dir, BLOBS_MANIFEST), "w", encoding="utf-8")
    emb_fp = open(os.path.join(out_dir, EMB_COPY), "w", encoding="utf-8")
    img_fp = open(os.path.join(out_dir, IMG_EMB_COPY), "w", encoding="utf-8")
    written_text: set = set()      # global content_sha dedup across all datasets
    written_img: set = set()       # global image content_hash dedup
    per_dataset: dict = {}
    all_records: list[ApplyRecord] = []
    try:
        for root in dataset_roots:
            root = os.path.abspath(root)
            store = VectorStore(store_override or default_store_dir(root), cfg.dimensions)
            records = _build_records(root, cfg, store, workers)
            name = os.path.basename(root)

            # text vectors: union of this dataset's present child shas, fetched once from its store
            want: set = set()
            for r in records:
                if r.status == "plan":
                    want.update(s for s in r.content_shas if s not in r.missing_shas)
            present = store.get_many(want) if (want and store.exists()) else {}
            for sha in sorted(present):
                if sha not in written_text:
                    written_text.add(sha)
                    emb_fp.write(f"{sha}\t{cfg.dimensions}\t{_vector_literal(present[sha])}\n")

            for r in records:
                if r.status != "plan":
                    continue
                plan_fp.write(json.dumps(_record_to_plan(r), ensure_ascii=False) + "\n")
                ident = r.identifiers
                for b in r.blobs:
                    blobs_fp.write(json.dumps({
                        "case_key": ident["case_key"], "dataset_key": ident["dataset_key"],
                        "document_name": ident["document_name"], "artifact": b.artifact,
                        "action": b.action, "source_path": b.source_path,
                        "content_type": b.content_type}, ensure_ascii=False) + "\n")
                for iv in r.image_vectors:
                    if iv.content_hash not in written_img:
                        written_img.add(iv.content_hash)
                        img_fp.write(f"{iv.content_hash}\t{iv.dim}\t{_vector_literal(iv.vector)}\n")

            per_dataset[name] = {"store": store.store_dir, "store_present": store.exists(),
                                 "docs_seen": len(records), **_agg(records)}
            all_records.extend(records)
    finally:
        for fp in (plan_fp, blobs_fp, emb_fp, img_fp):
            fp.close()

    corpus = _agg(all_records)
    manifest = {
        "tool": "text-first-extract-ingest", "mode": "prepare",
        "package_files": {"apply_plan": APPLY_PLAN, "embeddings_copy": EMB_COPY,
                          "image_embeddings_copy": IMG_EMB_COPY, "blobs_manifest": BLOBS_MANIFEST},
        "source_kind": cfg.source_kind, "embedding_model": cfg.embedding_model, "dimensions": cfg.dimensions,
        "config": cfg.echo(),
        "text_vectors_written": len(written_text), "image_vectors_written": len(written_img),
        "tenancy_policy": "ensure" if cfg.ensure_tenancy else "require_precreated",
        "datasets": per_dataset, "corpus": corpus,
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(out_dir, PREPARE_MANIFEST))
    return manifest
