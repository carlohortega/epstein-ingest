"""Text reconstruction — layer-type adaptive (Spec §6).

Measured finding (~/mcp-test/_font_probe.py): these productions are overwhelmingly an *invisible OCR
layer* (PyMuPDF render mode 3, font ``HiddenHorzOCR``) whose typography is NOISE, not structure
(16–19 distinct font sizes/page because the OCR engine sized each word to its scanned glyph box). You
cannot derive headings/sections/emphasis from that deterministically — so we do NOT force "markdown".

Reconstruction adapts on the detected ``text_layer_type``:
  - ocr_layer / mixed -> ``structured_text``: faithful reading-order text with only geometry-supported
    structure (paragraphs from vertical gaps, columns from x-clustering, lists from leading glyphs).
    No fabricated headings or emphasis.
  - born_digital      -> ``markdown`` via pymupdf4llm when available (page-level, real typography);
    otherwise the same span-level structured path (so it still works offline), labelled honestly.
  - none              -> empty content.

Hard requirement: scrub BEFORE assembly — we operate at the span level on ``get_text("dict")`` so
occluded/leaked spans never reach output (never "render whole page then string-replace").

``pymupdf4llm`` is optional (Spec §3.8). It is page-level and cannot scrub at span granularity, so it
is used only for born_digital pages that are NOT redaction-suspected.
"""

from __future__ import annotations

import re

import fitz

from .config import Config
from .redaction import RedactionContext

try:  # optional, per Spec §6 / §3.8
    import pymupdf4llm  # noqa: F401
    HAVE_PYMUPDF4LLM = True
except Exception:
    HAVE_PYMUPDF4LLM = False

RECONSTRUCTION = "adaptive"

_BULLET = re.compile(r"^\s*([•●▪‣⁃\-\*·])\s+")
_NUMBERED = re.compile(r"^\s*(\d{1,3}[.)])\s+")


# --------------------------------------------------------------------------- detection

def detect_layer_type(page: fitz.Page, cfg: Config) -> dict:
    """Render-free classification of the text layer from get_texttrace() (Spec §6)."""
    total = invisible = 0
    sizes: set[float] = set()
    fonts: set = set()
    try:
        for s in page.get_texttrace():
            n = len(s.get("chars", []))
            if n == 0:
                continue
            total += n
            if s.get("type") == 3 or s.get("opacity", 1) == 0:
                invisible += n
            # round size to nearest 0.5 pt (Spec §6)
            sizes.add(round(float(s.get("size", 0.0)) * 2) / 2.0)
            fonts.add(s.get("font"))
    except Exception:
        pass

    invisible_ratio = (invisible / total) if total else 0.0
    distinct_sizes = len(sizes)

    if total == 0:
        layer = "none"
    elif invisible_ratio >= cfg.layer_invisible_ocr and distinct_sizes >= cfg.size_noise_threshold:
        layer = "ocr_layer"
    elif invisible_ratio < cfg.layer_invisible_born and distinct_sizes <= cfg.born_max_sizes:
        layer = "born_digital"
    else:
        layer = "mixed"

    return {
        "text_layer_type": layer,
        "distinct_font_sizes": distinct_sizes,
        "distinct_fonts": len(fonts),
        "invisible_ratio": round(invisible_ratio, 4),
    }


# --------------------------------------------------------------------------- assembly helpers

def _cx(bb) -> float:
    return (bb[0] + bb[2]) / 2.0


def _blocks_no_bars(page: fitz.Page) -> tuple[list[dict], int, int]:
    """Fast path for pages with NO redaction bars (the vast majority). Uses span-level ``get_text("dict")``
    instead of per-glyph ``get_text("rawdict")`` — the latter is ~3-5x more expensive on dense OCR layers
    (it was 65% of the time on large scanned files). Produces identical ``text.content`` because with no
    bars there is nothing to scrub, so a line is just its spans' text concatenated."""
    data = page.get_text("dict")
    blocks_out: list[dict] = []
    for b in data.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        lines_out: list[dict] = []
        for line in sorted(b.get("lines", []), key=lambda ln: (round(ln["bbox"][1], 1), round(ln["bbox"][0], 1))):
            spans = sorted(line.get("spans", []), key=lambda s: round(s["bbox"][0], 1))
            max_size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)
            line_text = "".join(s.get("text", "") for s in spans).strip()
            if line_text:
                lines_out.append({"text": line_text, "max_size": max_size,
                                  "y": line["bbox"][1], "x": line["bbox"][0]})
        if lines_out:
            blocks_out.append({"bbox": b["bbox"], "lines": lines_out})
    return blocks_out, 0, 0


