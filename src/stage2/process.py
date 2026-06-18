"""Per-PDF Stage-2 processing: read the Stage-1 sidecar, render + classify every page, extract photo
artifacts, generate a thumbnail, and extend the sidecar JSON (Handoff §3/§4/§5/§6/§7).

Fault-isolated: a PDF that won't open is reported and skipped; a single bad page is routed fail-safe
(rendered if possible, classified ``image`` if not) and never aborts the document.
"""

from __future__ import annotations

import dataclasses
import json
import os

import fitz

from ..stage1.config import Config
from ..stage1.redaction import analyze_redaction
from ..stage1.walk import dump_json, sidecar_path
from . import STAGE2_TOOL_NAME, STAGE2_TOOL_VERSION
from .config import Stage2Config
from .classify import Thresholds, PageSignals, classify_page_kind, visible_text_chars
from .render import clamp_render_dpi, render_page_png, render_thumbnail
from .artifacts import extract_page_artifacts

_S1_FIELDS = {f.name for f in dataclasses.fields(Config)}


def _stage1_config(sidecar: dict) -> Config:
    """Rebuild the Stage-1 Config from the sidecar's echoed ``generator.config`` so Stage 2 classifies
    against exactly the thresholds Stage 1 measured with (unknown/extra keys are ignored)."""
    echo = (sidecar.get("generator") or {}).get("config") or {}
    overrides = {k: v for k, v in echo.items() if k in _S1_FIELDS}
    try:
        return dataclasses.replace(Config(), **overrides)
    except Exception:
        return Config()  # malformed echo -> fall back to defaults (still deterministic)


def _bar_coverage(page: fitz.Page, s1cfg: Config) -> float:
    """Union fraction of the page covered by detected redaction bars (Handoff §4 rule 2). Recomputed from
    the live page via Stage-1's detector because bar geometry is not stored in the sidecar."""
    ctx = analyze_redaction(page, s1cfg)
    if not ctx.bars:
        return 0.0
    page_area = abs(page.rect.get_area()) or 1.0
    area = sum(abs(b.get_area()) for b in ctx.bars)  # merged bars don't overlap -> sum is the union area
    return min(1.0, area / page_area)


def _result(action: str, rel: str, *, error_code: str | None = None, message: str | None = None,
            counts: dict | None = None, art: dict | None = None, vision: int = 0,
            page_count: int = 0) -> dict:
    base = {
        "action": action, "rel": rel, "error_code": error_code,
        "pages_rendered": 0, "image": 0, "mixed": 0, "text": 0, "blank": 0, "redacted": 0,
        "quality_suspect": 0, "artifacts_kept": 0, "artifacts_skipped": 0, "artifacts_full_page": 0,
        "vision_workload": vision, "page_count": page_count,
    }
    if counts:
        for k in ("pages_rendered", "image", "mixed", "text", "blank", "redacted", "quality_suspect"):
            base[k] = counts[k]
    if art:
        base["artifacts_kept"] = art["kept"]
        base["artifacts_skipped"] = art["skipped"]
        base["artifacts_full_page"] = art["full_page"]
    if message:
        base["message"] = message[:500]
    return base


