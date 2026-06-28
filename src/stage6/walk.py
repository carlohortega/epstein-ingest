"""Folder walk, the concurrency scheduler, the dry-run preview, and the Stage-6 run manifest (Handoff
§7/§8). Stage 6's unit of work is the DOCUMENT (read sidecar -> chunk -> write chunks.json), I/O-bound at
these doc sizes, so it uses a bounded THREAD pool that streams a window of per-doc tasks (a 500k-doc corpus
never buffers all futures). Each task opens, gates, chunks, and writes its own sidecar + sibling artifact;
a bad doc is isolated to its task. No model client, no API calls, no rate limiter (chunk-only).

``find_pdfs_pruned`` prunes the Stage-2 per-PDF output dirs (``images``/``pages``/``artifacts`` and the
``<stem>/`` dirs) so the walk over a fully-staged corpus does not descend into the PNG trees (mirrors
``src/stage5/walk.find_pdfs_pruned``).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..stage1.walk import dump_json, sidecar_path
from . import STAGE6_TOOL_NAME, STAGE6_TOOL_VERSION, TEXT_SOURCE, IMAGE_PAGE_POLICY, EMBEDDING_MODEL
from .chunk import chunk_document, CHUNKER_PARAMS, IdentifierError
from .config import Stage6Config
from .process import process_document_sync

_MANIFEST_NAME = "stage6-run-manifest.json"
_DEFAULT_PROGRESS_EVERY = 5000

_ACTIONS = ("chunked", "skipped_done", "skipped_status", "no_sidecar", "no_stage1", "error")


def find_pdfs_pruned(root: str) -> list[str]:
    """Recursively find ``*.pdf`` but prune the Stage-2 per-PDF output dirs, so a processed corpus does not
    descend into the PNG trees (mirrors src/stage5/walk.find_pdfs_pruned)."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in ("images", "pages", "artifacts") and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _doc_task(pdf_path, root, cfg, generated_at, force):
    # process_document_sync is fault-isolated (errors become an "error" result rather than raising), but
    # guard anyway so one unexpected exception never tears down the pool.
    try:
        return process_document_sync(pdf_path, root, cfg, generated_at, force=force)
    except Exception as exc:  # pragma: no cover - defensive
        rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
        from .process import _result
        return _result("error", rel, error_code="unexpected", message=f"{type(exc).__name__}: {exc}")


def run(root: str, cfg: Stage6Config, generated_at: str, *, force: bool, workers: int | None = None,
        progress_every: int = _DEFAULT_PROGRESS_EVERY) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs_pruned(root)
    total = len(pdfs)
    workers = workers or cfg.concurrency

    actions = {a: 0 for a in _ACTIONS}
    counts = {"pages_chunked": 0, "parents": 0, "children": 0, "skipped_pages": 0, "empty_docs": 0}
    errors: list[dict] = []
    missing: list[str] = []
    seen = 0

    def consume(r: dict) -> None:
        nonlocal seen
        seen += 1
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "chunked":
            for k in ("pages_chunked", "parents", "children", "skipped_pages"):
                counts[k] += r[k]
            if r["empty"]:
                counts["empty_docs"] += 1
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})
        elif r["action"] in ("no_sidecar", "no_stage1"):
            missing.append(r["rel"])
        if progress_every and seen % progress_every == 0:
            el = time.monotonic() - started
            rate = seen / el if el else 0.0
            eta = ((total - seen) / rate / 60.0) if rate else 0.0
            print(f"  ... {seen}/{total} pdfs ({rate:.1f}/s, eta {eta:.0f}m) | "
                  f"chunked={actions['chunked']} (empty={counts['empty_docs']}) "
                  f"parents={counts['parents']} children={counts['children']} "
                  f"errored={actions['error']}", flush=True)

    if workers <= 1 or total <= 1:
        for p in pdfs:
            consume(process_document_sync(p, root, cfg, generated_at, force=force))
    else:
        _run_scheduler(pdfs, root, cfg, generated_at, force, workers, consume)

    manifest = {
        "tool": STAGE6_TOOL_NAME,
        "tool_version": STAGE6_TOOL_VERSION,
        "chunker_source": "svkb_pipeline.chunking.chunk_pages",
        "chunker_params": CHUNKER_PARAMS,
        "embedding_model": EMBEDDING_MODEL,
        "text_source": TEXT_SOURCE,
        "image_page_policy": IMAGE_PAGE_POLICY,
        "generated_at_utc": generated_at,
        "root": root,
        "pdfs_seen": total,
        "pdfs_chunked": actions["chunked"],
        "pdfs_empty": counts["empty_docs"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_sidecar": actions["no_sidecar"],
        "pdfs_no_stage1": actions["no_stage1"],
        "pdfs_errored": actions["error"],
        "counts": {
            "docs": actions["chunked"],
            "empty_docs": counts["empty_docs"],
            "pages_chunked": counts["pages_chunked"],
            "skipped_pages": counts["skipped_pages"],
            "parents": counts["parents"],
            "children": counts["children"],
            "errored": actions["error"],
        },
        "errors": sorted(errors, key=lambda e: e["file"]),
        "skipped_missing_upstream": sorted(missing),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest


def _run_scheduler(pdfs, root, cfg, generated_at, force, workers, consume):
    window = max(workers * 2, workers + 1)    # documents in flight (bounded memory)
    docs = iter(pdfs)
    inflight: dict = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def admit() -> None:
            while len(inflight) < window:
                p = next(docs, None)
                if p is None:
                    return
                inflight[pool.submit(_doc_task, p, root, cfg, generated_at, force)] = p

        admit()
        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                inflight.pop(fut)
                consume(fut.result())
            admit()


def dry_run(root: str, cfg: Stage6Config, *, force: bool = False) -> dict:
    """Project pages/parents/children to be produced from existing sidecars with NO API calls (Handoff
    §10.8). Since chunking IS the (free, deterministic) work, the dry-run runs the chunker for exact counts
    but writes nothing — no sidecar, no chunks.json."""
    root = os.path.abspath(root)
    eligible = empty = pages_chunked = skipped_pages = parents = children = errored = 0
    for p in find_pdfs_pruned(root):
        try:
            with open(sidecar_path(p), "r", encoding="utf-8") as f:
                sc = json.load(f)
        except Exception:
            continue
        if sc.get("status") != "ok" or not sc.get("pages"):
            continue
        if sc.get("stage6") and not force:
            continue
        eligible += 1
        try:
            _chunks, _ids, c = chunk_document(p, sc, case_key=cfg.case_key, dataset_key=cfg.dataset_key)
        except IdentifierError:
            errored += 1
            continue
        pages_chunked += c["pages_chunked"]
        skipped_pages += c["skipped_pages"]
        parents += c["parents"]
        children += c["children"]
        if c["parents"] == 0:
            empty += 1
    return {
        "eligible_documents": eligible,
        "empty_documents": empty,
        "identifier_errors": errored,
        "pages_chunked": pages_chunked,
        "skipped_pages": skipped_pages,
        "parents": parents,
        "children": children,
    }
