"""Deterministic generator for the shared placeholder PAGE assets.

TWO canonical placeholders, used by BOTH epstein-ingest (Stage 8 §4a) and sv-kb to keep a PDF document intact
when an individual page can't be published as-is:

  content-redacted.{png,pdf}  — replaces a FLAGGED (high-risk) page/image  (alarming dark-red title)
  page-blank.{png,pdf}        — replaces a BLANK / effectively-blank page   (calm gray title)

(Standalone media assets — image/video/audio — that are quarantined are NOT given a placeholder; they are
simply not uploaded. See HANDOFF-stage8-ingest.md §4a.)

Identical bytes in both repos, so every substitution dedups to one `content_hash` per variant; neither is
embedded. The assets are committed (runtime never depends on a font); this script just (re)produces them.

Run with a Python that has Pillow (the anaconda env used for Stage 1/2 rendering):
  C:\\Users\\carlo\\anaconda3\\python.exe assets\\redaction\\generate_redaction_asset.py
Prints sha256 of each output.
"""

from __future__ import annotations

import hashlib
import os

from PIL import Image, ImageDraw, ImageFont

DPI = 200
W, H = int(8.5 * DPI), int(11 * DPI)             # 1700 x 2200 (US Letter @ 200 DPI)
BG = (255, 255, 255)
INK = (40, 40, 40)
HERE = os.path.dirname(os.path.abspath(__file__))

VARIANTS = {
    "content-redacted": {
        "accent": (140, 20, 20),                 # dark red — high-risk redaction
        "title": "CONTENT REDACTED",
        "lines": ["This content was flagged and has been withheld", "from the online corpus."],
        "footer": "Original retained in secure staging — not published or shared.",
    },
    "page-blank": {
        "accent": (120, 120, 120),               # calm gray — benign empty page
        "title": "BLANK PAGE",
        "lines": ["The original page was blank or contained", "no readable content."],
        "footer": "Placeholder rendered in place of an empty source page.",
    },
}


def _font(names: list[str], size: int) -> ImageFont.FreeTypeFont:
    for n in names:
        for cand in (n, os.path.join(r"C:\Windows\Fonts", n)):
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _fit_font(draw: ImageDraw.ImageDraw, text: str, names: list[str], max_w: int,
              start: int) -> ImageFont.FreeTypeFont:
    """Largest font (from ``start`` down) whose rendered ``text`` width fits ``max_w``."""
    size = start
    while size > 24:
        f = _font(names, size)
        l, _t, r, _b = draw.textbbox((0, 0), text, font=f)
        if (r - l) <= max_w:
            return f
        size -= 4
    return _font(names, 24)


def _centered(draw: ImageDraw.ImageDraw, y: int, text: str, font, fill) -> int:
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.text(((W - (r - l)) / 2 - l, y), text, font=font, fill=fill)
    return y + (b - t)


def build_png(path: str, cfg: dict) -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([40, 40, W - 40, H - 40], outline=INK, width=6)
    d.rectangle([70, 70, W - 70, H - 70], outline=INK, width=2)

    title_f = _fit_font(d, cfg["title"], ["ariblk.ttf", "arialbd.ttf", "arial.ttf"], int(W * 0.78), start=150)
    body_f = _font(["arial.ttf"], 64)
    foot_f = _font(["ariali.ttf", "arial.ttf"], 40)

    y = int(H * 0.34)
    y = _centered(d, y, cfg["title"], title_f, cfg["accent"]) + 70
    d.line([W * 0.26, y, W * 0.74, y], fill=INK, width=3)
    y += 60
    for ln in cfg["lines"]:
        y = _centered(d, y, ln, body_f, INK) + 24
    _centered(d, int(H * 0.82), cfg["footer"], foot_f, (90, 90, 90))

    img.save(path, "PNG", dpi=(DPI, DPI))


def build_pdf(png_path: str, pdf_path: str) -> None:
    Image.open(png_path).convert("RGB").save(pdf_path, "PDF", resolution=float(DPI))


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def main() -> None:
    for name, cfg in VARIANTS.items():
        png = os.path.join(HERE, f"{name}.png")
        pdf = os.path.join(HERE, f"{name}.pdf")
        build_png(png, cfg)
        build_pdf(png, pdf)
        for p in (png, pdf):
            print(f"{os.path.basename(p):24} {os.path.getsize(p):>9} bytes  sha256={_sha(p)}")
    print(f"dimensions: {W}x{H} (US-Letter @ {DPI} DPI)")


if __name__ == "__main__":
    main()
