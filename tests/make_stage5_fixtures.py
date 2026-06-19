"""Stage-5 synthetic fixtures — self-contained FROZEN Stage-3/Stage-4 sidecars (no PyMuPDF, no Azure).

Stage 5 never opens the PDF and never re-runs Stages 1-4: it reads each sidecar's frozen ``stage3`` /
``stage4`` blocks, ``document_summary`` (provisional), ``pages[].summary`` / ``text_safety``, and
``image_assets[].{caption,description,has_visual_content,safety,content_filtered}`` and finalizes from
those. So these fixtures write the sidecars (and an empty ``*.pdf`` so the walk finds them) directly, one
per routing archetype (Handoff §2) plus the safety-rollup and reduce-failure cases.

Corpus (stem -> what it exercises):
  text_only        provisional text, NO image_assets/stage4              -> PROMOTE (0 calls)
  fused            text + an embedded-photo ARTIFACT (has_visual_content) -> FUSED  (gate on assets, not
                   + a has_visual_content=false page image (excluded)         page_kind; §10.3)
  image_only       document_summary null + a content image page           -> IMAGE_ONLY (covered_text=0)
  empty            document_summary null + NO usable vision               -> NO_CONTENT (safety ok)
  quarantined      document_summary null + only content was content-filter-blocked (text page + asset)
                                                                          -> NO_CONTENT (safety review +
                                                                             content_filtered; §4)
  review_text      promoted doc whose page is text_safety=review          -> rollup folds text review (§6)
  fail_reduce      image-only whose caption carries FAIL_TOKEN            -> generic reduce error (§10.7)
  filter_reduce    image-only whose caption carries CFILTER_TOKEN         -> reduce content-filter -> quarantine
"""

from __future__ import annotations

import json
import os

FAIL_TOKEN = "FAILME"      # the failing fake client raises a generic error when this is in the reduce input
CFILTER_TOKEN = "BLOCKME"  # the fake client raises content_filter when this is in the reduce input

S3 = {"tool": "text-first-extract-stage3", "tool_version": "1.0.0",
      "prompt_version": "stage3-2026-06-16.1", "model": "gpt-4.1-nano"}
S4 = {"tool": "text-first-extract-stage4", "tool_version": "1.0.0",
      "prompt_version": "stage4-2026-06-19.2", "vision_model": "gpt-5-mini"}


def _ok_text(flag: str = "ok", categories=None) -> dict:
    return {"flag": flag, "categories": categories or []}


def _ok_img(flag: str = "ok") -> dict:
    return {"flag": flag, "categories": [], "minor_indicator": False, "explicit": False}


def _write(target: str, stem: str, sidecar: dict) -> str:
    pdf = os.path.join(target, stem + ".pdf")
    open(pdf, "wb").close()
    with open(os.path.splitext(pdf)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f, indent=2)
    return pdf


