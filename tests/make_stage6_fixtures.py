"""Synthetic FROZEN Stage-1.. sidecars for the Stage-6 acceptance tests. Stage 6 never opens the PDF (it
reads the sidecar and writes a sibling chunks.json), so the ``.pdf`` files are empty placeholders that only
need to exist for the walk to find them. Built under a ``DataSet-77/VOL00077/IMAGES/0001`` tree so the
identifier derivation (dataset_key from the abs path, case_key from ``source.relative_path``) has real
segments to match — exactly like the live corpus layout.
"""

from __future__ import annotations

import json
import os

DATASET = "DataSet-77"
CASE = "VOL00077"
BASE = f"{DATASET}/{CASE}/IMAGES/0001"


def _page(n: int, text: str) -> dict:
    return {"page_number": n, "page_kind": "text", "text": {"content": text, "format": "structured_text"}}


def _write(root: str, rel: str, sidecar: dict) -> None:
    pdf = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    open(pdf, "wb").close()
    with open(os.path.splitext(pdf)[0] + ".json", "w", encoding="utf-8") as f:
        json.dump(sidecar, f, ensure_ascii=False, indent=2)


def _ok(rel: str, pages: list[dict], **extra) -> dict:
    # stage5 + document_summary_final sentinels let the tests assert Stage 6 is append-only (§10.3).
    sc = {
        "status": "ok",
        "source": {"relative_path": rel.split(DATASET + "/", 1)[1]},   # -> VOL00077/IMAGES/0001/<stem>.pdf
        "pages": pages,
        "image_assets": [],
        "stage5": {"sentinel": "DO_NOT_TOUCH"},
        "document_summary_final": {"final": True, "sentinel": "DO_NOT_TOUCH"},
    }
    sc.update(extra)
    return sc


def build_stage6(root: str) -> None:
    """Lay down the fixture corpus. Stems (under BASE unless noted):
      * two_text     — 2 text pages                  -> parents>0, 0 skipped
      * mixed_skip   — 1 text page + 1 blank page    -> parents>0, 1 skipped
      * empty_doc    — 1 blank page                   -> parents=0, empty, chunks_ref null, NO file
      * not_ok       — status != ok                   -> skipped_status (gate)
      * no_ids       — path/relative_path lack VOL     -> no_identifiers error (no stage6 block)
    """
    two_text = f"{BASE}/two_text.pdf"
    _write(root, two_text, _ok(two_text, [
        _page(1, "Alpha bravo charlie delta. Echo foxtrot golf hotel india. Juliet kilo lima mike november."),
        _page(2, "Oscar papa quebec romeo. Sierra tango uniform victor whiskey. Xray yankee zulu one two."),
    ]))

    mixed = f"{BASE}/mixed_skip.pdf"
    _write(root, mixed, _ok(mixed, [
        _page(1, "Lorem ipsum dolor sit amet. Consectetur adipiscing elit sed do eiusmod tempor incididunt."),
        {"page_number": 2, "page_kind": "blank", "text": {"content": "   \n  ", "format": "structured_text"}},
    ]))

    empty = f"{BASE}/empty_doc.pdf"
    _write(root, empty, _ok(empty, [
        {"page_number": 1, "page_kind": "blank", "text": {"content": "", "format": "structured_text"}},
    ]))

    not_ok = f"{BASE}/not_ok.pdf"
    _write(root, not_ok, {"status": "error", "error": {"code": "encrypted"},
                          "source": {"relative_path": "x"}, "pages": []})

    # no VOL segment anywhere -> case_key can't be derived -> IdentifierError (§10.5).
    no_ids = f"{DATASET}/loose/no_ids.pdf"
    _write(root, no_ids, {"status": "ok", "source": {"relative_path": "loose/no_ids.pdf"},
                          "pages": [_page(1, "Some chunkable text here for the body of this document.")],
                          "stage5": {"sentinel": "DO_NOT_TOUCH"}})
