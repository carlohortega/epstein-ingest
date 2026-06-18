"""Redaction safety (Spec §7) — the #1 risk: a page "looks redacted" while the OCR text under the
black bar stays extractable. This module detects occluders + the invisible/under-dark layer and
exposes a ``should_scrub`` predicate so Markdown assembly (markdown.py) can drop occluded spans
BEFORE they ever reach the output.

Primitives validated this session in ~/mcp-test/epstein_probe.py / epstein_probe2.py:
  - occluder rects   : opaque filled rectangles from get_drawings(), classified by fill luminance
  - invisible layer  : glyphs drawn in render mode 3 / opacity 0 (get_texttrace())
  - under-dark        : one low-DPI grayscale render; a bbox is "under dark" if >= dark_frac of its
                        rendered pixels are below dark_level (i.e. sitting on a black/redaction region)

Render is performed at most ONCE per page, and skipped entirely when there are no occluders AND no
invisible glyphs (Spec §7 optimization) — keeping the common DataSet-12 page render-free.
"""

from __future__ import annotations

from dataclasses import dataclass

import fitz

from .config import Config


def _luminance(fill) -> float | None:
    if fill is None:
        return None
    try:
        if isinstance(fill, (int, float)):
            return float(fill)
        return sum(fill[:3]) / 3.0
    except Exception:
        return None


def _occluder_rects(page: fitz.Page, cfg: Config) -> list[tuple[fitz.Rect, str]]:
    """Opaque filled rectangles that could be redaction boxes, classified dark/white/other by fill."""
    rects: list[tuple[fitz.Rect, str]] = []
    page_area = abs(page.rect.get_area()) or 1.0
    for d in page.get_drawings():
        fill = d.get("fill")
        if fill is None:
            continue
        r = d.get("rect")
        if r is None:
            continue
        area = abs(r.get_area())
        if area < cfg.min_occluder_area or area > cfg.occluder_max_area_frac * page_area:
            continue  # skip slivers and full-page backgrounds
        lum = _luminance(fill)
        if lum is not None and lum < cfg.dark_lum:
            kind = "dark"
        elif lum is not None and lum > cfg.white_lum:
            kind = "white"
        else:
            kind = "other"
        rects.append((fitz.Rect(r), kind))
    return rects


def _invisible_glyph_count(page: fitz.Page) -> tuple[int, int]:
    """(invisible_glyphs, visible_glyphs) from get_texttrace() render mode 3 / opacity 0."""
    invis = vis = 0
    try:
        for span in page.get_texttrace():
            n = len(span.get("chars", []))
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                invis += n
            else:
                vis += n
    except Exception:
        pass
    return invis, vis


def _invisible_char_boxes(page: fitz.Page) -> list[tuple[float, float, float, float]]:
    """bboxes of every invisible glyph (render mode 3 / opacity 0)."""
    boxes: list[tuple[float, float, float, float]] = []
    try:
        for span in page.get_texttrace():
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                for c in span.get("chars", []):
                    box = c[3] if len(c) > 3 else None
                    if box:
                        boxes.append(tuple(box))
    except Exception:
        pass
    return boxes


