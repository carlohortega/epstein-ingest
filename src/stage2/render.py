"""Page rendering (Handoff §5): deterministic PNG renders with a DPI clamp, per-PDF thumbnail, and a
header-only PNG dimension reader (so a resumed run can read an existing render's size without re-rendering).

Determinism: identical input => identical PNG bytes. PyMuPDF's ``pixmap.tobytes("png")`` writes no
timestamp/metadata chunk, so a fixed DPI yields byte-identical output across runs and processes.
"""

from __future__ import annotations

import os
import struct

import fitz


def clamp_render_dpi(page: fitz.Page, requested_dpi: int, max_pixels: int, min_dpi: int) -> int:
    """Lower the DPI for very large pages so the rendered pixmap stays under ``max_pixels`` (OOM guard).

    Stage 1 had no ``clamp_render_dpi`` to reuse (the handoff assumed one), so the clamp lives here.
    A normal US-Letter page at 200 DPI is ~3.7 MP and is never clamped; a large-format engineering
    drawing would be, scaling DPI down by ``sqrt(max_pixels / requested_pixels)``.
    """
    w_pt = float(page.rect.width)
    h_pt = float(page.rect.height)
    if w_pt <= 0 or h_pt <= 0:
        return requested_dpi
    px = (w_pt / 72.0 * requested_dpi) * (h_pt / 72.0 * requested_dpi)
    if px <= max_pixels:
        return requested_dpi
    scaled = int(requested_dpi * (max_pixels / px) ** 0.5)
    return max(min_dpi, scaled)


def _atomic_write_png(png_bytes: bytes, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(png_bytes)
    os.replace(tmp, path)  # atomic: a crash never leaves a half-written render


def render_page_png(page: fitz.Page, dest_path: str, dpi: int, *, force: bool) -> tuple[int, int]:
    """Render ``page`` to ``dest_path`` at ``dpi`` (RGB). Idempotent: if the file already exists and not
    ``force``, the render is skipped and its dimensions are read from the existing PNG header.

    Returns ``(width_px, height_px)`` of the render.
    """
    if os.path.exists(dest_path) and not force:
        dims = png_dimensions(dest_path)
        if dims is not None:
            return dims
        # unreadable/partial existing file -> fall through and re-render
    pix = page.get_pixmap(dpi=dpi)  # RGB, no alpha
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    _atomic_write_png(pix.tobytes("png"), dest_path)
    return pix.width, pix.height


def render_thumbnail(page: fitz.Page, dest_path: str, max_px: int, *, force: bool) -> tuple[int, int]:
    """Render a downscaled thumbnail of ``page`` whose longest side is ~``max_px``. Deterministic."""
    if os.path.exists(dest_path) and not force:
        dims = png_dimensions(dest_path)
        if dims is not None:
            return dims
    longest_pt = max(float(page.rect.width), float(page.rect.height)) or 1.0
    dpi = max(1, int(round(max_px * 72.0 / longest_pt)))
    pix = page.get_pixmap(dpi=dpi)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    _atomic_write_png(pix.tobytes("png"), dest_path)
    return pix.width, pix.height


def png_dimensions(path: str) -> tuple[int, int] | None:
    """Read ``(width, height)`` from a PNG's IHDR chunk without decoding the image. ``None`` if the file
    is not a valid PNG header (e.g. a truncated/partial render)."""
    try:
        with open(path, "rb") as f:
            head = f.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n" or head[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", head[16:24])
    return int(w), int(h)
