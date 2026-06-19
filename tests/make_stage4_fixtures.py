"""Stage-4 synthetic fixtures — self-contained sidecars + staged PNGs (no PyMuPDF, no Azure).

Stage 4 never opens the PDF: it reads each sidecar's ``image_assets[]`` and the staged PNG at every
asset's ``path``. So these fixtures write the sidecars and the PNGs (and an empty ``*.pdf`` so the walk
finds them) directly — which also makes the rendered PNG the GATE ORACLE the acceptance test checks
against (Handoff §10.2), exactly like the real corpus.

Corpus:
  alpha.pdf — four image assets exercising every path:
    p1 image  CONTENT  -> vision keeps it (has_visual_content true) + embedding + (no CS: page, not artifact)
    p2 image  BLANK     -> Stage-1 dark_coverage below threshold -> METRIC pre-gate (no vision call);
                          with --no-pregate the vision fake calls it blank -> page_kind demoted to blank
    p3 escalated low_quality_text near-white -> no dark metric -> PNG-MEASURED pre-gate
    p4 artifact CONTENT -> vision keeps it + embedding + Content Safety scanned (artifacts-only default)
  beta.pdf  — one content image asset (used for the content-filter + error injection runs)
"""

from __future__ import annotations

import json
import os

from PIL import Image, ImageDraw

WHITE = (255, 255, 255)


def _content_png(path: str, size=(400, 520)) -> None:
    """A clearly non-blank image: white background with bold black blocks (dark fraction well above any
    pre-gate threshold, so it always reaches vision and the fake calls it content)."""
    im = Image.new("RGB", size, WHITE)
    d = ImageDraw.Draw(im)
    d.rectangle([40, 40, 360, 180], fill=(10, 10, 10))
    d.rectangle([60, 240, 340, 300], fill=(20, 20, 20))
    d.ellipse([120, 340, 280, 480], fill=(0, 0, 0))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)


def _blank_png(path: str, level: int = 255, size=(400, 520)) -> None:
    """A blank / near-blank page (uniform light fill) -> ~zero near-black pixels."""
    im = Image.new("RGB", size, (level, level, level))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    im.save(path)


def _sidecar(pdf_path: str, pages: list[dict], image_assets: list[dict]) -> None:
    sc = {
        "status": "ok",
        "stage2": {"tool": "fixture", "tool_version": "0", "counts": {"image_pages": 1}},
        "pages": pages,
        "image_assets": image_assets,
    }
    with open(os.path.splitext(pdf_path)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(sc, f, indent=2)


def build_stage4(target: str) -> dict:
    os.makedirs(target, exist_ok=True)

    # --- alpha.pdf --------------------------------------------------------------------------------
    alpha_pdf = os.path.join(target, "alpha.pdf")
    open(alpha_pdf, "wb").close()
    a = os.path.join(target, "alpha")
    _content_png(os.path.join(a, "images", "page-0001.png"))
    _blank_png(os.path.join(a, "images", "page-0002.png"), level=255)
    _blank_png(os.path.join(a, "images", "page-0003.png"), level=250)
    _content_png(os.path.join(a, "artifacts", "page-0004-img-01.png"), size=(220, 220))
    _sidecar(alpha_pdf,
             pages=[
                 {"page_number": 1, "page_kind": "image", "flags": ["image_safety_required"],
                  "metrics": {"dark_coverage": 0.13, "max_image_coverage": 0.978}},
                 {"page_number": 2, "page_kind": "image", "flags": [],
                  "metrics": {"dark_coverage": 0.001, "max_image_coverage": 0.978}},
                 {"page_number": 3, "page_kind": "text", "flags": ["low_quality_text"],
                  "metrics": {"dark_coverage": None}},
                 {"page_number": 4, "page_kind": "text", "flags": [], "metrics": {"dark_coverage": None}},
             ],
             image_assets=[
                 {"kind": "page", "page_number": 1, "path": "alpha/images/page-0001.png", "coverage": 0.978},
                 {"kind": "page", "page_number": 2, "path": "alpha/images/page-0002.png", "coverage": 0.978},
                 {"kind": "page", "page_number": 3, "path": "alpha/images/page-0003.png",
                  "route_reason": "low_quality_text"},
                 {"kind": "artifact", "page_number": 4, "path": "alpha/artifacts/page-0004-img-01.png"},
             ])

    # --- beta.pdf (single content asset; for content-filter / error injection runs) ----------------
    beta_pdf = os.path.join(target, "beta.pdf")
    open(beta_pdf, "wb").close()
    b = os.path.join(target, "beta")
    _content_png(os.path.join(b, "images", "page-0001.png"))
    _sidecar(beta_pdf,
             pages=[{"page_number": 1, "page_kind": "image", "flags": [],
                     "metrics": {"dark_coverage": 0.2}}],
             image_assets=[{"kind": "page", "page_number": 1, "path": "beta/images/page-0001.png",
                            "coverage": 0.978}])

    return {"alpha": alpha_pdf, "beta": beta_pdf}


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tfe_stage4_fixtures"
    for k, v in build_stage4(out).items():
        print(f"{k:8s} {v}")
