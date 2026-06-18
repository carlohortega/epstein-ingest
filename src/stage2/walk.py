"""Folder walk, idempotency/resume, bounded parallelism, and the Stage-2 run manifest (Handoff §8).

One PDF per worker process (PyMuPDF doc handles are not thread-safe; pages within a PDF are rendered
sequentially inside the worker). A crash mid-run loses at most the in-flight PDFs — completed PDFs carry
a ``stage2`` block and are skipped on the next pass.

Scale note: corpora here run to *hundreds of thousands* of PDFs (DataSet-11 ≈ 331k). Submitting every
task to the pool up front is an anti-pattern at that size — on Windows ``spawn`` the parent serialises
and queues all of them through one feeder thread, pinning a core and ballooning memory (331k Future
objects) while the workers starve. So we **stream**: keep only a bounded window of in-flight futures and
submit the next task each time one completes, and **aggregate incrementally** rather than holding every
result. Memory stays flat regardless of corpus size.
"""

from __future__ import annotations

import itertools
import os
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

import fitz

from ..stage1.walk import find_pdfs, dump_json  # reuse Stage 1's deterministic *.pdf walker + atomic JSON write
from . import STAGE2_TOOL_NAME, STAGE2_TOOL_VERSION
from .config import Stage2Config
from .process import process_pdf_stage2

_MANIFEST_NAME = "stage2-run-manifest.json"
_DEFAULT_PROGRESS_EVERY = 2000   # emit a progress line every N completed PDFs (0 = silent until done)

# per-PDF aggregate counters carried in each "rendered" result (summed into the manifest)
_AGG_KEYS = ("pages_rendered", "image", "mixed", "text", "blank", "redacted",
             "quality_suspect", "artifacts_kept", "artifacts_skipped", "artifacts_full_page")


def _worker(task: tuple) -> dict:
    pdf_path, root, s2cfg, generated_at, force = task
    return process_pdf_stage2(pdf_path, root, s2cfg, generated_at, force=force)


def run(root: str, s2cfg: Stage2Config, generated_at: str, *, force: bool, workers: int,
        progress_every: int = _DEFAULT_PROGRESS_EVERY) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs(root)
    total = len(pdfs)

    agg = {k: 0 for k in _AGG_KEYS}
    actions = {"rendered": 0, "skipped_done": 0, "skipped_status": 0, "no_sidecar": 0, "error": 0}
    errors: list[dict] = []
    no_sidecar: list[str] = []
    seen = 0

    def consume(r: dict) -> None:
        nonlocal seen
        seen += 1
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "rendered":
            for k in _AGG_KEYS:
                agg[k] += r[k]
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})
        elif r["action"] == "no_sidecar":
            no_sidecar.append(r["rel"])
        if progress_every and seen % progress_every == 0:
            el = time.monotonic() - started
            rate = seen / el if el else 0.0
            eta_min = ((total - seen) / rate / 60.0) if rate else 0.0
            print(f"  ... {seen}/{total} pdfs ({rate:.1f}/s, eta {eta_min:.0f}m) | "
                  f"rendered={actions['rendered']} image={agg['image']} text={agg['text']} "
                  f"vision={agg['image'] + agg['artifacts_kept']}", flush=True)

    # generator (NOT a 331k-long list) so tasks are materialised one at a time as the window advances
    tasks = ((p, root, s2cfg, generated_at, force) for p in pdfs)

    if workers <= 1 or total <= 1:
        for t in tasks:
            consume(_worker(t))
    else:
        max_inflight = max(workers * 4, workers + 1)   # bounded window: keep workers fed, memory flat
        with ProcessPoolExecutor(max_workers=workers) as ex:
            inflight = {ex.submit(_worker, t) for t in itertools.islice(tasks, max_inflight)}
            while inflight:
                done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
                for fut in done:
                    consume(fut.result())
                    nxt = next(tasks, None)
                    if nxt is not None:
                        inflight.add(ex.submit(_worker, nxt))

    manifest = {
        "tool": STAGE2_TOOL_NAME,
        "tool_version": STAGE2_TOOL_VERSION,
        "pymupdf_version": getattr(fitz, "pymupdf_version", "unknown"),
        "generated_at_utc": generated_at,
        "root": root,
        "pdfs_seen": total,
        "pdfs_rendered": actions["rendered"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_sidecar": actions["no_sidecar"],
        "pdfs_errored": actions["error"],
        "pages_rendered": agg["pages_rendered"],
        "image_pages": agg["image"],
        "mixed_pages": agg["mixed"],
        "text_pages": agg["text"],
        "blank_pages": agg["blank"],
        "redacted_pages": agg["redacted"],
        "text_quality_suspect": agg["quality_suspect"],
        "artifacts_kept": agg["artifacts_kept"],
        "artifacts_skipped": agg["artifacts_skipped"],
        "artifacts_full_page_skipped": agg["artifacts_full_page"],
        # operator's Stage-4 cost preview: every image page + every kept photo artifact is one vision unit.
        "vision_workload": agg["image"] + agg["artifacts_kept"],
        "errors": sorted(errors, key=lambda e: e["file"]),
        "skipped_no_sidecar": sorted(no_sidecar),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
