"""Stage-2-specific synthetic fixtures, layered on top of the Stage-1 fixtures (tests.make_fixtures).

The Stage-1 set already covers most page_kinds once classified by Stage 2:
  text_born_digital -> text   |  scanned_content -> image  |  blank_slip/black_blank -> blank
  figure_page -> mixed        |  redacted_leak -> text (real text layer survives -> NOT image)

These add the cases Stage 2 needs that Stage 1 didn't exercise:
  text_with_logo.pdf   - text + a tiny logo (coverage << figure threshold) -> text (logo is NOT a figure)
  fully_redacted.pdf   - invisible OCR under page-covering bars + only a Bates footer -> redacted
  repeated_logo.pdf    - the SAME logo on 3 pages -> one deduped artifact file with 3 occurrences
"""

from __future__ import annotations

import os

import fitz

REDACT_BATES = "EFTA00007777"


def _text_with_logo(path: str) -> None:
    """A text page with a small corner logo. The logo's coverage is far below figure_min_coverage, so the
    page must classify as ``text`` (acceptance #2: a logo on a text page is not an image page)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Internal Memorandum", fontsize=16)
    body = ("This memorandum summarizes the quarterly operations review and the budget approved by the "
            "committee. All regional teams reported steady progress against the plan for the year.")
    page.insert_textbox(fitz.Rect(72, 120, 520, 300), body, fontsize=11)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 80, 80))   # 80px source -> survives artifact_min_px
    pix.set_rect(pix.irect, (20, 80, 160))
    page.insert_image(fitz.Rect(500, 60, 530, 90), pixmap=pix)  # 30x30 pt on a 612x792 page -> ~0.0019 cov
    doc.save(path)
    doc.close()


def _fully_redacted(path: str) -> None:
    """Page-covering redaction: a stack of full-width black bars (each below the single-bar area cap), with
    invisible OCR text under each (so Stage 1 records redaction_runs), and only a visible Bates footer.
    Union bar coverage >= fully_redacted_bar_frac AND no readable text -> ``redacted``."""
    doc = fitz.open()
    page = doc.new_page()  # 612 x 792
    for i in range(14):
        y = 40 + i * 50
        # invisible OCR tokens under where the bar will sit (gives Stage 1 glyphs to scrub)
        page.insert_text((60, y + 26), "confidential withheld material " * 3, fontsize=9, render_mode=3)
        page.draw_rect(fitz.Rect(40, y, 572, y + 40), color=(0, 0, 0), fill=(0, 0, 0))
    page.insert_text((250, 780), REDACT_BATES, fontsize=8)  # the only readable text (a footer stamp)
    doc.save(path)
    doc.close()


def _repeated_logo(path: str) -> None:
    """Three text pages, each carrying the identical logo image -> the artifact must be written once and
    recorded with three occurrences (acceptance #4: dedup by content hash)."""
    doc = fitz.open()
    logo = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 120))
    logo.set_rect(logo.irect, (200, 30, 30))                 # uniform -> identical bytes on every page
    for n in range(3):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(72, 120, 520, 300),
                            f"Page {n + 1} of the report. The committee reviewed the operations budget and "
                            f"approved the annual financial plan for every region under review this year.",
                            fontsize=11)
        page.insert_image(fitz.Rect(450, 60, 520, 130), pixmap=logo)  # ~0.02 cov -> a logo, page stays text
    doc.save(path)
    doc.close()


def build_stage2(target: str) -> dict:
    os.makedirs(target, exist_ok=True)
    paths = {
        "text_with_logo": os.path.join(target, "text_with_logo.pdf"),
        "fully_redacted": os.path.join(target, "fully_redacted.pdf"),
        "repeated_logo": os.path.join(target, "repeated_logo.pdf"),
    }
    _text_with_logo(paths["text_with_logo"])
    _fully_redacted(paths["fully_redacted"])
    _repeated_logo(paths["repeated_logo"])
    return paths


if __name__ == "__main__":
    import sys
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tfe_stage2_fixtures"
    for k, v in build_stage2(out).items():
        print(f"{k:16s} {v}")
