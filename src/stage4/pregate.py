"""Tier-1 LOCAL pre-gate (Handoff §2) — the free blank filter that runs before any paid vision call.

The corpus's dominant case is a NON-picture (blank scan / broken-image placeholder / slip-sheet), and the
slam-dunk blanks are detectable from pixels for free. For `image` pages Stage 1 already computed
``dark_coverage`` (the fraction of near-black pixels) — a confident WHITE blank has ~zero dark pixels.
``dark_coverage`` is null on `text`/escalated pages, so for escalated ``low_quality_text`` pages and
artifacts we measure the rendered PNG's tonal stats here (the PNG already exists in ``images/`` /
``artifacts/``).

The pre-gate is deliberately CONSERVATIVE: it only fires on ultra-confident blanks (and essentially
solid-black pages), so vision still adjudicates anything ambiguous. A pre-gated asset gets
``has_visual_content=false`` + a ``content_class`` + a ``prefilter_hint`` recording which rule fired, and
its vision call is skipped — removing ~30% of image-page volume at ~zero risk (a provably-blank page
cannot be a real image). Content Safety still runs regardless of the gate (a "blank" is a heuristic, not
proof).
"""

from __future__ import annotations

import os

from .config import Stage4Config

# Returned verdict_source markers so a sidecar records HOW each asset was decided.
SOURCE_PREGATE = "pregate"
SOURCE_VISION = "vision"


def classify_dark(dark: float | None, cfg: Stage4Config) -> tuple[str | None, str | None]:
    """Map a near-black pixel fraction to a (content_class, prefilter_hint) when it is a confident
    blank/black, else (None, None) meaning 'send to vision'."""
    if dark is None:
        return (None, None)
    if dark < cfg.pregate_blank_dark_max:
        return ("blank", "likely_blank")
    if dark > cfg.pregate_black_dark_min:
        return ("black_or_redacted", "likely_black")
    return (None, None)


def measure_png_dark(path: str, dark_level: int) -> float | None:
    """Fraction of pixels at or below ``dark_level`` (0..255 grayscale) in the PNG, via a downscaled
    luminance histogram. Returns None if the image can't be read (no imaging lib / missing/corrupt file)
    — the caller then sends the asset to vision rather than guessing."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            # Downscale: the dark FRACTION is scale-invariant and a small image is far cheaper to histogram.
            im.thumbnail((256, 256))
            hist = im.histogram()             # 256 bins for mode "L"
        total = sum(hist) or 1
        dark = sum(hist[: max(0, min(255, dark_level)) + 1])
        return dark / total
    except Exception:
        return None


def build_page_dark_lookup(sidecar: dict) -> dict[int, float | None]:
    """page_number -> Stage-1 dark_coverage (None where Stage 1 didn't compute it: text/escalated pages)."""
    out: dict[int, float | None] = {}
    for p in (sidecar.get("pages") or []):
        pno = p.get("page_number")
        if pno is None:
            continue
        out[int(pno)] = (p.get("metrics") or {}).get("dark_coverage")
    return out


def pregate_asset(asset: dict, page_dark: dict[int, float | None], base_dir: str,
                  cfg: Stage4Config) -> dict | None:
    """Decide an asset locally, or return None to mean 'needs the vision call'.

    Order: (1) for `page` assets with a Stage-1 dark_coverage, use the metric (zero I/O); (2) otherwise,
    if PNG measurement is enabled, measure the rendered PNG. A fired rule yields a verdict dict carrying
    has_visual_content=false + content_class + prefilter_hint; anything ambiguous returns None."""
    if not cfg.pregate:
        return None

    dark: float | None = None
    measured = False
    if asset.get("kind") == "page":
        pno = asset.get("page_number")
        if pno is not None:
            dark = page_dark.get(int(pno))

    cls, hint = classify_dark(dark, cfg)
    if cls is None and cfg.pregate_measure_png:
        png = os.path.join(base_dir, str(asset.get("path") or "").replace("/", os.sep))
        m = measure_png_dark(png, cfg.pregate_png_dark_level)
        if m is not None:
            measured = True
            cls, hint = classify_dark(m, cfg)
            dark = m if dark is None else dark

    if cls is None:
        return None

    note = ("blank" if cls == "blank" else "solid-black/redacted")
    return {
        "verdict": {
            "caption": f"[pre-gated {note} page; vision call skipped]",
            "description": f"Local pre-gate classified this asset as {note} from its pixel tonality "
                           f"(near-black fraction={round(dark, 5) if dark is not None else None}); no "
                           f"meaningful visual content. The vision call was skipped.",
            "content_class": cls,
            "has_visual_content": False,
            "vision_confidence": 1.0,
            "safety": {"flag": "ok", "categories": [], "minor_indicator": False, "explicit": False},
        },
        "prefilter_hint": hint,
        "measured_png": measured,
        "dark": dark,
    }
