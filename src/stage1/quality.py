"""Text-quality metrics and the quality gate (Spec §8).

Computed on the SCRUBBED text (so quality reflects what would actually be indexed). Defaults are
chosen so DataSet-12 prose passes (~0.58 alpha, 1.0 wordlike, 4.8 mean word len) and PBB microfilm
edge-noise fails (sub-40 chars of garbage). Mirrors ~/mcp-test/epstein_quality.py.
"""

from __future__ import annotations

import re

from .config import Config

_WORD = re.compile(r"[A-Za-z]{2,}")


def text_quality(text: str, cfg: Config) -> dict:
    """Return {alpha_ratio, wordlike_ratio, mean_word_len, pass} for ``text``."""
    char_count = len(text)
    alpha = sum(1 for c in text if c.isalpha())
    tokens = _WORD.findall(text)
    n_tokens = len(tokens)
    wordlike = sum(1 for t in tokens if 2 <= len(t) <= 15)
    wlen_sum = sum(len(t) for t in tokens)

    alpha_ratio = (alpha / char_count) if char_count else 0.0
    wordlike_ratio = (wordlike / n_tokens) if n_tokens else 0.0
    mean_word_len = (wlen_sum / n_tokens) if n_tokens else 0.0

    passed = (
        char_count >= cfg.min_chars
        and alpha_ratio >= cfg.min_alpha_ratio
        and wordlike_ratio >= cfg.min_wordlike_ratio
        and cfg.mean_word_len_min <= mean_word_len <= cfg.mean_word_len_max
    )
    return {
        "alpha_ratio": round(alpha_ratio, 4),
        "wordlike_ratio": round(wordlike_ratio, 4),
        "mean_word_len": round(mean_word_len, 4),
        "pass": bool(passed),
    }
