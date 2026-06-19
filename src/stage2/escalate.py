"""Stage-2 post-passes (added 2026-06-18 for the DataSet-09 over-inclusive vision decision).

Two idempotent maintenance passes that operate on PDFs already carrying a ``stage2`` block:

- ``run_escalate`` — forward-fill the vision set. Under ``--no-stage-pages`` only ``image`` pages were
  rendered, but the adopted vision routing is ``image ∪ mixed ∪ low_quality_text``. ``low_quality_text``
  pages (poor-OCR/handwriting full-page scans) have NO render and aren't artifacts, so render each to
  ``<name>/images/`` and register it in ``image_assets[]`` (``kind="page"``, ``route_reason``). Idempotent.

- ``run_purge`` — delete the deferred ``<name>/pages/`` display renders (only the first full-staged batch
  has them). Display renders are regenerated deterministically at Stage 7 (render-on-ingest); ``images/``,
  ``artifacts/`` and ``thumbnail.png`` are never touched. Dry-run unless ``delete=True``; nulls dangling
  ``render_path`` on non-image pages so the sidecars match the ``--no-stage-pages`` state.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED

import fitz

from ..stage1.walk import dump_json, sidecar_path
from .config import Stage2Config
from .render import clamp_render_dpi, render_page_png

_ESCALATE_MANIFEST = "stage2-escalate-manifest.json"
_PURGE_MANIFEST = "stage2-purge-pages-manifest.json"


def _find_pdfs_pruned(root: str) -> list[str]:
    """Like stage1.walk.find_pdfs but PRUNES the Stage-2 per-PDF output dirs (``<stem>/`` with a sibling
    ``<stem>.pdf``, and the ``images/pages/artifacts`` subdirs). On a processed corpus this avoids
    descending into hundreds of thousands of PNG dirs (the Handoff §4.3 slow-walk)."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in ("images", "pages", "artifacts") and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def _pool_map(tasks_iter, worker, workers: int):
    """Bounded in-flight window over a process pool (same memory-flat pattern as walk.py). Yields results."""
    if workers <= 1:
        for t in tasks_iter:
            yield worker(t)
        return
    max_inflight = max(workers * 4, workers + 1)
    with ProcessPoolExecutor(max_workers=workers) as ex:
        inflight = {ex.submit(worker, t) for t in itertools.islice(tasks_iter, max_inflight)}
        while inflight:
            done, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in done:
                yield fut.result()
                nxt = next(tasks_iter, None)
                if nxt is not None:
                    inflight.add(ex.submit(worker, nxt))


# --------------------------------------------------------------------------- escalate

