"""Folder walk, the realtime concurrency scheduler, the dry-run cost preview, and the Stage-5 run
manifest (Handoff §7/§8/§10).

Like Stage 3/4 this stage is I/O/network-bound, so it uses a bounded THREAD pool with a shared rate
limiter (the reused Stage-3 client.py / ratelimit.py). Stage 5's unit of work is the DOCUMENT — its only
call is the document reduce (0 calls for a promote/no-content doc, 1 logical reduce for a fused/image-only
doc, which may itself fan in hierarchically) — so the scheduler simply submits one self-contained task per
document and streams a bounded window of them (a 500k-doc corpus never buffers all futures in memory). Each
task opens, routes, finalizes, and writes its own sidecar; a bad doc / API error is isolated to that task.

``find_pdfs_pruned`` prunes the Stage-2 per-PDF output dirs (``images``/``pages``/``artifacts`` and the
``<stem>/`` dirs) so the walk over a fully-staged corpus does not descend into the PNG trees (mirrors
``src/stage4/walk.find_pdfs_pruned``).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..stage1.walk import dump_json, sidecar_path
from ..stage3.client import estimate_cost_usd
from . import STAGE5_TOOL_NAME, STAGE5_TOOL_VERSION, PROMPT_VERSION
from .config import Stage5Config
from .fuse import route_document, build_fusion_inputs, PROMOTED, FUSED, IMAGE_ONLY, NO_CONTENT
from .process import open_sidecar, process_document_sync
from ..stage3 import faillog

_MANIFEST_NAME = "stage5-run-manifest.json"
_DEFAULT_PROGRESS_EVERY = 2000

_SOURCES = (PROMOTED, FUSED, IMAGE_ONLY, NO_CONTENT)
_ACTIONS = ("finalized", "skipped_done", "skipped_status", "no_sidecar", "no_stage3", "no_stage4", "error")


def find_pdfs_pruned(root: str) -> list[str]:
    """Recursively find ``*.pdf`` but prune the Stage-2 per-PDF output dirs, so a processed corpus does not
    descend into the PNG trees (mirrors src/stage4/walk.find_pdfs_pruned)."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in ("images", "pages", "artifacts") and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _doc_task(pdf_path, root, client, cfg, generated_at, provenance, force):
    # process_document_sync is fault-isolated (a generic reduce error becomes an "error" result rather than
    # raising), but guard anyway so one unexpected exception never tears down the pool.
    try:
        return process_document_sync(pdf_path, root, client, cfg, generated_at, provenance, force=force)
    except Exception as exc:  # pragma: no cover - defensive
        rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
        from .process import _result
        return _result("error", rel, error_code="unexpected", message=f"{type(exc).__name__}: {exc}")