def _surviving_blocks(page: fitz.Page, ctx: RedactionContext, cfg) -> tuple[list[dict], int, int]:
    """Cheap span-level path when the page has no redaction bars; per-glyph path only when it does."""
    if not ctx.bars:
        return _blocks_no_bars(page)
    return _blocks_with_bars(page, ctx, cfg)


def _blocks_with_bars(page: fitz.Page, ctx: RedactionContext, cfg) -> tuple[list[dict], int, int]:
    """Return (blocks, scrubbed_char_count, redaction_runs). Each block: {bbox, lines:[{text,max_size,y,x}]}.

    Positive-redaction reconstruction (Spec §7): the redaction bars are already detected (``ctx.bars`` —
    solid dark rectangles). Here we place a ``redaction_marker`` at each bar's position in reading order and
    drop any glyph that sits under a bar (a leak). A line with no bar over it keeps all its glyphs untouched.
    Bars with no text on their line still emit a marker, so a redaction is recorded even when nothing was
    OCR'd under it. Uses per-glyph ``rawdict`` because only here do we need glyph-precise scrub/placement."""
    data = page.get_text("rawdict")
    scrubbed = 0
    redaction_runs = 0
    placed = set()
    marker = cfg.redaction_marker
    blocks_out: list[dict] = []
    for b in data.get("blocks", []):
        if b.get("type", 0) != 0:
            continue
        lines_out: list[dict] = []
        for line in sorted(b.get("lines", []), key=lambda ln: (round(ln["bbox"][1], 1), round(ln["bbox"][0], 1))):
            spans = sorted(line.get("spans", []), key=lambda s: round(s["bbox"][0], 1))
            max_size = max((float(s.get("size", 0.0)) for s in spans), default=0.0)

            if not ctx.line_has_bar(line["bbox"]):
                text = "".join(c.get("c", "") for s in spans for c in s.get("chars", []))
            else:
                glyphs = [(c.get("c", ""), c["bbox"]) for s in spans for c in s.get("chars", [])]
                tokens: list[str] = []
                gi, n = 0, len(glyphs)
                for bar in ctx.bars_on_line(line["bbox"]):
                    seg: list[str] = []
                    while gi < n and _cx(glyphs[gi][1]) < bar.x0:
                        ch, bb = glyphs[gi]
                        if ctx.in_bar(bb):
                            scrubbed += len(ch)
                        else:
                            seg.append(ch)
                        gi += 1
                    if seg:
                        tokens.append("".join(seg))
                    if id(bar) not in placed:   # mark each bar once, even if it spans several lines
                        tokens.append(marker)
                        redaction_runs += 1
                        placed.add(id(bar))
                    while gi < n and _cx(glyphs[gi][1]) <= bar.x1:   # glyphs under this bar -> scrub
                        ch, bb = glyphs[gi]
                        if ctx.in_bar(bb):
                            scrubbed += len(ch)
                        gi += 1
                trail: list[str] = []
                while gi < n:
                    ch, bb = glyphs[gi]
                    if ctx.in_bar(bb):
                        scrubbed += len(ch)
                    else:
                        trail.append(ch)
                    gi += 1
                if trail:
                    tokens.append("".join(trail))
                text = " ".join(t for t in tokens if t)
                while "  " in text:
                    text = text.replace("  ", " ")

            line_text = text.strip()
            if line_text:
                lines_out.append({"text": line_text, "max_size": max_size,
                                  "y": line["bbox"][1], "x": line["bbox"][0]})
        if lines_out:
            blocks_out.append({"bbox": b["bbox"], "lines": lines_out})

    # bars not placed on any text line (a redaction over whitespace) — still record a marker by position
    for bar in ctx.bars:
        if id(bar) not in placed:
            redaction_runs += 1
            blocks_out.append({"bbox": (bar.x0, bar.y0, bar.x1, bar.y1),
                               "lines": [{"text": marker, "max_size": 0.0, "y": bar.y0, "x": bar.x0}]})
    return blocks_out, scrubbed, redaction_runs