def build_stage5(target: str) -> dict:
    os.makedirs(target, exist_ok=True)
    paths: dict = {}

    # --- text_only -> PROMOTE (no image_assets, no stage4) ----------------------------------------
    paths["text_only"] = _write(target, "text_only", {
        "status": "ok", "stage3": dict(S3),
        "document_summary": {"text": "Provisional summary of a two-page internal memo.",
                             "provisional": True, "covered_pages": 2,
                             "safety_rollup": {"review": 0, "pages_flagged": []}},
        "pages": [
            {"page_number": 1, "page_kind": "text", "summary": "Memo introduces the quarterly review.",
             "text_safety": _ok_text()},
            {"page_number": 2, "page_kind": "text", "summary": "Memo concludes with action items.",
             "text_safety": _ok_text()},
        ],
    })

    # --- fused -> FUSED. The ONLY kept vision is an ARTIFACT on a text page (§10.3: gate on
    #     has_visual_content, not page_kind), plus a has_visual_content=false page image that must be
    #     EXCLUDED from the fold. ------------------------------------------------------------------
    paths["fused"] = _write(target, "fused", {
        "status": "ok", "stage3": dict(S3), "stage4": dict(S4),
        "document_summary": {"text": "Provisional: a letter that references an attached photograph.",
                             "provisional": True, "covered_pages": 2,
                             "safety_rollup": {"review": 0, "pages_flagged": []}},
        "pages": [
            {"page_number": 1, "page_kind": "text", "summary": "Letter describing the site visit.",
             "text_safety": _ok_text()},
            {"page_number": 2, "page_kind": "text", "summary": "Letter closes with a signature block.",
             "text_safety": _ok_text()},
            {"page_number": 3, "page_kind": "blank", "flags": ["vision_blank"]},
        ],
        "image_assets": [
            {"kind": "artifact", "page_number": 1, "path": "fused/artifacts/page-0001-img-01.png",
             "caption": "Photograph of two people at a desk.",
             "description": "A black-and-white photo of two individuals seated at a desk with papers.",
             "has_visual_content": True, "safety": _ok_img()},
            {"kind": "page", "page_number": 3, "path": "fused/images/page-0003.png",
             "caption": "Blank scanned page.", "description": "An essentially blank scanned page.",
             "has_visual_content": False, "safety": _ok_img()},
        ],
    })

    # --- image_only -> IMAGE_ONLY (document_summary null; one content image page) ------------------
    paths["image_only"] = _write(target, "image_only", {
        "status": "ok", "stage3": dict(S3), "stage4": dict(S4),
        "document_summary": None,
        "pages": [{"page_number": 1, "page_kind": "image", "flags": []}],
        "image_assets": [
            {"kind": "page", "page_number": 1, "path": "image_only/images/page-0001.png",
             "caption": "Handwritten note.",
             "description": "A handwritten note listing several names and dates.",
             "has_visual_content": True, "safety": _ok_img()},
        ],
    })

    # --- empty -> NO_CONTENT, genuinely empty (no text, no vision, no content-filter) --------------
    paths["empty"] = _write(target, "empty", {
        "status": "ok", "stage3": dict(S3),
        "document_summary": None,
        "pages": [{"page_number": 1, "page_kind": "blank", "flags": []}],
    })

    # --- quarantined -> NO_CONTENT but its only content was content-filter-blocked (§4) ------------
    paths["quarantined"] = _write(target, "quarantined", {
        "status": "ok", "stage3": dict(S3), "stage4": dict(S4),
        "document_summary": None,
        "pages": [{"page_number": 1, "page_kind": "text", "flags": ["content_filtered"],
                   "text_safety": {"flag": "review", "categories": [], "content_filtered": True}}],
        "image_assets": [
            {"kind": "artifact", "page_number": 1, "path": "quarantined/artifacts/page-0001-img-01.png",
             "has_visual_content": False, "content_filtered": True,
             "safety": {"flag": "review", "categories": [], "minor_indicator": False,
                        "explicit": False, "content_filtered": True}},
        ],
    })

    # --- review_text -> PROMOTE but the page is text_safety=review (rollup must fold it; §6) --------
    paths["review_text"] = _write(target, "review_text", {
        "status": "ok", "stage3": dict(S3),
        "document_summary": {"text": "Provisional summary of an incident report.",
                             "provisional": True, "covered_pages": 1,
                             "safety_rollup": {"review": 1, "pages_flagged": ["1"]}},
        "pages": [{"page_number": 1, "page_kind": "text", "summary": "Incident report details.",
                   "text_safety": _ok_text("review", ["violence"])}],
    })

    # --- fail_reduce -> IMAGE_ONLY whose reduce input carries FAIL_TOKEN (generic error; §10.7) -----
    paths["fail_reduce"] = _write(target, "fail_reduce", {
        "status": "ok", "stage3": dict(S3), "stage4": dict(S4),
        "document_summary": None,
        "pages": [{"page_number": 1, "page_kind": "image", "flags": []}],
        "image_assets": [
            {"kind": "page", "page_number": 1, "path": "fail_reduce/images/page-0001.png",
             "caption": f"Photo {FAIL_TOKEN}", "description": f"contains {FAIL_TOKEN}",
             "has_visual_content": True, "safety": _ok_img()},
        ],
    })

    # --- filter_reduce -> IMAGE_ONLY whose reduce input carries CFILTER_TOKEN (block -> quarantine) -
    paths["filter_reduce"] = _write(target, "filter_reduce", {
        "status": "ok", "stage3": dict(S3), "stage4": dict(S4),
        "document_summary": None,
        "pages": [{"page_number": 1, "page_kind": "image", "flags": []}],
        "image_assets": [
            {"kind": "page", "page_number": 1, "path": "filter_reduce/images/page-0001.png",
             "caption": f"Photo {CFILTER_TOKEN}", "description": f"contains {CFILTER_TOKEN}",
             "has_visual_content": True, "safety": _ok_img()},
        ],
    })

    return paths


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tfe_stage5_fixtures"
    for k, v in build_stage5(out).items():
        print(f"{k:14s} {v}")