class _GrayRender:
    """One low-DPI grayscale render; answers ``dark_frac(bbox)`` by sampling pixels directly from the
    pixmap buffer (no PIL — keeps the dependency surface to PyMuPDF + stdlib)."""

    def __init__(self, page: fitz.Page, dpi: int):
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        self.samples = pix.samples           # bytes, 1 byte/pixel
        self.stride = pix.stride
        self.width = pix.width
        self.height = pix.height
        self.scale = dpi / 72.0
        self.ox = page.rect.x0
        self.oy = page.rect.y0

    def region_stats(self, bbox) -> tuple[float, float, int]:
        """(mean, stddev, pixel_count) over ``bbox`` — used to tell a solid fill bar (low stddev) from a
        textured dark photo (high stddev)."""
        x0 = int((bbox[0] - self.ox) * self.scale)
        y0 = int((bbox[1] - self.oy) * self.scale)
        x1 = int((bbox[2] - self.ox) * self.scale)
        y1 = int((bbox[3] - self.oy) * self.scale)
        x0, x1 = sorted((max(0, min(x0, self.width)), max(0, min(x1, self.width))))
        y0, y1 = sorted((max(0, min(y0, self.height)), max(0, min(y1, self.height))))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return 255.0, 0.0, 0
        s = self.samples
        stride = self.stride
        total = sq = cnt = 0
        for y in range(y0, y1):
            base = y * stride
            row = s[base + x0: base + x1]
            cnt += len(row)
            total += sum(row)
            sq += sum(v * v for v in row)
        if cnt == 0:
            return 255.0, 0.0, 0
        mean = total / cnt
        var = max(0.0, sq / cnt - mean * mean)
        return mean, var ** 0.5, cnt

    def dark_frac(self, bbox, dark_level: int) -> float:
        x0 = int((bbox[0] - self.ox) * self.scale)
        y0 = int((bbox[1] - self.oy) * self.scale)
        x1 = int((bbox[2] - self.ox) * self.scale)
        y1 = int((bbox[3] - self.oy) * self.scale)
        x0, x1 = sorted((max(0, min(x0, self.width)), max(0, min(x1, self.width))))
        y0, y1 = sorted((max(0, min(y0, self.height)), max(0, min(y1, self.height))))
        if x1 - x0 < 1 or y1 - y0 < 1:
            return 0.0
        dark = tot = 0
        s, stride = self.samples, self.stride
        for y in range(y0, y1):
            base = y * stride
            row = s[base + x0: base + x1]
            tot += len(row)
            dark += sum(1 for v in row if v < dark_level)
        return (dark / tot) if tot else 0.0

    def _px_to_pt(self, x0, y0, x1, y1) -> tuple:
        return (x0 / self.scale + self.ox, y0 / self.scale + self.oy,
                x1 / self.scale + self.ox, y1 / self.scale + self.oy)

    def find_dark_bars(self, cfg) -> list:
        """Find solid dark RECTANGLES (redaction bars) by scanning the render for near-black horizontal
        runs and merging vertically. Returns bar rects in PDF points. Subsampled (``bar_scan_step``) for
        speed; each candidate is then verified at full resolution for solidity (so a textured dark photo,
        which has no solid black rectangle, is rejected)."""
        step = max(1, cfg.bar_scan_step)
        w, h, stride, s = self.width, self.height, self.stride, self.samples
        dark_level = cfg.dark_level
        min_run_px = max(1, int(cfg.bar_min_width_pt * self.scale))

        open_rects: list[list] = []   # [x0, x1, y0, y_last]
        closed: list[list] = []
        for gy in range(0, h, step):
            base = gy * stride
            runs: list[tuple] = []
            x = 0
            start = None
            while x < w:
                if s[base + x] < dark_level:
                    if start is None:
                        start = x
                else:
                    if start is not None:
                        if x - start >= min_run_px:
                            runs.append((start, x))
                        start = None
                x += step
            if start is not None and w - start >= min_run_px:
                runs.append((start, w))

            used = [False] * len(runs)
            new_open: list[list] = []
            for r in open_rects:
                hit = False
                for k, (rx0, rx1) in enumerate(runs):
                    if not used[k] and not (rx1 < r[0] or rx0 > r[1]):  # x overlap with prior row
                        r[0] = min(r[0], rx0); r[1] = max(r[1], rx1); r[3] = gy
                        used[k] = True; hit = True
                        new_open.append(r)
                        break
                if not hit:
                    closed.append(r)
            for k, (rx0, rx1) in enumerate(runs):
                if not used[k]:
                    new_open.append([rx0, rx1, gy, gy])
            open_rects = new_open
        closed += open_rects

        bars: list = []
        page_area_px = w * h or 1
        min_w = cfg.bar_min_width_pt * self.scale
        min_h = cfg.bar_min_height_pt * self.scale
        max_h = cfg.bar_max_height_pt * self.scale
        for x0, x1, y0, y_last in closed:
            bw = x1 - x0
            bh = (y_last - y0) + step
            if bw < min_w or bh < min_h or bh > max_h:
                continue
            if bw * bh > cfg.bar_max_area_frac * page_area_px:
                continue
            pt = self._px_to_pt(x0, y0, x1, y_last + step)
            # verify solidity on the INTERIOR (inset) — a real bar's interior is pure black, a dark
            # photo region's interior is gray; edges/white margins of the detected rect are excluded.
            f = cfg.bar_interior_inset
            dx = (pt[2] - pt[0]) * f
            dy = (pt[3] - pt[1]) * f
            inner = (pt[0] + dx, pt[1] + dy, pt[2] - dx, pt[3] - dy)
            mean, _std, nn = self.region_stats(inner)
            if nn >= 4 and mean < cfg.redaction_solid_mean_max and \
               self.dark_frac(inner, dark_level) >= cfg.bar_solid_dark_frac:
                bars.append(fitz.Rect(pt))
        return bars


