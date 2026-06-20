"""Tiny shared helpers for ``src/embed``.

``dump_json`` is the SAME deterministic, atomic JSON writer as ``src/stage1/walk.dump_json`` (fixed indent,
UTF-8, construction key order, tmp+rename), kept local on purpose: Stage 7 is network-only and never touches
a PDF, so it must NOT import ``stage1.walk`` (whose module-level ``import fitz`` would drag PyMuPDF into a
stage that has no use for it). Same bytes, no PyMuPDF dependency.
"""

from __future__ import annotations

import json
import os


def dump_json(obj: dict, path: str) -> None:
    """Deterministic write: fixed indent, UTF-8, keys in construction order, trailing newline; atomic via
    tmp + rename so a crash never leaves a half-written manifest."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)