def run(root: str, cfg: Stage5Config, generated_at: str, *, client, provenance: dict, force: bool,
        workers: int | None = None, mode: str = "realtime",
        progress_every: int = _DEFAULT_PROGRESS_EVERY, content_filter_log: str | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs_pruned(root)
    total = len(pdfs)
    workers = workers or cfg.concurrency
    cf_log_path = content_filter_log or faillog.default_path(root)

    actions = {a: 0 for a in _ACTIONS}
    sources = {s: 0 for s in _SOURCES}
    counts = {"quarantined": 0, "safety_review": 0, "calls": 0,
              "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    errors: list[dict] = []
    missing: list[str] = []
    cf_entries: list[dict] = []
    seen = 0

    def consume(r: dict) -> None:
        nonlocal seen
        seen += 1
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "finalized":
            if r["source"] in sources:
                sources[r["source"]] += 1
            if r["content_filtered"]:
                counts["quarantined"] += 1
            if r["safety_review"]:
                counts["safety_review"] += 1
            for k in ("calls", "prompt_tokens", "completion_tokens", "cached_tokens"):
                counts[k] += r[k]
            cf_entries.extend(r.get("content_filter_entries") or [])
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})
        elif r["action"] in ("no_sidecar", "no_stage3", "no_stage4"):
            missing.append(r["rel"])
        if progress_every and seen % progress_every == 0:
            el = time.monotonic() - started
            rate = seen / el if el else 0.0
            eta = ((total - seen) / rate / 60.0) if rate else 0.0
            cost = estimate_cost_usd(counts["prompt_tokens"], counts["completion_tokens"],
                                     counts["cached_tokens"], cfg, batch=False)
            print(f"  ... {seen}/{total} pdfs ({rate:.1f}/s, eta {eta:.0f}m) | "
                  f"promoted={sources[PROMOTED]} fused={sources[FUSED]} "
                  f"image_only={sources[IMAGE_ONLY]} no_content={sources[NO_CONTENT]} "
                  f"quarantined={counts['quarantined']} ~${cost:.2f}", flush=True)

    if workers <= 1 or total <= 1:
        for p in pdfs:
            consume(process_document_sync(p, root, client, cfg, generated_at, provenance,
                                          force=force, mode=mode))
    else:
        _run_scheduler(pdfs, root, cfg, generated_at, client, provenance, force, workers, consume)

    cf_total = faillog.merge_write(cf_log_path, cf_entries, generated_at)
    cost = estimate_cost_usd(counts["prompt_tokens"], counts["completion_tokens"],
                             counts["cached_tokens"], cfg, batch=False)
    manifest = {
        "tool": STAGE5_TOOL_NAME,
        "tool_version": STAGE5_TOOL_VERSION,
        "model": cfg.model,
        "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "prompt_version": PROMPT_VERSION,
        "mode": mode,
        "generated_at_utc": generated_at,
        "root": root,
        "pdfs_seen": total,
        "pdfs_finalized": actions["finalized"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_sidecar": actions["no_sidecar"],
        "pdfs_no_stage3": actions["no_stage3"],
        "pdfs_no_stage4": actions["no_stage4"],
        "pdfs_errored": actions["error"],
        "counts": {
            "docs": actions["finalized"],
            "promoted": sources[PROMOTED],
            "fused": sources[FUSED],
            "image_only": sources[IMAGE_ONLY],
            "no_content": sources[NO_CONTENT],
            "quarantined": counts["quarantined"],
            "safety_review": counts["safety_review"],
            "calls": counts["calls"],
            "errored": actions["error"],
        },
        "usage": {
            "prompt_tokens": counts["prompt_tokens"],
            "completion_tokens": counts["completion_tokens"],
            "cached_tokens": counts["cached_tokens"],
            "calls": counts["calls"],
            "estimated_cost_usd": round(cost, 6),
            "batch": False,
        },
        "content_filter_log": cf_log_path if cf_total else None,
        "content_filter_failures": cf_total,
        "errors": sorted(errors, key=lambda e: e["file"]),
        "skipped_missing_upstream": sorted(missing),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest


def _run_scheduler(pdfs, root, cfg, generated_at, client, provenance, force, workers, consume):
    window = max(workers * 2, workers + 1)    # documents in flight (bounded memory)
    docs = iter(pdfs)
    inflight: dict = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def admit() -> None:
            while len(inflight) < window:
                p = next(docs, None)
                if p is None:
                    return
                inflight[pool.submit(_doc_task, p, root, client, cfg, generated_at, provenance,
                                     force)] = p

        admit()
        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                inflight.pop(fut)
                consume(fut.result())
            admit()


def dry_run(root: str, cfg: Stage5Config, *, force: bool = False) -> dict:
    """Project the promote / fuse / image-only / no-content breakdown + nano $ from existing sidecars with
    NO API calls (Handoff §10.8). Promote and no-content cost nothing; each fused/image-only doc is ONE
    reduce whose input tokens are estimated from the interleaved item characters (~4 chars/token)."""
    root = os.path.abspath(root)
    breakdown = {PROMOTED: 0, FUSED: 0, IMAGE_ONLY: 0, NO_CONTENT: 0}
    eligible = reduce_calls = est_in = est_out = 0
    for p in find_pdfs_pruned(root):
        try:
            with open(sidecar_path(p), "r", encoding="utf-8") as f:
                sc = json.load(f)
        except Exception:
            continue
        if sc.get("status") != "ok" or not sc.get("stage3"):
            continue
        if (sc.get("image_assets") or []) and not sc.get("stage4"):
            continue
        if sc.get("stage5") and not force:
            continue
        eligible += 1
        route, _kept, _has_text = route_document(sc)
        if route in (FUSED, IMAGE_ONLY):
            items, _ct, _cv = build_fusion_inputs(sc)
            if not items:
                route = NO_CONTENT
            else:
                reduce_calls += 1
                est_in += sum(len(s) + 1 for s in items) // 4
                est_out += cfg.dryrun_avg_output_tokens
        breakdown[route] += 1
    realtime = estimate_cost_usd(est_in, est_out, 0, cfg, batch=False)
    return {
        "eligible_documents": eligible,
        "promoted": breakdown[PROMOTED],
        "fused": breakdown[FUSED],
        "image_only": breakdown[IMAGE_ONLY],
        "no_content": breakdown[NO_CONTENT],
        "reduce_calls": reduce_calls,
        "estimated_prompt_tokens": est_in,
        "estimated_completion_tokens": est_out,
        "estimated_cost_usd_realtime": round(realtime, 4),
    }
