"""Embedded photo-artifact extraction (Handoff §6).

For pages with embedded raster images, extract each one via PyMuPDF, normalize to PNG, and write to
``<name>/artifacts/page-<NNNN>-img-<MM>.png`` — **deduped by content hash** (a logo repeated on every
page is written once; every occurrence is recorded pointing at the single file). Pure extraction /
normalization only: no captioning or safety here (that is Stage 4).
"""

from __future__ import annotations

import hashlib
import os

import fitz


def _normalize_xref_to_png(doc: fitz.Document, xref: int) -> tuple[bytes, int, int]:
    """Decode an image xref to normalized PNG bytes + ``(width, height)``. CMYK/odd colorspaces are
    converted to RGB so the output is always a portable PNG."""
    pix = fitz.Pixmap(doc, xref)
    try:
        if pix.n - pix.alpha >= 4:           # CMYK (or more) -> RGB
            pix = fitz.Pixmap(fitz.csRGB, pix)
        return pix.tobytes("png"), int(pix.width), int(pix.height)
    finally:
        pix = None  # release the pixmap buffer promptly


def _atomic_write(data: bytes, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def extract_page_artifacts(doc: fitz.Document, page: fitz.Page, page_number: int, base_dir: str,
                           name: str, *, min_px: int, force: bool, full_page_xrefs: set,
                           seen: dict, assets: list, counts: dict) -> None:
    """Extract every embedded *photo* raster on ``page``, deduping across the whole PDF via ``seen``.

    A "photo artifact" is a sub-page embedded image (figure/photo/logo). Full-page backing scans are NOT
    artifacts: on an ``image`` page the backing scan IS the page render (registered as a page asset), so
    re-registering it here would send the identical pixels to vision twice (Handoff §4 distinguishes
    full-page backing scans from mid-size figures). ``full_page_xrefs`` (from Stage-1 ``images[].is_full_page``)
    are therefore skipped and tallied separately, not extracted.

    Mutates shared state so the caller can run this per page while accumulating one manifest:
      * ``seen``   : sha256 -> the canonical artifact asset dict (for occurrence aggregation)
      * ``assets`` : ordered list of unique artifact asset dicts (first-occurrence order)
      * ``counts`` : ``{"kept": int, "skipped": int, "full_page": int}``
    """
    # full=True -> stable per-page order; index MM is the 1-based position in this list (a given xref
    # therefore always maps to the same MM, regardless of which images get skipped).
    for idx, img in enumerate(page.get_images(full=True), start=1):
        xref = int(img[0]) if img else 0
        if xref <= 0:
            continue
        if xref in full_page_xrefs:          # full-page backing scan -> the page render, not a photo artifact
            counts["full_page"] += 1
            continue
        try:
            png, w, h = _normalize_xref_to_png(doc, xref)
        except Exception:
            counts["skipped"] += 1          # undecodable (e.g. a stencil mask) -> skip, never abort
            continue
        if w < min_px or h < min_px:        # icon/sliver too small to carry content (Handoff §6)
            counts["skipped"] += 1
            continue

        sha = hashlib.sha256(png).hexdigest()
        occ = {"page_number": page_number, "image_index": idx}
        existing = seen.get(sha)
        if existing is not None:            # duplicate (e.g. a repeated logo) -> one file, record occurrence
            existing["occurrences"].append(occ)
            continue

        rel = f"{name}/artifacts/page-{page_number:04d}-img-{idx:02d}.png"
        dest = os.path.join(base_dir, name, "artifacts", f"page-{page_number:04d}-img-{idx:02d}.png")
        if not (os.path.exists(dest) and not force):
            _atomic_write(png, dest)
        asset = {
            "kind": "artifact",
            "page_number": page_number,
            "image_index": idx,
            "path": rel,
            "content_sha": sha,
            "width_px": w,
            "height_px": h,
            "occurrences": [occ],
        }
        seen[sha] = asset
        assets.append(asset)
        counts["kept"] += 1