def process_pdf_stage2(pdf_path: str, root: str, s2cfg: Stage2Config, generated_at: str, *,
                       force: bool) -> dict:
    rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
    sc = sidecar_path(pdf_path)

    # 1. Stage-1 sidecar gate -------------------------------------------------
    try:
        with open(sc, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
    except FileNotFoundError:
        return _result("no_sidecar", rel)
    except Exception as exc:
        return _result("error", rel, error_code="bad_sidecar", message=f"{type(exc).__name__}: {exc}")
    if sidecar.get("status") != "ok":
        return _result("skipped_status", rel)
    if sidecar.get("stage2") and not force:        # already complete -> resume skip (crash-safe granularity)
        return _result("skipped_done", rel)

    # 2. thresholds (Stage-2 config + the Stage-1 values the §4 decision needs) ------------------------
    s1cfg = _stage1_config(sidecar)
    th = Thresholds(min_chars=s1cfg.min_chars, full_page_threshold=s1cfg.full_page_threshold,
                    figure_min_coverage=s1cfg.figure_min_coverage, blank_ink_max=s1cfg.blank_ink_max,
                    fully_redacted_bar_frac=s2cfg.fully_redacted_bar_frac)
    marker = s1cfg.redaction_marker

    # 3. open the PDF ---------------------------------------------------------
    try:
        doc = fitz.open(pdf_path, filetype="pdf")
    except Exception as exc:
        return _result("error", rel, error_code="open_failed", message=f"{type(exc).__name__}: {exc}")

    name = os.path.splitext(os.path.basename(pdf_path))[0]
    base_dir = os.path.dirname(pdf_path)
    pages = sidecar.get("pages") or []
    page_assets: list[dict] = []
    artifact_assets: list[dict] = []
    seen: dict = {}
    art_counts = {"kept": 0, "skipped": 0, "full_page": 0}
    counts = {"pages_rendered": 0, "image": 0, "mixed": 0, "text": 0, "blank": 0, "redacted": 0,
              "quality_suspect": 0}

    try:
        for i in range(min(doc.page_count, len(pages))):
            pobj = pages[i]
            page = doc[i]
            page_number = int(pobj.get("page_number") or (i + 1))
            m = pobj.get("metrics") or {}
            txt = pobj.get("text") or {}
            red = pobj.get("redaction") or {}

            char_count = int(txt.get("char_count") or 0)
            vis_chars = visible_text_chars(txt.get("content") or "", marker)
            redaction_runs = int(red.get("redaction_runs") or 0)
            max_cov = float(m.get("max_image_coverage") or 0.0)

            # Rule 2 needs union bar coverage — but only recompute it (a render) when the cheap sidecar
            # signals already point at a fully-redacted page. Otherwise leave it None (rule 2 can't fire).
            bar_cov = None
            if redaction_runs > 0 and vis_chars < th.min_chars:
                try:
                    bar_cov = _bar_coverage(page, s1cfg)
                except Exception:
                    bar_cov = None

            sig = PageSignals(
                char_count=char_count, visible_text_chars=vis_chars, max_image_coverage=max_cov,
                embedded_image_count=int(m.get("embedded_image_count") or 0),
                needs_vision_reason=pobj.get("needs_vision_reason"),
                ink_coverage=m.get("ink_coverage"), dark_coverage=m.get("dark_coverage"),
                redaction_runs=redaction_runs)
            kind, reason = classify_page_kind(sig, th, bar_cov)
            counts[kind] += 1

            # Stage 2's page_kind keys on char-count/coverage/redaction only (Handoff §4); it does NOT
            # re-run Stage 1's §8 quality gate. A page Stage 1 rejected as garbage OCR (quality_fail) but
            # which has >= min_chars therefore lands in the cheap text/mixed lane. We keep it there (no
            # vision) but FLAG it so Stage 6/7 ingestion can decide whether its text is trustworthy or the
            # page should be escalated. (Not a leak — redaction is handled separately and stays fail-safe.)
            if kind in ("text", "mixed") and pobj.get("needs_vision_reason") == "quality_fail":
                if "low_quality_text" not in (pobj.get("flags") or []):
                    pobj.setdefault("flags", []).append("low_quality_text")
                counts["quality_suspect"] += 1

            dpi = clamp_render_dpi(page, s2cfg.render_dpi, s2cfg.max_render_pixels, s2cfg.min_render_dpi)
            render_rel = None
            render_dpi = None
            try:
                if kind == "image":
                    fn = f"page-{page_number:04d}.png"
                    w, h = render_page_png(page, os.path.join(base_dir, name, "images", fn), dpi, force=force)
                    render_rel, render_dpi = f"{name}/images/{fn}", dpi
                    counts["pages_rendered"] += 1
                    page_assets.append({"kind": "page", "page_number": page_number, "path": render_rel,
                                        "width_px": w, "height_px": h, "coverage": round(max_cov, 4)})
                elif s2cfg.stage_pages:
                    fn = f"page-{page_number:04d}.png"
                    w, h = render_page_png(page, os.path.join(base_dir, name, "pages", fn), dpi, force=force)
                    render_rel, render_dpi = f"{name}/pages/{fn}", dpi
                    counts["pages_rendered"] += 1
                # else (non-image, stage_pages=False): render deferred to Stage 7 -> render_rel stays None
            except Exception as exc:
                pobj.setdefault("flags", []).append(f"stage2_render_error:{type(exc).__name__}")

            pobj["page_kind"] = kind
            pobj["page_kind_reason"] = reason
            pobj["render_path"] = render_rel
            pobj["render_dpi"] = render_dpi

            # photo artifacts (Handoff §6) — only on pages that actually carry embedded rasters; full-page
            # backing scans (Stage-1 is_full_page) are the page render, not a photo artifact, so skip them.
            if int(m.get("embedded_image_count") or 0) > 0:
                full_page_xrefs = {int(im["xref"]) for im in (pobj.get("images") or [])
                                   if im.get("is_full_page") and im.get("xref")}
                try:
                    extract_page_artifacts(doc, page, page_number, base_dir, name,
                                           min_px=s2cfg.artifact_min_px, force=force,
                                           full_page_xrefs=full_page_xrefs,
                                           seen=seen, assets=artifact_assets, counts=art_counts)
                except Exception as exc:
                    pobj.setdefault("flags", []).append(f"stage2_artifact_error:{type(exc).__name__}")

        # thumbnail (Handoff §5) — one per PDF, representative page (default: first) ----------------------
        thumb_rel = None
        if doc.page_count > 0:
            tp = min(max(1, s2cfg.thumbnail_page), doc.page_count) - 1
            try:
                render_thumbnail(doc[tp], os.path.join(base_dir, name, "thumbnail.png"),
                                 s2cfg.thumbnail_max_px, force=force)
                thumb_rel = f"{name}/thumbnail.png"
            except Exception:
                thumb_rel = None

        image_assets = page_assets + artifact_assets
        vision_workload = len(page_assets) + art_counts["kept"]

        # 4. extend the sidecar (top-level) ----------------------------------
        sidecar["thumbnail_path"] = thumb_rel
        sidecar["image_assets"] = image_assets
        sidecar["stage2"] = {
            "tool": STAGE2_TOOL_NAME,
            "tool_version": STAGE2_TOOL_VERSION,
            "pymupdf_version": getattr(fitz, "pymupdf_version", "unknown"),
            "generated_at_utc": generated_at,
            "config": {
                **s2cfg.echo(),
                "derived_from_stage1": {
                    "min_chars": s1cfg.min_chars,
                    "full_page_threshold": s1cfg.full_page_threshold,
                    "figure_min_coverage": s1cfg.figure_min_coverage,
                    "blank_ink_max": s1cfg.blank_ink_max,
                    "redaction_marker": s1cfg.redaction_marker,
                },
            },
            "counts": {
                "pages_rendered": counts["pages_rendered"],
                "image_pages": counts["image"],
                "mixed_pages": counts["mixed"],
                "text_pages": counts["text"],
                "blank_pages": counts["blank"],
                "redacted_pages": counts["redacted"],
                "text_quality_suspect": counts["quality_suspect"],
                "artifacts": art_counts["kept"],
                "artifacts_skipped": art_counts["skipped"],
                "artifacts_full_page_skipped": art_counts["full_page"],
                "vision_workload": vision_workload,
            },
        }
        dump_json(sidecar, sc)
        return _result("rendered", rel, counts=counts, art=art_counts, vision=vision_workload,
                       page_count=doc.page_count)
    finally:
        doc.close()
