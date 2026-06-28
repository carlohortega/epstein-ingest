"""Stage-8 driver: the ``--dry-run`` write-plan projection (offline, no DB/Storage) and its run manifest.
This is the read-only projection only — S8 ``--prepare`` lives in ``emit.py`` and the live S9 ``--apply``
lives in ``apply.py`` (the only live-writing entry point).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config import IngestConfig
from .discover import find_pdfs_for_dataset, default_store_dir
from .plan import plan_document, DocPlan
from .store_reader import VectorStore
from .util import dump_json

_DRY_PLAN_NAME = "ingest-dry-run-plan.json"

_AGG = ("pages", "blank_pages", "parents", "children", "unique_sha", "vectors_present", "vectors_missing",
        "image_assets", "images_embed", "images_withheld")
_STATUSES = ("plan", "skip_status", "skip_csam", "no_sidecar", "no_chunks", "error")


def _agg_plans(plans: list[DocPlan]) -> dict:
    totals = {k: 0 for k in _AGG}
    statuses = {s: 0 for s in _STATUSES}
    tiers: dict = {}
    tenancy: set = set()
    errors: list[dict] = []
    csam_docs: list[str] = []
    csam_assets = 0
    source_withheld_docs = 0
    for p in plans:
        statuses[p.status] = statuses.get(p.status, 0) + 1
        csam_assets += p.csam_assets
        if p.status == "skip_csam":
            csam_docs.append(p.rel)                       # NOT ingested — preserve + report upstream
        if p.status == "error":
            errors.append({"file": p.rel, "error": p.error})
        if p.status != "plan":
            continue
        for k in _AGG:
            totals[k] += getattr(p, k)
        if p.source_withheld:
            source_withheld_docs += 1
        for t, n in p.withheld_tiers.items():
            tiers[t] = tiers.get(t, 0) + n
        if p.identifiers:
            tenancy.add((p.identifiers["case_key"], p.identifiers["dataset_key"]))
    return {"documents_to_ingest": statuses["plan"], "by_status": statuses, "totals": totals,
            "withheld_tiers": tiers, "source_withheld_docs": source_withheld_docs,
            "csam_documents_NOT_ingested": len(csam_docs), "csam_assets": csam_assets,
            "csam_doc_list": sorted(csam_docs)[:200],
            "tenancy_pairs": sorted(map(list, tenancy)), "errors": errors[:200]}


def dry_run(dataset_roots: list[str], cfg: IngestConfig, *, store_override: str | None = None,
            workers: int = 1, write_plan_to: str | None = None) -> dict:
    """Project the full write plan for one or more dataset roots — NO DB/Storage writes. Surfaces the
    critical ``vectors_missing`` count (a non-zero value means a child's content_sha isn't in the embed
    store, so the live guard provider WOULD raise — fix before a real run)."""
    started = time.monotonic()
    per_dataset = {}
    all_plans: list[DocPlan] = []
    for root in dataset_roots:
        root = os.path.abspath(root)
        store = VectorStore(store_override or default_store_dir(root), cfg.dimensions)
        pdfs = find_pdfs_for_dataset(root)
        plans: list[DocPlan] = []
        if workers > 1 and len(pdfs) > 1:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = [pool.submit(plan_document, p, root, cfg, store) for p in pdfs]
                for fut in as_completed(futs):
                    plans.append(fut.result())
        else:
            plans = [plan_document(p, root, cfg, store) for p in pdfs]
        per_dataset[os.path.basename(root)] = {"store": store.store_dir, "store_present": store.exists(),
                                               "pdfs_seen": len(pdfs), **_agg_plans(plans)}
        all_plans.extend(plans)

    manifest = {
        "tool": "text-first-extract-ingest", "mode": "dry-run",
        "source_kind": cfg.source_kind, "embedding_model": cfg.embedding_model, "dimensions": cfg.dimensions,
        "config": cfg.echo(),
        "datasets": per_dataset,
        "corpus": _agg_plans(all_plans),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    if write_plan_to:
        dump_json(manifest, write_plan_to)
    return manifest
