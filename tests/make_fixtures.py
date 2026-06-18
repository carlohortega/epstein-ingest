"""Generate synthetic PDF fixtures that exercise each routing path, so the tool can be validated
without the real (sensitive, off-box) corpora. Writes into a target directory.

Fixtures:
  text_born_digital.pdf  - real vector text, few sizes -> text_sufficient (born_digital)
  ocr_layer.pdf          - invisible OCR layer (render mode 3), many sizes -> text_sufficient (ocr_layer)
  redacted_leak.pdf      - invisible tokens UNDER a black bar -> redaction_suspected, tokens scrubbed
  garbage_microfilm.pdf  - a few chars of edge noise -> quality_fail / low_text
  figure_page.pdf        - text + a mid-size embedded image with no text over it -> has_figures
  encrypted.pdf          - password protected -> status:error code:encrypted
  corrupt.pdf            - not a PDF at all -> status:error code:corrupt
"""

from __future__ import annotations

import os
import sys

import fitz

SECRET = "TOPSECRETNAME"  # the under-bar token that must never survive scrubbing


def _born_digital(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), "Quarterly Report", fontsize=18)
    body = ("The committee reviewed the financial results for the quarter and approved the budget "
            "for the next fiscal year. Revenue increased across all regions while costs remained flat. "
            "The board thanked the staff for their diligent and careful work this period.")
    page.insert_textbox(fitz.Rect(72, 120, 520, 320), body, fontsize=11)
    doc.save(path)
    doc.close()


def _ocr_layer(path: str) -> None:
    """Simulate an OCR layer: invisible text (render mode 3) at many noisy sizes over a light page."""
    doc = fitz.open()
    page = doc.new_page()
    words = ("the quick brown fox jumps over the lazy dog while the committee reviewed financial "
             "statements and approved the annual budget for operations across every region this year").split()
    x, y = 72, 100
    for i, w in enumerate(words):
        size = 6.0 + (i % 13) * 0.5  # many distinct sizes -> typographic noise
        page.insert_text((x, y), w + " ", fontsize=size, render_mode=3)
        x += 5 + len(w) * 5
        if x > 480:
            x, y = 72, y + 22
    doc.save(path)
    doc.close()


def _redacted_leak(path: str) -> None:
    """Invisible OCR tokens, some sitting UNDER a solid black bar (recoverable redaction = leak)."""
    doc = fitz.open()
    page = doc.new_page()
    safe = ("meeting notes for the quarterly committee review of the operations budget and the annual "
            "financial plan approved by the board members present at the session today this year here now").split()
    x, y = 72, 100
    for i, w in enumerate(safe):
        size = 6.0 + (i % 11) * 0.5
        page.insert_text((x, y), w + " ", fontsize=size, render_mode=3)
        x += 5 + len(w) * 5
        if x > 480:
            x, y = 72, y + 22
    # a line of secret tokens, invisible, at a fixed spot...
    bar_y = y + 40
    page.insert_text((80, bar_y), f"{SECRET} {SECRET} {SECRET}", fontsize=9, render_mode=3)
    # ...then cover that spot with a solid black redaction bar
    page.draw_rect(fitz.Rect(74, bar_y - 11, 320, bar_y + 4), color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(path)
    doc.close()


PARTIAL_SECRET = "SECRETXYZ"


def _partial_redaction(path: str) -> None:
    """A bar covering ONLY the middle token of a line. Exercises glyph-level scrub: the outer words must
    survive, the under-bar token must be removed (a span-level scrub would wrongly keep the whole line)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 400), "PUBLICA SECRETXYZ PUBLICB", fontsize=12)
    page.draw_rect(fitz.Rect(123, 389, 193, 403), color=(0, 0, 0), fill=(0, 0, 0))  # covers only SECRETXYZ
    doc.save(path)
    doc.close()


def _raster_redaction(path: str) -> None:
    """A solid-black RASTER bar (burned into an image, not a vector rect) over the middle word. The
    middle word must become [REDACTED]; the outer words survive; the page must be flagged."""
    doc = fitz.open()
    page = doc.new_page()
    # words spaced apart so the bar (with margin) covers only the middle one
    page.insert_text((72, 400), "ALPHA", fontsize=12)
    page.insert_text((250, 400), "BRAVO", fontsize=12)
    page.insert_text((430, 400), "CHARLIE", fontsize=12)
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 110, 48))
    pix.set_rect(pix.irect, (0, 0, 0))                       # uniform solid black
    page.insert_image(fitz.Rect(244, 386, 300, 406), pixmap=pix, keep_proportion=False)  # covers BRAVO
    doc.save(path)
    doc.close()


def _dark_photo(path: str) -> None:
    """Text over a TEXTURED dark photo (alternating bands = high variance). Must NOT be treated as a
    redaction: the text is kept, no [REDACTED] marker, page not flagged. (The real-data false positive.)"""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 400), "ONE TWO THREE FOUR", fontsize=12)
    w, h = 220, 20
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, w, h))
    for y in range(h):
        v = 0 if (y // 2) % 2 == 0 else 150                 # dark, but high-variance (a photo, not a bar)
        pix.set_rect(fitz.IRect(0, y, w, y + 1), (v, v, v))
    page.insert_image(fitz.Rect(70, 388, 230, 404), pixmap=pix)
    doc.save(path)
    doc.close()


def _garbage(path: str) -> None:
    """Microfilm-style garbage OCR: enough chars to pass the length gate, but non-word noise (digits,
    punctuation, frame-edge tokens) so the quality gate rejects it -> vision (quality_fail)."""
    doc = fitz.open()
    page = doc.new_page()
    junk = ("SM-138 0548 :: 8401 9924 // 3381-2 NORMALT 15 MARS 0048 .. 7791 || 1120 ;; "
            "9983 ++ 0571 == 4492 7Q9 0Z1 q7 w8 SM138 0548991")
    page.insert_text((40, 60), junk, fontsize=9)
    doc.save(path)
    doc.close()


def _figure_page(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 72, 520, 160),
                        "Figure 1 below shows the apparatus used during the recorded observation.",
                        fontsize=11)
    # a mid-size embedded raster image with no text over it -> a real figure
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 200, 200))
    pix.set_rect(pix.irect, (180, 60, 60))
    page.insert_image(fitz.Rect(180, 300, 420, 520), pixmap=pix)
    doc.save(path)
    doc.close()


BATES_BLANK = "EFTA00009999"


def _blank_slip(path: str) -> None:
    """A slip sheet / separator: essentially white, only a Bates footer stamp -> blank (skip vision)."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((250, 800), BATES_BLANK, fontsize=8)  # just the control number, near the footer
    doc.save(path)
    doc.close()


def _black_blank(path: str) -> None:
    """A solid-black scanner blank / separator: near-100% dark, no content -> blank (skip vision)."""
    doc = fitz.open()
    page = doc.new_page()
    rect = page.rect
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, int(rect.width), int(rect.height)))
    pix.set_rect(pix.irect, (0, 0, 0))
    page.insert_image(rect, pixmap=pix)
    doc.save(path)
    doc.close()


