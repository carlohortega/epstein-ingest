"""Folder walk, idempotency/resume, bounded parallelism, and the run manifest (Spec §3).

One PDF per worker process (PyMuPDF doc handles are not thread-safe; pages within a PDF are processed
sequentially inside the worker). A crash mid-run loses at most the in-flight file.
"""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait

import fitz

from . import TOOL_NAME, TOOL_VERSION, SCHEMA_VERSION
from .config import Config
from .document import process_pdf, sha256_file, _error_sidecar


def sidecar_path(pdf_path: str) -> str:
    return os.path.splitext(pdf_path)[0] + ".json"


def find_pdfs(root: str) -> list[str]:
    """Recursively find *.pdf (case-insensitive), in a stable sorted order for determinism."""
    out: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _recorded_sha(sidecar: str) -> str | None:
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            return (json.load(f).get("source") or {}).get("sha256")
    except Exception:
        return None


def _sidecar_status_ok(sidecar: str) -> bool:
    """True if an existing sidecar records a successful extraction (status == 'ok')."""
    try:
        with open(sidecar, "r", encoding="utf-8") as f:
            return json.load(f).get("status") == "ok"
    except Exception:
        return False


def dump_json(obj: dict, path: str) -> None:
    """Deterministic write: fixed indent, UTF-8, keys in construction order, trailing newline."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)  # atomic on POSIX -> a crash never leaves a half-written sidecar


def _worker(task: tuple) -> dict:
    """Process one PDF, write its sidecar, return a small summary for the manifest."""
    path, root, cfg, generated_at, precomputed = task
    sidecar = sidecar_path(path)
    obj = process_pdf(path, root, cfg, generated_at, precomputed)
    dump_json(obj, sidecar)

    rel = os.path.relpath(path, root).replace(os.sep, "/")
    if obj["status"] == "error":
        return {"action": "error", "rel": rel, "error_code": obj["error"]["code"],
                "pages_total": 0, "text_sufficient": 0, "need_vision": 0,
                "redaction_flagged": 0, "blank": 0}
    s = obj["extraction_summary"]
    return {
        "action": "extracted", "rel": rel, "error_code": None,
        "pages_total": obj["source"]["page_count"],
        "text_sufficient": s["pages_text_sufficient"],
        "need_vision": s["pages_need_vision"],
        "redaction_flagged": s["pages_redaction_flagged"],
        "blank": s["pages_blank"],
    }


def _error_result(rel: str, code: str) -> dict:
    return {"action": "error", "rel": rel, "error_code": code,
            "pages_total": 0, "text_sufficient": 0, "need_vision": 0, "redaction_flagged": 0, "blank": 0}


def _quarantine(task: tuple, code: str, message: str) -> dict:
    """Write an error sidecar for a file whose worker was killed (stall) or crashed, so it is recorded
    and skipped on resume rather than re-hanging the run. Returns its manifest result."""
    path, root, cfg, generated_at, precomputed = task
    try:
        obj = _error_sidecar(path, root, cfg, generated_at, code, message, precomputed, None)
        dump_json(obj, sidecar_path(path))
    except Exception:
        pass
    return _error_result(os.path.relpath(path, root).replace(os.sep, "/"), code)


def _pump(tasks: list, workers: int, cfg: Config):
    """Run tasks through a process pool with a BOUNDED in-flight window (memory-safe at millions of
    files), a periodic HEARTBEAT to stdout, and STALL recovery (if no task completes for
    ``stall_timeout_seconds`` the pool is killed and the stuck file(s) quarantined). Yields result dicts."""
    window = max(workers + cfg.submit_window_extra, workers + 1)
    stall = cfg.stall_timeout_seconds
    beat = cfg.heartbeat_seconds
    total = len(tasks)
    start = last_beat = time.monotonic()
    done = 0
    it = iter(tasks)
    ex = ProcessPoolExecutor(max_workers=workers)
    inflight: dict = {}

    def _submit_next() -> bool:
        try:
            t = next(it)
        except StopIteration:
            return False
        inflight[ex.submit(_worker, t)] = t
        return True

    try:
        for _ in range(window):
            if not _submit_next():
                break
        while inflight:
            completed, _pending = wait(set(inflight), timeout=stall, return_when=FIRST_COMPLETED)
            now = time.monotonic()
            if not completed:
                stuck = list(inflight.values())
                print(f"[stall] no progress for {stall}s — killing pool and quarantining "
                      f"{len(stuck)} in-flight file(s) as timeout; continuing", flush=True)
                try:
                    for p in list(getattr(ex, "_processes", {}).values()):
                        p.kill()
                except Exception:
                    pass
                ex.shutdown(wait=False, cancel_futures=True)
                for t in stuck:
                    yield _quarantine(t, "timeout", f"no completion within {stall}s; quarantined")
                    done += 1
                inflight = {}
                ex = ProcessPoolExecutor(max_workers=workers)
                for _ in range(window):
                    if not _submit_next():
                        break
                last_beat = now
                continue
            for fut in completed:
                t = inflight.pop(fut)
                try:
                    yield fut.result()
                except Exception as exc:  # worker crashed outright (process_pdf already catches normal errors)
                    yield _quarantine(t, "worker_error", f"{type(exc).__name__}: {exc}")
                done += 1
                _submit_next()
            if now - last_beat >= beat:
                el = now - start
                rate = done / el if el > 0 else 0.0
                eta = (total - done) / rate / 60.0 if rate > 0 else 0.0
                print(f"[progress] {done}/{total} ({100.0*done/total:.1f}%)  {rate:.1f}/s  "
                      f"in-flight={len(inflight)}  eta~{eta:.0f}m", flush=True)
                last_beat = now
    finally:
        ex.shutdown(wait=False)


def run(root: str, cfg: Config, generated_at: str, *, force: bool, workers: int,
        skip_existing: bool = False) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = find_pdfs(root)

    tasks: list[tuple] = []
    skipped = 0
    for p in pdfs:
        sc = sidecar_path(p)
        precomputed = None
        if not force and os.path.exists(sc):
            if skip_existing:
                # fast incremental/resume: skip on sidecar EXISTENCE alone (a stat, no file read).
                # Opening+parsing every sidecar each pass is catastrophic at 100k+ files on an
                # AV-scanned disk (the skip-scan took hours and grew each pass). The dataset is
                # append-only, so existence is sufficient; a still-missing sidecar => (re)process.
                skipped += 1
                continue
            else:
                rec = _recorded_sha(sc)
                if rec:
                    cur = sha256_file(p)
                    if rec == cur:
                        skipped += 1
                        continue
                    precomputed = cur  # changed file: reuse the hash we just computed
        tasks.append((p, root, cfg, generated_at, precomputed))

    if tasks:
        print(f"[start] {len(tasks)} file(s) to process, {skipped} already done, {workers} workers", flush=True)
    results: list[dict] = []
    if workers <= 1 or len(tasks) <= 1:
        for t in tasks:
            results.append(_worker(t))
    else:
        results.extend(_pump(tasks, workers, cfg))

    extracted = sum(1 for r in results if r["action"] == "extracted")
    errored = sum(1 for r in results if r["action"] == "error")
    manifest = {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "pymupdf_version": getattr(fitz, "pymupdf_version", "unknown"),
        "generated_at_utc": generated_at,
        "root": root,
        "files_seen": len(pdfs),
        "files_extracted": extracted,
        "files_skipped": skipped,
        "files_errored": errored,
        "pages_total": sum(r["pages_total"] for r in results),
        "pages_text_sufficient": sum(r["text_sufficient"] for r in results),
        "pages_need_vision": sum(r["need_vision"] for r in results),
        "pages_redaction_flagged": sum(r["redaction_flagged"] for r in results),
        "pages_blank": sum(r["blank"] for r in results),
        "errors": sorted([{"file": r["rel"], "code": r["error_code"]}
                          for r in results if r["action"] == "error"],
                         key=lambda e: e["file"]),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, "extract-run-manifest.json"))
    return manifest
