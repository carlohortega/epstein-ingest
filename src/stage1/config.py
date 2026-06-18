"""All tunable thresholds (Spec §7/§8/§9/§13) in one place.

Every field is echoed verbatim into each sidecar JSON under ``generator.config`` so a JSON is
self-describing and a run is reproducible from its own output.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Config:
    # --- redaction: occluder detection (Spec §7) ---
    min_occluder_area: float = 50.0          # pt^2; ignore slivers below this
    occluder_max_area_frac: float = 0.98     # ignore fills covering ~the whole page (backgrounds)
    dark_lum: float = 0.25                    # fill luminance < this => "dark" occluder (redaction bar)
    white_lum: float = 0.90                   # fill luminance > this => "white" occluder
    occluder_overlap: float = 0.60            # span/word bbox >= this fraction inside an occluder => covered

    # --- redaction: under-dark render probe (Spec §7) ---
    render_dpi: int = 150                     # low-DPI grayscale render for the "under dark" test
    dark_level: int = 90                      # 0..255; rendered pixel < this counts as dark ink/bar
    dark_frac: float = 0.55                   # bbox region with >= this fraction dark pixels => under dark
    scrub_partial_min: float = 0.02           # line darkness in [this, ..) => inspect glyphs for a bar
    redaction_scrub_ratio: float = 0.02       # scrubbed/total chars above this => redaction suspected

    # --- positive redaction detection (a redaction is a SOLID fill bar, not just dark pixels) ---
    # A real black-bar redaction is a near-uniform dark rectangle; a dark PHOTO/figure has high pixel
    # variance. Requiring low variance is what tells a bar from imagery (verified against real pages).
    # A real redaction bar's INTERIOR is pure black (mean ~0); a dark photo region's interior is gray
    # (mean >= ~13, measured on real pages). Sampling the inset interior + a near-black mean cleanly
    # separates them. (Edges/white margins of the detected rect are excluded by the inset.)
    bar_interior_inset: float = 0.25          # shrink each side this fraction before measuring solidity
    redaction_solid_mean_max: float = 10.0    # bar interior MEAN below this (0..255) => pure-black fill
    redaction_marker: str = "[REDACTED]"      # inserted in text.content at each detected redaction bar

    # Bar detection: scan the render for solid dark RECTANGLES (the bars themselves), so a redaction is
    # caught and positionally marked even when no OCR text sits under it (verified on real pages).
    bar_scan_step: int = 4                    # pixel subsample step for the row scan (speed vs. precision)
    bar_min_width_pt: float = 12.0            # ignore dark runs narrower than this (not a redaction bar)
    bar_min_height_pt: float = 5.0            # ignore thin horizontal rules
    bar_max_height_pt: float = 60.0           # ignore tall dark columns (photos/figures, not bars)
    bar_max_area_frac: float = 0.35           # ignore huge dark regions (photo/page, not a bar)
    bar_solid_dark_frac: float = 0.85         # rect interior must be >= this fraction near-black (solid)
    bar_overlap: float = 0.50                 # glyph bbox >= this fraction inside a bar => under the bar

    # --- text-layer-type detection (Spec §6) ---
    layer_invisible_ocr: float = 0.50         # invisible_ratio >= this (+ noisy sizes) => ocr_layer
    layer_invisible_born: float = 0.10        # invisible_ratio < this (+ few sizes) => born_digital
    size_noise_threshold: int = 8             # distinct font sizes >= this => typographic noise (OCR)
    born_max_sizes: int = 4                   # distinct font sizes <= this (+ low invisible) => born_digital
    column_gap_frac: float = 0.05             # min mid-page gutter (fraction of width) to split columns

    # --- quality gate (Spec §8) ---
    min_chars: int = 40                       # minimum scrubbed chars to be usable text
    min_alpha_ratio: float = 0.45             # alphabetic / total chars
    min_wordlike_ratio: float = 0.60          # fraction of [A-Za-z]{2,15} tokens
    mean_word_len_min: float = 2.5
    mean_word_len_max: float = 9.0

    # --- routing / figures (Spec §9, §13) ---
    full_page_threshold: float = 0.80         # image coverage >= this => treated as backing scan
    figure_min_coverage: float = 0.05         # sub-image coverage >= this is a candidate figure
    figure_min_words_over: int = 5            # < this many words over a candidate image => a real figure

    # --- blank / slip-sheet detection (Spec §9a) ---
    # For low-text pages only, render once at low DPI and measure coverage at BOTH extremes:
    #   white-blank: ink_coverage < blank_ink_max          (white page + maybe a Bates stamp)
    #   black-blank: dark_coverage > 1 - blank_ink_max     (solid black scan / scanner blank)
    # Either => slip sheet / separator / blank scan -> skip vision. CONSERVATIVE by default (silent
    # content loss is worse than a wasted vision call): any real marks break the uniformity at that
    # extreme and route back to vision. blank_ink_max is the tolerance at either end. Set <= 0 to disable.
    blank_ink_max: float = 0.0008             # coverage tolerance from the white/black extreme
    ink_white_level: int = 245                # grayscale pixel >= this counts as white (not ink)
    blank_render_dpi: int = 72                # DPI for the blank-check render (coverage needs no detail)
    # (dark_level below, in the redaction group, defines the "near-black" pixel cutoff reused here.)

    # --- doc-level routing recommendation (Spec §9) ---
    text_primary_frac: float = 0.90           # >= this fraction text-sufficient => "text_primary"
    vision_primary_frac: float = 0.10         # <= this fraction text-sufficient => "vision_primary"

    # --- large-run robustness (batched submission, heartbeat, stall recovery) ---
    heartbeat_seconds: int = 60               # emit a progress line to stdout at this cadence
    stall_timeout_seconds: int = 300          # if NO file completes for this long, a worker is hung:
                                              #   kill+recycle the pool, quarantine the stuck file(s) as
                                              #   timeout errors. Keep generous (a huge PDF can take a while).
    submit_window_extra: int = 4              # in-flight futures = workers + this. Bounds memory so a
                                              #   million-file run never materializes a million futures.

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``generator.config`` (stable key order = deterministic output)."""
        d = asdict(self)
        # asdict preserves field declaration order on CPython 3.7+, which is the order above.
        return d
