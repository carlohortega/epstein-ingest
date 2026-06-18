"""Deterministic ``page_kind`` classification — the heart of Stage 2 (Handoff §4).

First match wins. All inputs come from the Stage-1 sidecar (per-page ``metrics``/``text``/``redaction``
and ``needs_vision_reason``) except ``bar_coverage``, which the caller recomputes from the live page only
for redaction-candidate pages (it is not stored in the sidecar). Thresholds come from Stage-2 config plus
the Stage-1 values echoed in ``generator.config`` — so Stage 2 classifies against exactly what Stage 1
measured.

Vision routing (Handoff §4): **only** ``page_kind == "image"`` pages require vision (Stage 4).
``blank``/``redacted`` never do; ``text``/``mixed`` never do (their text is canonical). This supersedes
Stage-1's ``needs_vision`` for routing: a redacted page WITH a usable text layer is ``text``/``mixed``,
not ``image``, so it is not sent to vision — the corpus-specific cost win.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Thresholds:
    """The thresholds the §4 decision needs, assembled from Stage-2 config + Stage-1's echoed config."""
    min_chars: int                  # stage1: minimum usable text chars
    full_page_threshold: float      # stage1: image coverage >= this => backing scan / full-page image
    figure_min_coverage: float      # stage1: sub-image coverage >= this is a candidate figure
    blank_ink_max: float            # stage1: blank tolerance at the white/black extreme
    fully_redacted_bar_frac: float  # stage2: union bar coverage >= this => fully redacted


@dataclass(frozen=True)
class PageSignals:
    """The per-page signals the §4 decision reads (all from the Stage-1 sidecar)."""
    char_count: int                 # text.char_count (INCLUDES [REDACTED] markers — see visible_text_chars)
    visible_text_chars: int         # char_count with redaction markers + whitespace removed (real readable text)
    max_image_coverage: float
    embedded_image_count: int
    needs_vision_reason: str | None
    ink_coverage: float | None
    dark_coverage: float | None
    redaction_runs: int


def visible_text_chars(content: str, redaction_marker: str) -> int:
    """Count real readable characters: drop every ``[REDACTED]`` marker, then all whitespace.

    Handoff §4 rule 2 wants the "visible non-bar text" (e.g. just a Bates footer) compared to ``min_chars``.
    Stage 1's ``text.char_count`` is ``len(content)`` and INCLUDES the inserted ``[REDACTED]`` markers
    (reconstruct.py), so a fully-redacted page can show a large char_count of pure markers. Stripping the
    markers (and whitespace) recovers what a human could actually read on the page.
    """
    if not content:
        return 0
    stripped = content.replace(redaction_marker, " ")
    return len(re.sub(r"\s+", "", stripped))


def classify_page_kind(sig: PageSignals, th: Thresholds, bar_coverage: float | None) -> tuple[str, str]:
    """Return ``(page_kind, reason)``. ``bar_coverage`` may be ``None`` when the page is not a redaction
    candidate (cheap caller-side gate) — rule 2 then cannot fire, which is correct."""

    # 1. blank — Stage 1 already flagged it, or the blank gate at either extreme (defense in depth;
    #    blank_ink_max is read from Stage 1's config so this matches Stage 1's own gate exactly).
    if (sig.needs_vision_reason == "blank"
            or (sig.ink_coverage is not None and sig.ink_coverage < th.blank_ink_max)
            or (sig.dark_coverage is not None and sig.dark_coverage > 1.0 - th.blank_ink_max)):
        return "blank", "stage1_blank"

    # 2. redacted (fully) — bars cover ~the whole page AND nothing readable remains (just a Bates footer).
    #    Gated on the cheap sidecar signals by the caller (redaction_runs>0 AND little visible text); the
    #    expensive bar-area recompute is only done then.
    if (sig.redaction_runs > 0 and sig.visible_text_chars < th.min_chars
            and bar_coverage is not None and bar_coverage >= th.fully_redacted_bar_frac):
        return "redacted", f"bars_cover_{bar_coverage:.3f}"

    # 3. image — a genuine image page: full-page image AND not text-sufficient. Becomes an image asset.
    if sig.max_image_coverage >= th.full_page_threshold and sig.char_count < th.min_chars:
        return "image", "full_page_image_low_text"

    # 4. mixed — text-sufficient page carrying a mid-size embedded figure (captured as an artifact, no vision).
    if (sig.char_count >= th.min_chars and sig.embedded_image_count >= 1
            and th.figure_min_coverage <= sig.max_image_coverage < th.full_page_threshold):
        return "mixed", "text_with_figure"

    # 5. text — text-sufficient with at most small images (a logo is below figure_min_coverage on a text page).
    if sig.char_count >= th.min_chars:
        return "text", "text_sufficient"

    # 6. residual low-text page that is NOT blank/redacted/image (e.g. a sparse scan or a low-text figure
    #    page below full_page_threshold). The §4 prose only defines text/mixed for text-sufficient pages;
    #    for an unsure low-text page §10 says bias to vision. Route to image (fail-safe) so no content is
    #    silently dropped to a text page that has almost nothing to read.
    return "image", "low_text_failsafe"
