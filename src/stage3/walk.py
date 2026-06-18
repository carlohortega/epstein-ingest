"""Folder walk, the realtime concurrency scheduler, the dry-run cost preview, and the Stage-3 run
manifest (Handoff §8/§9/§11).

Unlike Stages 1–2 (CPU-bound process pool) this stage is I/O/network-bound, so it uses a bounded THREAD
pool with a shared rate limiter (client.py / ratelimit.py). The scheduler streams a bounded WINDOW of
documents and submits only leaf work to the pool — one task per page call, then one task per document's
summary reduce — so a worker thread never waits on the pool (no nested-pool deadlock) and a 300k-page
corpus never buffers all tasks/results in memory. Pages from different documents pipeline freely; a
document's sidecar is written exactly once, when its reduce completes.

Batch mode (--batch) is a different transport entirely and lives in batch.py.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

from ..stage1.walk import find_pdfs, dump_json, sidecar_path
from . import STAGE3_TOOL_NAME, STAGE3_TOOL_VERSION, PROMPT_VERSION
from .config import Stage3Config
from .client import Stage3CallError, estimate_cost_usd
from .summarize import route_page, summarize_page
from .process import open_sidecar, DocState, run_doc_reduce, finalize
from . import faillog

_MANIFEST_NAME = "stage3-run-manifest.json"
_DEFAULT_PROGRESS_EVERY = 2000

_AGG_KEYS = ("pages_summarized", "pages_skipped", "pages_errored", "low_confidence", "safety_review",
             "content_filtered", "doc_summary_calls", "prompt_tokens", "completion_tokens",
             "cached_tokens", "calls")
_ACTIONS = ("summarized", "skipped_done", "skipped_status", "no_sidecar", "no_stage2", "error")


def _page_task(client, state: DocState, idx: int, cfg: Stage3Config):
    text = (state.pages[idx].get("text") or {}).get("content") or ""
    try:
        return summarize_page(client, text, cfg)
    except Stage3CallError as exc:
        return exc


def _reduce_task(client, state: DocState):
    try:
        return run_doc_reduce(state, client)
    except Stage3CallError as exc:
        return exc


def run(root: str, cfg: Stage3Config, generated_at: str, *, client, provenance: dict, force: bool,
        workers: int | None = None, mode: str = "realtime",
        progress_every: int = _DEFAULT_PROGRESS_EVERY, content_filter_log: str | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs(root)
    total = len(pdfs)
    workers = workers or cfg.concurrency
    cf_log_path = content_filter_log or faillog.default_path(root)

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
        if r["action"] == "summarized":
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
                                     agg["cached_tokens"], cfg, batch=(mode == "batch"))
            print(f"  ... {seen}/{total} pdfs ({rate:.1f}/s, eta {eta:.0f}m) | "
                  f"summarized={actions['summarized']} pages={agg['pages_summarized']} "
                  f"review={agg['safety_review']} filtered={agg['content_filtered']} "
                  f"~${cost:.2f}", flush=True)

    if workers <= 1 or total <= 1:
        from .process import process_document_sync
        for p in pdfs:
            consume(process_document_sync(p, root, client, cfg, generated_at, provenance,
                                          force=force, mode=mode))
    else:
        _run_scheduler(pdfs, root, cfg, generated_at, client, provenance, force, mode, workers, consume)

    cf_total = faillog.merge_write(cf_log_path, cf_entries, generated_at)
    cost = estimate_cost_usd(agg["prompt_tokens"], agg["completion_tokens"], agg["cached_tokens"],
                             cfg, batch=(mode == "batch"))
    manifest = {
        "tool": STAGE3_TOOL_NAME,
        "tool_version": STAGE3_TOOL_VERSION,
        "model": cfg.model,
        "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "prompt_version": PROMPT_VERSION,
        "mode": mode,
        "generated_at_utc": generated_at,
        "root": root,
        "pdfs_seen": total,
        "pdfs_summarized": actions["summarized"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_sidecar": actions["no_sidecar"],
        "pdfs_no_stage2": actions["no_stage2"],
        "pdfs_errored": actions["error"],
        "pages_summarized": agg["pages_summarized"],
        "pages_skipped": agg["pages_skipped"],
        "pages_errored": agg["pages_errored"],
        "low_confidence": agg["low_confidence"],
        "safety_review": agg["safety_review"],
        "content_filtered": agg["content_filtered"],
        "doc_summary_calls": agg["doc_summary_calls"],
        "usage": {
            "prompt_tokens": agg["prompt_tokens"],
            "completion_tokens": agg["completion_tokens"],
            "cached_tokens": agg["cached_tokens"],
            "calls": agg["calls"],
            "estimated_cost_usd": round(cost, 6),
            "batch": mode == "batch",
        },
        "content_filter_log": cf_log_path if cf_total else None,
        "content_filter_failures": cf_total,
        "errors": sorted(errors, key=lambda e: e["file"]),
        "skipped_missing_upstream": sorted(missing),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest


def _run_scheduler(pdfs, root, cfg, generated_at, client, provenance, force, mode, workers, consume):
    window = max(workers * 2, workers + 1)   # documents open concurrently (bounded memory)
    docs = iter(pdfs)
    inflight: dict = {}                       # future -> (state, ("page", idx) | ("reduce",))
    open_count = 0

    def fin(state, reduce_or_err) -> None:
        nonlocal open_count
        r = reduce_or_err
        if isinstance(r, Stage3CallError):
            state.sidecar["document_summary_error"] = {"code": r.code, "message": str(r)[:300]}
            r = None
        consume(finalize(state, r, generated_at=generated_at, provenance=provenance, mode=mode))
        open_count -= 1

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
                state = DocState(rel, sidecar_path(p), sidecar, cfg, force=force)
                open_count += 1
                if state.remaining == 0:                       # nothing to call -> straight to reduce
                    inflight[pool.submit(_reduce_task, client, state)] = (state, ("reduce",))
                else:
                    for idx in state.to_call:
                        inflight[pool.submit(_page_task, client, state, idx, cfg)] = (state, ("page", idx))

        admit()
        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                state, tag = inflight.pop(fut)
                if tag[0] == "page":
                    state.record_page_result(tag[1], fut.result())
                    if state.remaining == 0:
                        inflight[pool.submit(_reduce_task, client, state)] = (state, ("reduce",))
                else:
                    fin(state, fut.result())
            admit()


def dry_run(root: str, cfg: Stage3Config) -> dict:
    """Project tokens/cost from existing sidecars with NO API calls (Handoff §11): eligible-page count ×
    per-page token estimate (from char_count, capped at max_input_chars) + one reduce per eligible doc."""
    root = os.path.abspath(root)
    pages = docs_with_text = est_in = est_out = 0
    for p in find_pdfs(root):
        try:
            import json
            with open(sidecar_path(p), "r", encoding="utf-8") as f:
                sc = json.load(f)
        except Exception:
            continue
        if sc.get("status") != "ok" or not sc.get("stage2"):
            continue
        page_sum_tokens = 0
        eligible = 0
        for pg in (sc.get("pages") or []):
            if route_page(pg, cfg)[0] != "summarize":
                continue
            eligible += 1
            chars = min(int((pg.get("text") or {}).get("char_count") or 0), cfg.max_input_chars)
            est_in += chars // 4
            est_out += cfg.dryrun_avg_output_tokens
            page_sum_tokens += cfg.dryrun_avg_output_tokens
        pages += eligible
        if eligible:
            docs_with_text += 1
            est_in += page_sum_tokens                 # the doc-summary reduce reads the page summaries
            est_out += cfg.dryrun_avg_output_tokens
    realtime = estimate_cost_usd(est_in, est_out, 0, cfg, batch=False)
    batch = estimate_cost_usd(est_in, est_out, 0, cfg, batch=True)
    return {
        "eligible_pages": pages,
        "eligible_documents": docs_with_text,
        "estimated_prompt_tokens": est_in,
        "estimated_completion_tokens": est_out,
        "estimated_cost_usd_realtime": round(realtime, 4),
        "estimated_cost_usd_batch": round(batch, 4),
    }
