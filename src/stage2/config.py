"""Stage-2 tunable thresholds (Handoff §4/§5/§6) in one place.

Every field is echoed verbatim into each sidecar's ``stage2.config`` so a JSON is self-describing and
a run is reproducible from its own output (mirrors Stage 1's ``generator.config`` convention).

Note: the ``page_kind`` decision (§4) ALSO depends on a few Stage-1 thresholds (``min_chars``,
``full_page_threshold``, ``figure_min_coverage``, ``blank_ink_max``, ``redaction_marker``). Those are
NOT duplicated here — Stage 2 reads them back from the sidecar's ``generator.config`` so it classifies
against exactly the values Stage 1 used. The relied-upon subset is echoed into ``stage2.config`` under
``derived_from_stage1`` for transparency.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Stage2Config:
    # --- rendering (Handoff §5) ---
    render_dpi: int = 200                 # DPI for page/asset display renders (legible for display + vision)
    max_render_pixels: int = 40_000_000   # DPI-clamp ceiling: pages above this px count render at a lower
    #                                       effective DPI so large-format pages don't OOM (we have no
    #                                       Stage-1 clamp_render_dpi to reuse, so the clamp lives here).
    min_render_dpi: int = 36              # never clamp below this (keep a clamped huge page still legible)

    # --- thumbnail (Handoff §5) ---
    thumbnail_max_px: int = 512           # longest side of the per-PDF thumbnail
    thumbnail_page: int = 1               # 1-based representative page rendered as the thumbnail (default: first)

    # --- redacted classification (Handoff §4 rule 2) ---
    fully_redacted_bar_frac: float = 0.60  # union bar-area coverage >= this (+ no readable text) => redacted

    # --- embedded photo artifacts (Handoff §6) ---
    artifact_min_px: int = 64             # skip embedded images smaller than this on either side (icons/logos
    #                                       too small to carry content) — recorded as artifacts_skipped

    # --- staging policy (Handoff §3 / §10 open question) ---
    stage_pages: bool = True              # render non-image pages to <name>/pages/ (a pure upload for Stage 7).
    #                                       Set False to DEFER those renders to Stage 7 (render on ingest) when
    #                                       staging ~1M page PNGs is prohibitive; image-page renders, artifacts,
    #                                       and the thumbnail are ALWAYS staged regardless.

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``stage2.config`` (stable key order => deterministic output)."""
        return asdict(self)