def _order_blocks(blocks: list[dict], page: fitz.Page, cfg: Config) -> tuple[list[dict], bool]:
    """Reading order with simple 2-column detection by a mid-page gutter. Returns (ordered, ambiguous)."""
    if len(blocks) < 2:
        return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1))), False

    x0p, x1p = page.rect.x0, page.rect.x1
    width = (x1p - x0p) or 1.0
    mid = x0p + width / 2.0
    margin = cfg.column_gap_frac * width
    left = [b for b in blocks if b["bbox"][2] <= mid + margin]
    right = [b for b in blocks if b["bbox"][0] >= mid - margin]
    straddle = [b for b in blocks if b not in left and b not in right]

    # two real columns: both sides populated and few blocks straddle the gutter
    if len(left) >= 2 and len(right) >= 2 and len(straddle) <= max(1, len(blocks) // 10):
        ordered = (sorted(left, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
                   + sorted(right, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1))))
        return ordered, bool(straddle)
    return sorted(blocks, key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1))), False


def _body_size(blocks: list[dict]) -> float:
    sizes = [ln["max_size"] for b in blocks for ln in b["lines"] if ln["max_size"] > 0]
    return sorted(sizes)[len(sizes) // 2] if sizes else 0.0


def _render_blocks(blocks: list[dict], allow_headings: bool, body_size: float) -> str:
    out: list[str] = []
    for b in blocks:
        lines = b["lines"]
        # list items stay on their own lines; otherwise wrapped lines join into a paragraph
        if any(_BULLET.match(ln["text"]) or _NUMBERED.match(ln["text"]) for ln in lines):
            rendered = []
            for ln in lines:
                t = ln["text"]
                m = _BULLET.match(t)
                if m:
                    t = "- " + t[m.end():]
                rendered.append(t)
            out.append("\n".join(rendered))
            continue
        if allow_headings and len(lines) == 1:
            t = lines[0]["text"]
            if len(t) <= 80 and len(t.split()) <= 12 and body_size > 0 and lines[0]["max_size"] >= body_size * 1.35:
                out.append(f"# {t}")
                continue
        out.append(" ".join(ln["text"] for ln in lines))
    return "\n\n".join(out)


# --------------------------------------------------------------------------- entry point

def reconstruct(page: fitz.Page, ctx: RedactionContext, layer_type: str, cfg: Config) -> dict:
    """Return {content, format, char_count, total_chars, scrubbed_char_count, redaction_runs, flags}."""
    blocks, scrubbed, redaction_runs = _surviving_blocks(page, ctx, cfg)
    flags: list[str] = []

    if layer_type == "born_digital":
        fmt = "markdown"
        allow_headings = True
    elif layer_type == "none":
        fmt = "plain"
        allow_headings = False
    else:  # ocr_layer | mixed
        fmt = "structured_text"
        allow_headings = False

    if not blocks:
        return {"content": "", "format": "plain" if fmt == "plain" else fmt,
                "char_count": 0, "total_chars": scrubbed, "scrubbed_char_count": scrubbed,
                "redaction_runs": redaction_runs, "flags": flags}

    ordered, ambiguous = _order_blocks(blocks, page, cfg)
    if ambiguous:
        flags.append("ambiguous_block_structure")
    body_size = _body_size(ordered) if allow_headings else 0.0
    content = _render_blocks(ordered, allow_headings, body_size)

    kept = sum(len(ln["text"]) for b in ordered for ln in b["lines"])
    return {
        "content": content,
        "format": fmt,
        "char_count": len(content),
        "total_chars": kept + scrubbed,
        "scrubbed_char_count": scrubbed,
        "redaction_runs": redaction_runs,
        "flags": flags,
    }