def _merge_bars(bars: list) -> list:
    """Union overlapping bar rects so a bar found by both the vector and raster paths is marked once."""
    merged: list = []
    for b in sorted(bars, key=lambda r: (round(r.y0, 1), round(r.x0, 1))):
        hit = False
        for m in merged:
            inter = b & m
            if inter.is_valid and abs(inter.get_area()) > 0:
                m.x0, m.y0 = min(m.x0, b.x0), min(m.y0, b.y0)
                m.x1, m.y1 = max(m.x1, b.x1), max(m.y1, b.y1)
                hit = True
                break
        if not hit:
            merged.append(fitz.Rect(b))
    return merged


@dataclass
class RedactionContext:
    """Carries the detected redaction bars + diagnostics. Bars are the authoritative redaction signal:
    they are solid dark rectangles (vector fills OR rasterized), found independently of any OCR text, so
    a redaction is detected and positionally marked even when no text sits under the bar."""
    occluder_rects: int
    dark_occluder_rects: int
    invisible_glyphs_total: int
    visible_glyphs_total: int
    invisible_glyphs_under_dark: int
    bars: list           # list[fitz.Rect] in PDF points
    _cfg: Config

    def line_has_bar(self, line_bbox) -> bool:
        """Cheap pre-check: does any bar vertically overlap this text line? (skip the slow path if not)."""
        ly0, ly1 = line_bbox[1], line_bbox[3]
        lh = (ly1 - ly0) or 1.0
        for bar in self.bars:
            ov = min(ly1, bar.y1) - max(ly0, bar.y0)
            if ov >= 0.5 * lh:
                return True
        return False

    def bars_on_line(self, line_bbox) -> list:
        """Bars that substantially cover this line (>= half the line height), left-to-right."""
        ly0, ly1 = line_bbox[1], line_bbox[3]
        lh = (ly1 - ly0) or 1.0
        out = [bar for bar in self.bars if (min(ly1, bar.y1) - max(ly0, bar.y0)) >= 0.5 * lh]
        return sorted(out, key=lambda r: r.x0)

    def in_bar(self, bbox) -> bool:
        """Is a glyph bbox at least ``bar_overlap`` inside any bar? (geometric — no pixel sampling)."""
        wr = fitz.Rect(bbox)
        warea = abs(wr.get_area()) or 1.0
        for bar in self.bars:
            inter = wr & bar
            if inter.is_valid and abs(inter.get_area()) / warea >= self._cfg.bar_overlap:
                return True
        return False


def page_coverage(page: fitz.Page, dpi: int, white_level: int, dark_level: int) -> tuple[float, float]:
    """One low-DPI grayscale render -> (ink_coverage, dark_coverage).

    ``ink_coverage`` = fraction of pixels darker than ``white_level`` (anything that isn't white).
    ``dark_coverage`` = fraction of pixels darker than ``dark_level`` (near-black).

    Used by the blank/slip-sheet gate (Spec §9a): a page that is essentially white-except-a-stamp has
    near-zero ink_coverage; a solid-black scanner blank has near-one dark_coverage. C-speed via translate
    tables + count (no per-pixel Python loop), so it stays cheap even when most pages are full-page scans.
    """
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
    s = pix.samples
    if not s:
        return 0.0, 0.0
    n = len(s)
    ink_tbl = bytes(1 if i < white_level else 0 for i in range(256))
    dark_tbl = bytes(1 if i < dark_level else 0 for i in range(256))
    return s.translate(ink_tbl).count(1) / n, s.translate(dark_tbl).count(1) / n


def analyze_redaction(page: fitz.Page, cfg: Config) -> RedactionContext:
    occluders = _occluder_rects(page, cfg)
    dark_rects = [r for r, kind in occluders if kind == "dark"]
    invis_total, vis_total = _invisible_glyph_count(page)

    # Bars = vector dark fill rects (solid by construction) + solid dark rectangles found in the raster.
    bars: list = [fitz.Rect(r) for r in dark_rects]
    # Render when there could be a rasterized bar: an embedded image, an invisible layer, or an occluder.
    if occluders or invis_total > 0 or len(page.get_images()) > 0:
        gray = _GrayRender(page, cfg.render_dpi)
        bars.extend(gray.find_dark_bars(cfg))
    bars = _merge_bars(bars)

    # Diagnostic: invisible glyphs whose center sits inside a detected bar (cheap, geometric).
    under = 0
    if bars:
        for bx in _invisible_char_boxes(page):
            cx, cy = (bx[0] + bx[2]) / 2.0, (bx[1] + bx[3]) / 2.0
            if any(b.x0 <= cx <= b.x1 and b.y0 <= cy <= b.y1 for b in bars):
                under += 1

    return RedactionContext(
        occluder_rects=len(occluders),
        dark_occluder_rects=len(dark_rects),
        invisible_glyphs_total=invis_total,
        visible_glyphs_total=vis_total,
        invisible_glyphs_under_dark=under,
        bars=bars,
        _cfg=cfg,
    )
