"""Per-page processing in the exact order mandated by Spec §4:
render-free analysis -> redaction scrub -> text reconstruction -> metrics -> quality gate -> routing.
"""

from __future__ import annotations

import re

import fitz

from .config import Config
from .classify import classify
from .images import page_images
from .quality import text_quality
from .reconstruct import detect_layer_type, reconstruct
from .redaction import analyze_redaction, page_coverage

# Bates control numbers stamped on production pages, e.g. "EFTA02730468", "ABC-000123".
# Prefix letters immediately followed (or one separator) by a long zero-padded number. Requiring >=6
# contiguous digits avoids matching codeword+year like "AMALGAM 2005" (space + only 4 digits).
_BATES = re.compile(r"\b[A-Z]{2,8}[-_]?\d{6,}\b")


def _figures_present(page: fitz.Page, images: list[dict], cfg: Config) -> bool:
    """Rule-4 heuristic (Spec §9.4 / §13): a mid-size embedded image with little co-located text is a
    figure/photo the text layer won't capture -> needs vision. Full-page images are backing scans
    (rule 5). Conservative: when unsure, bias to vision."""
    candidates = [im for im in images
                  if cfg.figure_min_coverage <= im["coverage"] < cfg.full_page_threshold]
    if not candidates:
        return False
    words = page.get_text("words")
    for im in candidates:
        irect = fitz.Rect(im["bbox"])
        over = 0
        for w in words:
            inter = fitz.Rect(w[0], w[1], w[2], w[3]) & irect
            if inter.is_valid and abs(inter.get_area()) > 0:
                over += 1
                if over >= cfg.figure_min_words_over:
                    break
        if over < cfg.figure_min_words_over:
            return True  # an image with little text over it -> real figure
    return False


def process_page(page: fitz.Page, page_number: int, cfg: Config) -> dict:
    # 1. render-free analysis -------------------------------------------------
    raw_text = page.get_text("text") or ""
    embedded_image_count = len(page.get_images(full=True))
    images, max_cov = page_images(page, cfg)
    is_full_page_image = max_cov >= cfg.full_page_threshold
    page_class = classify(raw_text, embedded_image_count)
    layer = detect_layer_type(page, cfg)
    bates_match = _BATES.search(raw_text)
    bates_stamp = bates_match.group(0) if bates_match else None

    # 2. redaction scrub context ---------------------------------------------
    ctx = analyze_redaction(page, cfg)

    # 3. text reconstruction (scrub applied at span level) --------------------
    rec = reconstruct(page, ctx, layer["text_layer_type"], cfg)
    content = rec["content"]
    char_count = rec["char_count"]
    scrubbed_char_count = rec["scrubbed_char_count"]
    total_chars = rec["total_chars"]
    redaction_runs = rec["redaction_runs"]

    # 4. metrics (on scrubbed text) ------------------------------------------
    quality = text_quality(content, cfg)

    # redaction record + suspicion (Spec §7) ----------------------------------
    # Positive signal: suspected only when a SOLID bar actually covered OCR text (redaction_runs>0).
    # "invisible glyphs over dark pixels" alone is NOT enough — it false-positives on dark photos/figures
    # (confirmed by visual review of real pages); those glyphs are kept as text, not flagged.
    suspected = redaction_runs > 0

    # 6. routing decision (Spec §9, first match wins) -------------------------
    # coverage is computed only for low-text pages (the vision-bound subset) — it stays null for
    # text-rich pages so the common case remains render-free.
    ink_coverage = None
    dark_coverage = None
    blank_kind = None   # "white" | "black"
    if suspected:
        needs_vision, reason = True, "redaction_suspected"
    elif char_count < cfg.min_chars:
        if cfg.blank_ink_max > 0:
            ink_coverage, dark_coverage = page_coverage(
                page, cfg.blank_render_dpi, cfg.ink_white_level, cfg.dark_level)
            if ink_coverage < cfg.blank_ink_max:
                needs_vision, reason, blank_kind = False, "blank", "white"   # white page + maybe a stamp
            elif dark_coverage > 1.0 - cfg.blank_ink_max:
                needs_vision, reason, blank_kind = False, "blank", "black"   # solid-black scanner blank
            else:
                needs_vision, reason = True, "low_text"
        else:
            needs_vision, reason = True, "low_text"
    elif not quality["pass"]:
        needs_vision, reason = True, "quality_fail"
    elif embedded_image_count >= 1 and not is_full_page_image and _figures_present(page, images, cfg):
        needs_vision, reason = True, "has_figures"
    else:
        needs_vision, reason = False, "text_sufficient"

    # A blank page carries no real prose — the only surviving text is the Bates stamp; label it honestly.
    text_format = "plain" if reason == "blank" else rec["format"]

    # flags -------------------------------------------------------------------
    flags: list[str] = list(rec["flags"])
    if reason == "blank":
        if blank_kind == "black":
            flags.append("blank_black")
        else:
            flags.append("slip_sheet" if embedded_image_count > 0 else "blank_page")
    if ctx.invisible_glyphs_total >= 3 * max(1, ctx.visible_glyphs_total):
        flags.append("heavy_invisible_layer")
    if embedded_image_count > 0:
        flags.append("image_safety_required")  # Spec §10
    if page.rotation:
        flags.append("rotated")

    return {
        "page_number": page_number,
        "page_class": page_class,
        "needs_vision": bool(needs_vision),
        "needs_vision_reason": reason,
        "text": {
            "content": content,
            "format": text_format,
            "char_count": char_count,
            "visible_chars": ctx.visible_glyphs_total,
            "invisible_chars": ctx.invisible_glyphs_total,
        },
        "metrics": {
            "text_layer_type": layer["text_layer_type"],
            "distinct_font_sizes": layer["distinct_font_sizes"],
            "distinct_fonts": layer["distinct_fonts"],
            "embedded_image_count": embedded_image_count,
            "max_image_coverage": max_cov,
            "is_full_page_image": bool(is_full_page_image),
            "ink_coverage": round(ink_coverage, 5) if ink_coverage is not None else None,
            "dark_coverage": round(dark_coverage, 5) if dark_coverage is not None else None,
            "bates_stamp": bates_stamp,
            "width_pt": round(float(page.rect.width), 2),
            "height_pt": round(float(page.rect.height), 2),
            "rotation": int(page.rotation),
            "quality": quality,
        },
        "redaction": {
            "occluder_rects": ctx.occluder_rects,
            "dark_occluder_rects": ctx.dark_occluder_rects,
            "invisible_glyphs_total": ctx.invisible_glyphs_total,
            "invisible_glyphs_under_dark": ctx.invisible_glyphs_under_dark,
            "redaction_runs": redaction_runs,
            "scrubbed_char_count": scrubbed_char_count,
            "suspected": bool(suspected),
        },
        "images": images,
        "flags": flags,
        # internal aggregation helpers (stripped before emit)
        "_agg": {
            "visible_chars": ctx.visible_glyphs_total,
            "invisible_chars": ctx.invisible_glyphs_total,
            "scrubbed_chars": scrubbed_char_count,
            "text_chars": char_count,
            "needs_vision": bool(needs_vision),
            "suspected": bool(suspected),
            "blank": reason == "blank",
        },
    }
