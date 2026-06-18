"""Embedded-image inventory (Spec §5 ``images[]``, §10).

Records references only — never extracts/transcodes image bytes (keeps Stage 1 cheap). Uses
``get_image_info(xrefs=True)`` so we get placement bbox + dimensions + colorspace without decoding.
"""

from __future__ import annotations

import fitz

from .config import Config


def page_images(page: fitz.Page, cfg: Config) -> tuple[list[dict], float]:
    """Return (images[], max_image_coverage). One entry per raster placement on the page."""
    page_area = abs(page.rect.get_area()) or 1.0
    out: list[dict] = []
    max_cov = 0.0
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    # stable order: top-to-bottom, left-to-right by placement bbox
    infos = sorted(infos, key=lambda i: (round(i.get("bbox", (0, 0, 0, 0))[1], 1),
                                         round(i.get("bbox", (0, 0, 0, 0))[0], 1),
                                         i.get("xref", 0)))
    for info in infos:
        bbox = [round(float(v), 2) for v in info.get("bbox", (0.0, 0.0, 0.0, 0.0))]
        cov = abs(fitz.Rect(bbox).get_area()) / page_area
        is_full = cov >= cfg.full_page_threshold
        max_cov = max(max_cov, cov)
        out.append({
            "xref": int(info.get("xref", 0)),
            "bbox": bbox,
            "width_px": int(info.get("width", 0)),
            "height_px": int(info.get("height", 0)),
            "coverage": round(cov, 4),
            "is_full_page": bool(is_full),
            "colorspace": info.get("cs-name") or info.get("colorspace") or None,
        })
    return out, round(max_cov, 4)