def _escalate_worker(task: tuple) -> dict:
    pdf_path, root, s2cfg, generated_at = task
    sc = sidecar_path(pdf_path)
    try:
        with open(sc, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
    except Exception:
        return {"action": "no_sidecar", "rendered": 0}
    if not sidecar.get("stage2"):
        return {"action": "no_stage2", "rendered": 0}

    pages = sidecar.get("pages") or []
    targets = [q for q in pages if "low_quality_text" in (q.get("flags") or [])]
    if not targets:
        return {"action": "none", "rendered": 0}

    name = os.path.splitext(os.path.basename(pdf_path))[0]
    base_dir = os.path.dirname(pdf_path)
    image_assets = sidecar.get("image_assets") or []
    have_page = {a.get("page_number") for a in image_assets if a.get("kind") == "page"}

    rendered = 0
    doc = None
    try:
        for q in targets:
            pno = int(q.get("page_number"))
            if pno in have_page:          # already an image asset (idempotent re-run / also-image page)
                continue
            rel = f"{name}/images/page-{pno:04d}.png"
            dest = os.path.join(base_dir, name, "images", f"page-{pno:04d}.png")
            if doc is None:
                doc = fitz.open(pdf_path)
            page = doc[pno - 1]
            dpi = clamp_render_dpi(page, s2cfg.render_dpi, s2cfg.max_render_pixels, s2cfg.min_render_dpi)
            w, h = render_page_png(page, dest, dpi, force=False)   # render_page_png is itself idempotent
            image_assets.append({"kind": "page", "page_number": pno, "path": rel,
                                 "width_px": w, "height_px": h, "route_reason": "low_quality_text"})
            q["render_path"] = rel
            q["render_dpi"] = dpi
            have_page.add(pno)
            rendered += 1
    except Exception as exc:
        if doc is not None:
            doc.close()
        return {"action": "error", "rendered": rendered, "message": f"{type(exc).__name__}: {exc}"}
    finally:
        if doc is not None:
            doc.close()

    if rendered:
        sidecar["image_assets"] = image_assets
        s2 = sidecar["stage2"]
        cnt = s2.setdefault("counts", {})
        cnt["escalated_low_quality_text"] = cnt.get("escalated_low_quality_text", 0) + rendered
        if "vision_workload" in cnt:
            cnt["vision_workload"] += rendered
        s2["escalation"] = {"generated_at_utc": generated_at, "route_reason": "low_quality_text",
                            "pages_rendered": cnt["escalated_low_quality_text"]}
        dump_json(sidecar, sc)
    return {"action": "escalated" if rendered else "none", "rendered": rendered}


def run_escalate(root: str, s2cfg: Stage2Config, generated_at: str, *, workers: int,
                 progress_every: int = 5000) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = _find_pdfs_pruned(root)
    total = len(pdfs)
    agg = {"seen": 0, "escalated_pdfs": 0, "pages_rendered": 0, "no_stage2": 0, "no_sidecar": 0, "errors": 0}
    tasks = ((p, root, s2cfg, generated_at) for p in pdfs)
    for r in _pool_map(tasks, _escalate_worker, workers):
        agg["seen"] += 1
        agg["pages_rendered"] += r.get("rendered", 0)
        if r["action"] == "escalated":
            agg["escalated_pdfs"] += 1
        elif r["action"] == "no_stage2":
            agg["no_stage2"] += 1
        elif r["action"] == "no_sidecar":
            agg["no_sidecar"] += 1
        elif r["action"] == "error":
            agg["errors"] += 1
        if progress_every and agg["seen"] % progress_every == 0:
            el = time.monotonic() - started
            rate = agg["seen"] / el if el else 0.0
            print(f"  ... {agg['seen']}/{total} pdfs ({rate:.0f}/s) | escalated_pdfs={agg['escalated_pdfs']} "
                  f"pages_rendered={agg['pages_rendered']} errors={agg['errors']}", flush=True)
    manifest = {"mode": "escalate-vision", "route_reason": "low_quality_text", "root": root,
                "generated_at_utc": generated_at, "pdfs_seen": total,
                "pdfs_escalated": agg["escalated_pdfs"], "pages_rendered": agg["pages_rendered"],
                "pdfs_no_stage2": agg["no_stage2"], "pdfs_no_sidecar": agg["no_sidecar"],
                "errors": agg["errors"], "wall_clock_seconds": round(time.monotonic() - started, 1)}
    dump_json(manifest, os.path.join(root, _ESCALATE_MANIFEST))
    return manifest


# --------------------------------------------------------------------------- purge pages/

def _purge_worker(task: tuple) -> dict:
    pdf_path, root, delete = task
    name = os.path.splitext(os.path.basename(pdf_path))[0]
    pages_dir = os.path.join(os.path.dirname(pdf_path), name, "pages")
    if not os.path.isdir(pages_dir):
        return {"had_pages": 0, "bytes": 0, "files": 0, "deleted": 0, "nulled": 0}
    total = 0
    nfiles = 0
    for dp, _d, fs in os.walk(pages_dir):
        for fn in fs:
            try:
                total += os.path.getsize(os.path.join(dp, fn))
                nfiles += 1
            except OSError:
                pass
    nulled = 0
    deleted = 0
    if delete:
        shutil.rmtree(pages_dir, ignore_errors=True)
        deleted = 1
        sc = sidecar_path(pdf_path)
        try:
            with open(sc, "r", encoding="utf-8") as f:
                sidecar = json.load(f)
            changed = False
            for q in (sidecar.get("pages") or []):
                rp = q.get("render_path")
                if rp and "/pages/" in rp:
                    q["render_path"] = None
                    q["render_dpi"] = None
                    nulled += 1
                    changed = True
            if changed:
                dump_json(sidecar, sc)
        except Exception:
            pass
    return {"had_pages": 1, "bytes": total, "files": nfiles, "deleted": deleted, "nulled": nulled}


def run_purge(root: str, *, workers: int, delete: bool, progress_every: int = 20000) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    pdfs = _find_pdfs_pruned(root)
    total = len(pdfs)
    agg = {"seen": 0, "with_pages": 0, "bytes": 0, "files": 0, "deleted": 0, "nulled": 0}
    tasks = ((p, root, delete) for p in pdfs)
    for r in _pool_map(tasks, _purge_worker, workers):
        agg["seen"] += 1
        agg["with_pages"] += r["had_pages"]
        agg["bytes"] += r["bytes"]
        agg["files"] += r["files"]
        agg["deleted"] += r["deleted"]
        agg["nulled"] += r["nulled"]
        if progress_every and agg["seen"] % progress_every == 0:
            print(f"  ... {agg['seen']}/{total} scanned | with_pages={agg['with_pages']} "
                  f"reclaimable={agg['bytes']/1e9:.1f}GB deleted={agg['deleted']}", flush=True)
    manifest = {"mode": "purge-staged-pages", "dry_run": (not delete), "root": root,
                "pdfs_seen": total, "pdfs_with_pages_dir": agg["with_pages"],
                "page_png_files": agg["files"], "bytes": agg["bytes"],
                "gb": round(agg["bytes"] / 1e9, 2), "dirs_deleted": agg["deleted"],
                "render_paths_nulled": agg["nulled"], "wall_clock_seconds": round(time.monotonic() - started, 1)}
    if delete:   # only write a manifest for a real run; a dry-run shouldn't litter the tree
        dump_json(manifest, os.path.join(root, _PURGE_MANIFEST))
    return manifest
