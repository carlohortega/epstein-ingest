"""Stage-3-specific synthetic fixtures, layered on the Stage-1/2 sets.

Stages 1–2 already produce every page_kind Stage 3 routes on (text/mixed summarized; image/blank/redacted
skipped) plus a redacted page whose under-bar SECRET was scrubbed to [REDACTED] (acceptance §10.3). These
add two born-digital text pages the fake client keys off:

  sensitive_text.pdf - contains the word VIOLENCE -> the fake flags text_safety=review (acceptance §10.5)
  fail_page.pdf      - contains the token FAILME   -> the failing fake raises -> summary_error (§10.6)
  filtered_page.pdf  - contains the token BLOCKME  -> the fake raises content_filter -> review + logged
"""

from __future__ import annotations

import os

import fitz

SENSITIVE_WORD = "VIOLENCE"
FAIL_TOKEN = "FAILME"
CFILTER_TOKEN = "BLOCKME"   # the fake client raises a content_filter error when this is on the page


def _text_page(path: str, body: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Internal Note", fontsize=16)
    page.insert_textbox(fitz.Rect(72, 120, 520, 320), body, fontsize=11)
    doc.save(path)
    doc.close()


def build_stage3(target: str) -> dict:
    os.makedirs(target, exist_ok=True)
    paths = {
        "sensitive": os.path.join(target, "sensitive_text.pdf"),
        "fail": os.path.join(target, "fail_page.pdf"),
        "filtered": os.path.join(target, "filtered_page.pdf"),
    }
    _text_page(paths["sensitive"],
               f"This report documents an incident involving {SENSITIVE_WORD} during the operation and "
               "the committee response that followed across all regions reviewed this fiscal year.")
    _text_page(paths["fail"],
               f"This page carries the token {FAIL_TOKEN} that the failing test client rejects, while the "
               "remaining pages of the corpus summarize normally and the run completes end to end.")
    _text_page(paths["filtered"],
               f"This page carries the token {CFILTER_TOKEN} that the fake client treats as content the "
               "Azure policy blocks, so it must be flagged review and recorded in the failures log.")
    return paths


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tfe_stage3_fixtures"
    for k, v in build_stage3(out).items():
        print(f"{k:12s} {v}")
