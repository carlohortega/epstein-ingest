"""Folder walk, the realtime concurrency scheduler, the dry-run cost preview, and the Stage-4 run
manifest (Handoff §6/§8/§10).

Like Stage 3 this stage is I/O/network-bound, so it uses a bounded THREAD pool with a shared rate limiter
(client.py / ratelimit.py). The scheduler streams a bounded WINDOW of documents and submits one task per
ASSET (not per page) — pre-gate → vision verdict → embedding → content safety, all inside the worker — so
a 130k-asset corpus never buffers everything in memory and resume granularity is per asset. A document's
sidecar is written exactly once, when its last asset completes.

``find_pdfs_pruned`` prunes the Stage-2 per-PDF output dirs (``images``/``pages``/``artifacts`` and the
``<stem>/`` dirs) so the walk over a fully-staged corpus does not descend into hundreds of thousands of
PNG directories (the same fix Stage-2's escalate/purge passes use).
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..stage1.walk import dump_json, sidecar_path
from . import STAGE4_TOOL_NAME, STAGE4_TOOL_VERSION, PROMPT_VERSION
from ..stage3.client import estimate_cost_usd
from .config import Stage4Config
from .assets import process_asset, image_token_estimate
from .pregate import build_page_dark_lookup, classify_dark
from .process import open_sidecar, AssetState, finalize, process_document_sync
from . import faillog

_MANIFEST_NAME = "stage4-run-manifest.json"
_DEFAULT_PROGRESS_EVERY = 1000

_AGG_KEYS = ("assets", "captioned", "pregated_blank", "has_visual_content_true", "content_filtered",
             "safety_review", "embedded", "embed_errors", "content_safety_scanned",
             "content_safety_review", "errored", "prompt_tokens", "completion_tokens", "cached_tokens",
             "calls")
_ACTIONS = ("processed", "skipped_done", "skipped_status", "no_sidecar", "no_stage2", "no_assets", "error")


def find_pdfs_pruned(root: str) -> list[str]:
    """Recursively find ``*.pdf`` but prune the Stage-2 per-PDF output dirs, so a processed corpus does not
    descend into the PNG trees (mirrors stage2.escalate._find_pdfs_pruned)."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in ("images", "pages", "artifacts") and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _asset_task(state: AssetState, idx: int, cfg: Stage4Config, client, embedder, scanner):
    # process_asset is fault-isolated (returns an error outcome rather than raising), but guard anyway so
    # one unexpected exception never tears down the pool.
    try:
        return process_asset(idx, state.assets[idx], base_dir=state.base_dir, page_dark=state.page_dark,
                             cfg=cfg, client=client, embedder=embedder, scanner=scanner)
    except Exception as exc:  # pragma: no cover - defensive
        from .assets import AssetOutcome
        return AssetOutcome(index=idx, status="error", error_code="unexpected",
                            error_message=f"{type(exc).__name__}: {exc}")