def _scanned_content(path: str) -> None:
    """A genuine full-page scan: white paper with dark "text" bands (non-uniform marks) + only a Bates
    stamp of extractable text -> low_text (needs vision). Must NOT be mistaken for blank at either extreme."""
    doc = fitz.open()
    page = doc.new_page()
    rect = page.rect
    iw, ih = 300, 390
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, iw, ih))
    pix.set_rect(pix.irect, (255, 255, 255))           # white paper
    for y in range(0, ih, 6):                          # fine dot pattern: high ink, but NO solid rectangle
        for x in range(0, iw, 6):                      # (so it is neither a blank nor a redaction bar)
            pix.set_rect(fitz.IRect(x, y, x + 2, y + 2), (0, 0, 0))
    page.insert_image(rect, pixmap=pix)
    page.insert_text((250, 800), "EFTA00010001", fontsize=8)
    doc.save(path)
    doc.close()


def _encrypted(path: str) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), "secret protected content here for the encrypted fixture", fontsize=12)
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256,
             owner_pw="owner-secret", user_pw="user-secret")
    doc.close()


def _corrupt(path: str) -> None:
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 this is not a real pdf body \x00\x01\x02 garbage")


def build_all(target: str) -> dict:
    os.makedirs(target, exist_ok=True)
    sub = os.path.join(target, "VOL00012", "IMAGES")
    os.makedirs(sub, exist_ok=True)
    paths = {
        "born_digital": os.path.join(target, "text_born_digital.pdf"),
        "ocr_layer": os.path.join(sub, "ocr_layer.pdf"),
        "redacted_leak": os.path.join(sub, "redacted_leak.pdf"),
        "partial_redaction": os.path.join(sub, "partial_redaction.pdf"),
        "raster_redaction": os.path.join(sub, "raster_redaction.pdf"),
        "dark_photo": os.path.join(sub, "dark_photo.pdf"),
        "garbage": os.path.join(target, "garbage_microfilm.pdf"),
        "figure": os.path.join(target, "figure_page.pdf"),
        "blank_slip": os.path.join(sub, "blank_slip.pdf"),
        "black_blank": os.path.join(sub, "black_blank.pdf"),
        "scanned_content": os.path.join(sub, "scanned_content.pdf"),
        "encrypted": os.path.join(target, "encrypted.pdf"),
        "corrupt": os.path.join(target, "corrupt.pdf"),
    }
    _born_digital(paths["born_digital"])
    _ocr_layer(paths["ocr_layer"])
    _redacted_leak(paths["redacted_leak"])
    _partial_redaction(paths["partial_redaction"])
    _raster_redaction(paths["raster_redaction"])
    _dark_photo(paths["dark_photo"])
    _garbage(paths["garbage"])
    _figure_page(paths["figure"])
    _blank_slip(paths["blank_slip"])
    _black_blank(paths["black_blank"])
    _scanned_content(paths["scanned_content"])
    _encrypted(paths["encrypted"])
    _corrupt(paths["corrupt"])
    return paths


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/tfe_fixtures"
    built = build_all(out)
    for k, v in built.items():
        print(f"{k:14s} {v}")
