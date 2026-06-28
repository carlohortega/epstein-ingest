"""Per-document PLAN (offline, no DB/Storage) — what ingest would write for one staged PDF, with the §4a
safety outcomes (CSAM-suspected → whole-doc NOT ingested; withhold → asset removed + placeholder page) and
which child vectors are present in the embed store. Drives ``--dry-run`` and (later) the live loader.
Identifiers match the live chunks exactly: ``case_key`` is the constant case ('epstein'), ``dataset_key`` the
``DataSet-NN`` path segment, ``document_name`` the stem (verified against the chunks' context_prefix).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .config import IngestConfig
from .discover import sidecar_path, chunks_path, derive_identifiers, IdentifierError
from .safety import image_asset_verdict, page_is_blank


@dataclass
class DocPlan:
    rel: str
    status: str = "plan"        # plan | skip_status | skip_csam | no_sidecar | error
    error: str | None = None
    identifiers: dict = field(default_factory=dict)
    pages: int = 0
    blank_pages: int = 0
    parents: int = 0
    children: int = 0
    unique_sha: int = 0
    vectors_present: int = 0
    vectors_missing: int = 0
    image_assets: int = 0
    images_embed: int = 0
    images_withheld: int = 0          # §4a withhold (asset removed, placeholder page)
    csam_assets: int = 0              # CSAM-suspected -> whole doc NOT ingested (status=skip_csam)
    source_withheld: bool = False     # any unit removed -> documents.source_withheld=true
    withheld_tiers: dict = field(default_factory=dict)


def plan_document(pdf_path: str, root: str, cfg: IngestConfig, store) -> DocPlan:
    rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
    p = DocPlan(rel)
    try:
        with open(sidecar_path(pdf_path), "r", encoding="utf-8") as f:
            sc = json.load(f)
    except FileNotFoundError:
        p.status = "no_sidecar"; return p
    except Exception as exc:
        p.status = "error"; p.error = f"bad_sidecar: {type(exc).__name__}"; return p
    if sc.get("status") != "ok":
        p.status = "skip_status"; return p

    try:
        document_name, case_key, dataset_key = derive_identifiers(pdf_path, cfg.case_key)
    except IdentifierError as exc:
        p.status = "error"; p.error = f"no_identifiers: {str(exc)[:120]}"; return p
    p.identifiers = {"document_name": document_name, "case_key": case_key, "dataset_key": dataset_key}

    # --- §4a image-asset classification (CSAM short-circuits the whole document) ---
    for a in (sc.get("image_assets") or []):
        p.image_assets += 1
        v = image_asset_verdict(a, threshold=cfg.content_safety_threshold,
                                withhold_on_missing=cfg.redact_on_missing_verdict)
        if v.csam:
            p.csam_assets += 1
        elif v.withhold:
            p.images_withheld += 1
            t = v.tier or "unknown"
            p.withheld_tiers[t] = p.withheld_tiers.get(t, 0) + 1
        elif a.get("has_visual_content") is True and a.get("embedding_ref"):
            p.images_embed += 1
    if p.csam_assets:
        p.status = "skip_csam"           # WHOLE document not ingested; preserve + report upstream
        return p
    p.source_withheld = p.images_withheld > 0

    pages = sc.get("pages") or []
    p.pages = len(pages)
    p.blank_pages = sum(1 for pg in pages if page_is_blank(pg))

    # chunks + which child vectors are in the store
    shas: set[str] = set()
    try:
        cpath = chunks_path(pdf_path)
        if os.path.exists(cpath):
            with open(cpath, "r", encoding="utf-8") as f:
                parents = json.load(f)
            p.parents = len(parents)
            for par in parents:
                for c in (par.get("children") or []):
                    p.children += 1
                    s = c.get("content_sha")
                    if s:
                        shas.add(s)
    except Exception as exc:
        p.status = "error"; p.error = f"bad_chunks: {type(exc).__name__}"; return p

    p.unique_sha = len(shas)
    if shas and store.exists():
        present = store.get_many(shas)
        p.vectors_present = len(present)
        p.vectors_missing = len(shas) - len(present)
    else:
        p.vectors_missing = len(shas)
    return p