def run(root: str, cfg: Stage4Config, generated_at: str, *, client, embedder, scanner, provenance: dict,
        force: bool, workers: int | None = None, progress_every: int = _DEFAULT_PROGRESS_EVERY,
        content_filter_log: str | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs_pruned(root)
    total = len(pdfs)
    workers = workers or cfg.concurrency
    cf_log_path = content_filter_log or faillog.default_path(root)
    embedding_model = provenance.get("embedding_model")

    agg = {k: 0 for k in _AGG_KEYS}
    actions = {a: 0 for a in _ACTIONS}
    errors: list[dict] = []
    missing: list[str] = []
    cf_entries: list[dict] = []
    seen = 0

    def consume(r: dict) -> None:
        nonlocal seen
        seen += 1
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "processed":
            for k in _AGG_KEYS:
                agg[k] += r[k]
            cf_entries.extend(r.get("content_filter_entries") or [])
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})
        elif r["action"] in ("no_sidecar", "no_stage2"):
            missing.append(r["rel"])
        if progress_every and seen % progress_every == 0:
            el = time.monotonic() - started
            rate = seen / el if el else 0.0
            eta = ((total - seen) / rate / 60.0) if rate else 0.0
            cost = estimate_cost_usd(agg["prompt_tokens"], agg["completion_tokens"],
                                     agg["cached_tokens"], cfg)
            print(f"  ... {seen}/{total} pdfs ({rate:.1f}/s, eta {eta:.0f}m) | "
                  f"assets={agg['assets']} vis_true={agg['has_visual_content_true']} "
                  f"pregated={agg['pregated_blank']} filtered={agg['content_filtered']} "
                  f"~${cost:.2f}", flush=True)

    if workers <= 1 or total <= 1:
        for p in pdfs:
            consume(process_document_sync(p, root, cfg, generated_at, provenance, client=client,
                                          embedder=embedder, scanner=scanner, force=force))
    else:
        _run_scheduler(pdfs, root, cfg, generated_at, client, embedder, scanner, embedding_model,
                       provenance, force, workers, consume)

    cf_total = faillog.merge_write(cf_log_path, cf_entries, generated_at)
    cost = estimate_cost_usd(agg["prompt_tokens"], agg["completion_tokens"], agg["cached_tokens"], cfg)
    manifest = {
        "tool": STAGE4_TOOL_NAME,
        "tool_version": STAGE4_TOOL_VERSION,
        "vision_model": cfg.model,
        "vision_deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "embedding_model": provenance.get("embedding_model"),
        "content_safety": provenance.get("content_safety"),
        "prompt_version": PROMPT_VERSION,
        "mode": "realtime",
        "generated_at_utc": generated_at,
        "root": root,
        "pdfs_seen": total,
        "pdfs_processed": actions["processed"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_assets": actions["no_assets"],
        "pdfs_no_sidecar": actions["no_sidecar"],
        "pdfs_no_stage2": actions["no_stage2"],
        "pdfs_errored": actions["error"],
        "counts": {k: agg[k] for k in (
            "assets", "captioned", "pregated_blank", "has_visual_content_true", "content_filtered",
            "safety_review", "embedded", "embed_errors", "content_safety_scanned",
            "content_safety_review", "errored")},
        "usage": {
            "prompt_tokens": agg["prompt_tokens"],
            "completion_tokens": agg["completion_tokens"],
            "cached_tokens": agg["cached_tokens"],
            "calls": agg["calls"],
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


def _run_scheduler(pdfs, root, cfg, generated_at, client, embedder, scanner, embedding_model,
                   provenance, force, workers, consume):
    window = max(workers * 2, workers + 1)
    docs = iter(pdfs)
    inflight: dict = {}                       # future -> (state, idx)
    open_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def admit() -> None:
            nonlocal open_count
            while open_count < window:
                p = next(docs, None)
                if p is None:
                    return
                rel, sidecar, skip = open_sidecar(p, root, force=force)
                if skip is not None:
                    consume(skip)
                    continue
                state = AssetState(rel, sidecar_path(p), sidecar, cfg, force=force)
                open_count += 1
                if state.remaining == 0:           # all assets already done -> just (re)write the block
                    consume(finalize(state, state.run_usage, generated_at=generated_at,
                                     provenance=provenance))
                    open_count -= 1
                    continue
                for idx in state.to_call:
                    inflight[pool.submit(_asset_task, state, idx, cfg, client, embedder, scanner)] = \
                        (state, idx)

        admit()
        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                state, _idx = inflight.pop(fut)
                state.record(fut.result(), embedding_model)
                if state.remaining == 0:
                    consume(finalize(state, state.run_usage, generated_at=generated_at,
                                     provenance=provenance))
                    open_count -= 1
            admit()


def dry_run(root: str, cfg: Stage4Config, *, force: bool = False) -> dict:
    """Project eligible assets / tokens / cost from existing sidecars with NO API calls (Handoff §10).

    Conservative: applies only the METRIC-based pre-gate (image-page dark_coverage, zero I/O). The
    PNG-measured pre-gate (escalated pages + artifacts) further reduces the paid set at run time, so the
    real run is cheaper than this preview, never more expensive."""
    root = os.path.abspath(root)
    docs = assets = pregated = vision = est_in = est_out = 0
    for p in find_pdfs_pruned(root):
        try:
            with open(sidecar_path(p), "r", encoding="utf-8") as f:
                sc = json.load(f)
        except Exception:
            continue
        if sc.get("status") != "ok" or not sc.get("stage2"):
            continue
        ia = sc.get("image_assets") or []
        if not ia:
            continue
        if sc.get("stage4") and not force:
            continue
        page_dark = build_page_dark_lookup(sc)
        doc_vision = 0
        for a in ia:
            if not force and "has_visual_content" in a:
                continue
            assets += 1
            dark = page_dark.get(int(a["page_number"])) if (a.get("kind") == "page"
                                                            and a.get("page_number") is not None) else None
            cls, _ = classify_dark(dark, cfg) if cfg.pregate else (None, None)
            if cls is not None:
                pregated += 1
                continue
            vision += 1
            doc_vision += 1
            est_in += image_token_estimate(a, cfg) + cfg.dryrun_prompt_overhead_tokens
            est_out += cfg.dryrun_avg_output_tokens
        if doc_vision:
            docs += 1
    realtime = estimate_cost_usd(est_in, est_out, 0, cfg, batch=False)
    batch = estimate_cost_usd(est_in, est_out, 0, cfg, batch=True)
    return {
        "eligible_assets": assets,
        "pregated_blank_metric": pregated,
        "vision_calls": vision,
        "documents_with_vision": docs,
        "estimated_prompt_tokens": est_in,
        "estimated_completion_tokens": est_out,
        "estimated_cost_usd_realtime": round(realtime, 4),
        "estimated_cost_usd_batch": round(batch, 4),
    }
