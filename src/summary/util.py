"""Tiny self-contained helpers, reimplemented here on purpose.

The live stages keep these in ``src/stage1/walk.py`` (``dump_json``/``sidecar_path``) and
``src/stage6/chunk.py`` (``derive_identifiers``), but importing those modules pulls heavy transitive
imports into every process-pool worker: ``src.stage1.walk`` does ``import fitz`` (PyMuPDF) and
``src.stage6.chunk`` runs ``_import_svkb()`` at module load. The summary scanner is pure stdlib over local
JSON (Handoff §1/§5), so we copy the ~25 lines we need rather than inherit those dependencies.

The implementations are byte-compatible with the originals:
  * ``dump_json`` — same atomic write-temp-then-``os.replace`` as ``stage1.walk.dump_json``.
  * ``derive_identifiers`` — same ``DataSet-NN`` / ``VOLxxxxx`` regexes as ``stage6.chunk``.
"""

from __future__ import annotations

import json
import os
import re

_DATASET_RE = re.compile(r"(DataSet-\d+)", re.IGNORECASE)
_CASE_RE = re.compile(r"(VOL\d+)", re.IGNORECASE)


def sidecar_path(pdf_path: str) -> str:
    """``<stem>.json`` beside ``<stem>.pdf`` (same as stage1.walk.sidecar_path)."""
    return os.path.splitext(pdf_path)[0] + ".json"


def chunks_path(pdf_path: str) -> str:
    """``<stem>.chunks.json`` beside ``<stem>.pdf`` (the Stage-6 sibling; read only in --chunks mode)."""
    return os.path.splitext(pdf_path)[0] + ".chunks.json"


def dump_json(obj: dict, path: str) -> None:
    """Deterministic atomic write: fixed indent, UTF-8, construction key order, trailing newline. Mirrors
    ``stage1.walk.dump_json`` — used ONLY for report artifacts/cache, never for a sidecar (acceptance §10.1)."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def derive_identifiers(pdf_path: str, sidecar: dict | None = None) -> tuple[str | None, str | None]:
    """(dataset_key, case_key) for a path, e.g. ("DataSet-12", "VOL00012"). Casing preserved verbatim.
    ``dataset_key`` from the absolute path; ``case_key`` from the sidecar's ``source.relative_path`` when
    available (the live convention) else the absolute path. Returns ``None`` for a key that can't be derived
    (the scanner buckets such docs under an "unknown" group rather than crashing — Handoff §1)."""
    norm = pdf_path.replace("\\", "/")
    rel = ""
    if sidecar:
        rel = ((sidecar.get("source") or {}).get("relative_path") or "").replace("\\", "/")
    ds = _DATASET_RE.search(norm)
    case = _CASE_RE.search(rel) or _CASE_RE.search(norm)
    return (ds.group(1) if ds else None), (case.group(1) if case else None)
