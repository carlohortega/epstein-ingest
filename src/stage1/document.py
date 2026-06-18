"""Per-PDF processing: open, walk pages, assemble the sidecar JSON object (Spec §5).

Fault-isolated (Spec §3.6): a malformed/encrypted/corrupt PDF yields a ``status:"error"`` sidecar and
never aborts the run. A per-page failure is caught and routed fail-safe to vision rather than dropping
the whole document.
"""

from __future__ import annotations

import hashlib
import os

import fitz

from . import SCHEMA_VERSION, TOOL_NAME, TOOL_VERSION
from .config import Config
from .reconstruct import RECONSTRUCTION
from .page import process_page
from .quality import text_quality


def sha256_file(path: str, _bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


def _generator_block(cfg: Config, generated_at: str) -> dict:
    return {
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "pymupdf_version": getattr(fitz, "pymupdf_version", getattr(fitz, "VersionBind", "unknown")),
        "reconstruction": RECONSTRUCTION,
        "generated_at_utc": generated_at,
        "config": cfg.echo(),
    }


def _error_sidecar(path: str, root: str, cfg: Config, generated_at: str,
                   code: str, message: str, sha: str | None, page_count: int | None) -> dict:
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "error": {"code": code, "message": message[:500]},
        "generator": _generator_block(cfg, generated_at),
        "source": {
            "filename": os.path.basename(path),
            "relative_path": rel,
            "bytes": size,
            "sha256": sha,
            "page_count": page_count,
            "pdf_producer": None,
            "pdf_metadata": {},
        },
        "document": {"source_kind": "pdf", "content_type": "application/pdf",
                     "bates_begin": None, "bates_end": None, "title": None},
        "extraction_summary": None,
        "pages": [],
    }


def _fallback_page(page_number: int, exc: Exception) -> dict:
    """Fail-safe page record when a single page errors: route it to vision, emit no text."""
    return {
        "page_number": page_number,
        "page_class": "full_page_visual",
        "needs_vision": True,
        "needs_vision_reason": "quality_fail",
        "text": {"content": "", "format": "plain", "char_count": 0, "visible_chars": 0, "invisible_chars": 0},
        "metrics": {"text_layer_type": "none", "distinct_font_sizes": 0, "distinct_fonts": 0,
                    "embedded_image_count": 0, "max_image_coverage": 0.0, "is_full_page_image": False,
                    "ink_coverage": None, "dark_coverage": None, "bates_stamp": None,
                    "width_pt": 0.0, "height_pt": 0.0, "rotation": 0,
                    "quality": {"alpha_ratio": 0.0, "wordlike_ratio": 0.0, "mean_word_len": 0.0, "pass": False}},
        "redaction": {"occluder_rects": 0, "dark_occluder_rects": 0, "invisible_glyphs_total": 0,
                      "invisible_glyphs_under_dark": 0, "redaction_runs": 0,
                      "scrubbed_char_count": 0, "suspected": False},
        "images": [],
        "flags": [f"page_error:{type(exc).__name__}"],
        "_agg": {"visible_chars": 0, "invisible_chars": 0, "scrubbed_chars": 0,
                 "text_chars": 0, "needs_vision": True, "suspected": False, "blank": False},
    }


def process_pdf(path: str, root: str, cfg: Config, generated_at: str,
                precomputed_sha: str | None = None) -> dict:
    sha = precomputed_sha or sha256_file(path)
    try:
        doc = fitz.open(path, filetype="pdf")
    except Exception as exc:
        return _error_sidecar(path, root, cfg, generated_at, "corrupt",
                              f"{type(exc).__name__}: {exc}", sha, None)

    try:
        if getattr(doc, "needs_pass", False) or getattr(doc, "is_encrypted", False):
            return _error_sidecar(path, root, cfg, generated_at, "encrypted",
                                  "password-protected PDF", sha, getattr(doc, "page_count", None))

        meta = doc.metadata or {}
        rel = os.path.relpath(path, root).replace(os.sep, "/")
        pages: list[dict] = []
        kept_text_parts: list[str] = []
        agg = {"visible": 0, "invisible": 0, "scrubbed": 0, "text": 0,
               "text_sufficient": 0, "need_vision": 0, "redaction_flagged": 0, "blank": 0}

        for i in range(doc.page_count):
            try:
                pg = process_page(doc[i], i + 1, cfg)
            except Exception as exc:  # per-page fault isolation
                pg = _fallback_page(i + 1, exc)
            a = pg.pop("_agg")
            agg["visible"] += a["visible_chars"]
            agg["invisible"] += a["invisible_chars"]
            agg["scrubbed"] += a["scrubbed_chars"]
            agg["text"] += a["text_chars"]
            if a["needs_vision"]:
                agg["need_vision"] += 1
            else:
                agg["text_sufficient"] += 1
            if a["suspected"]:
                agg["redaction_flagged"] += 1
            if a["blank"]:
                agg["blank"] += 1
            kept_text_parts.append(pg["text"]["content"])
            pages.append(pg)

        page_count = doc.page_count
        doc_quality = text_quality("\n\n".join(kept_text_parts), cfg)

        frac = (agg["text_sufficient"] / page_count) if page_count else 0.0
        if frac >= cfg.text_primary_frac:
            recommendation = "text_primary"
        elif frac <= cfg.vision_primary_frac:
            recommendation = "vision_primary"
        else:
            recommendation = "mixed"

        return {
            "schema_version": SCHEMA_VERSION,
            "status": "ok",
            "error": None,
            "generator": _generator_block(cfg, generated_at),
            "source": {
                "filename": os.path.basename(path),
                "relative_path": rel,
                "bytes": os.path.getsize(path),
                "sha256": sha,
                "page_count": page_count,
                "pdf_producer": meta.get("producer") or None,
                "pdf_metadata": {k: v for k, v in meta.items() if v},
            },
            "document": {
                "source_kind": "pdf",
                "content_type": "application/pdf",
                "bates_begin": None,
                "bates_end": None,
                "title": meta.get("title") or None,
            },
            "extraction_summary": {
                "total_text_chars": agg["text"],
                "visible_chars": agg["visible"],
                "invisible_chars": agg["invisible"],
                "scrubbed_chars": agg["scrubbed"],
                "pages_text_sufficient": agg["text_sufficient"],
                "pages_need_vision": agg["need_vision"],
                "pages_redaction_flagged": agg["redaction_flagged"],
                "pages_blank": agg["blank"],
                "text_quality": doc_quality,
                "routing_recommendation": recommendation,
            },
            "pages": pages,
        }
    finally:
        doc.close()
