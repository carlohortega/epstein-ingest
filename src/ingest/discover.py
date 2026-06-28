"""Locate each dataset's documents (Handoff §2a). Legitimate PDFs live ONLY under an ``IMAGES`` directory,
but at THREE depths: ``DataSet-NN/IMAGES`` (DS-1..8, no VOL), ``DataSet-NN/VOL*/IMAGES`` (DS-9..12). So we
**find the IMAGES dir per dataset** and walk it recursively — never the dataset root (which holds the
excluded DATA/NATIVES/CORRUPTED/... junk). Self-contained (no stage6/stage1 import → no PyMuPDF dependency),
mirroring ``stage6.walk.find_pdfs_pruned``'s pruning.
"""

from __future__ import annotations

import os
import re

_PRUNE_DIRS = ("images", "pages", "artifacts")    # Stage-2 per-PDF PNG trees (when nested under IMAGES)
_DATASET_RE = re.compile(r"(DataSet-\d+)", re.IGNORECASE)


class IdentifierError(ValueError):
    """Raised when dataset_key can't be derived from the path — refuse rather than register a wrong key."""


def derive_identifiers(pdf_path: str, case_key: str) -> tuple[str, str, str]:
    """(document_name, case_key, dataset_key) for ingest — MUST match what the chunks' content_sha were keyed
    with (verified from the live chunks' context_prefix: ``epstein/DataSet-NN``). ``case_key`` is the constant
    case ('epstein'); ``dataset_key`` is the ``DataSet-NN`` path segment (casing preserved); ``document_name``
    is the PDF stem. NOTE: case_key is NOT derived from ``VOL#####`` (that was the earlier bug)."""
    norm = pdf_path.replace("\\", "/")
    document_name = os.path.splitext(os.path.basename(norm))[0]
    m = _DATASET_RE.search(norm)
    if not m:
        raise IdentifierError(f"no DataSet-NN segment in path: {pdf_path}")
    return document_name, case_key, m.group(1)


def find_images_dirs(dataset_root: str) -> list[str]:
    """Return the ``IMAGES`` directories under a dataset root: ``<root>/IMAGES`` if present (DS-1..8), else
    every ``<root>/VOL*/IMAGES`` (DS-9..12). Returns [] if none (a media-only or empty dataset)."""
    dataset_root = os.path.abspath(dataset_root)
    direct = os.path.join(dataset_root, "IMAGES")
    if os.path.isdir(direct):
        return [direct]
    out: list[str] = []
    try:
        for name in sorted(os.listdir(dataset_root)):
            vol = os.path.join(dataset_root, name)
            img = os.path.join(vol, "IMAGES")
            if name.upper().startswith("VOL") and os.path.isdir(img):
                out.append(img)
    except OSError:
        pass
    return out


def find_pdfs(images_dir: str) -> list[str]:
    """Recursively find ``*.pdf`` under an IMAGES dir, pruning the Stage-2 per-PDF output dirs (so a staged
    corpus doesn't descend into the PNG trees). Handles flat (DS-9) and nested-``0001`` (others) layouts."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(images_dir):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in _PRUNE_DIRS and d != ".embeddings" and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.lower().endswith(".pdf"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def find_pdfs_for_dataset(dataset_root: str) -> list[str]:
    """All legitimate PDF paths for a dataset, across however many IMAGES dirs it has."""
    out: list[str] = []
    for img in find_images_dirs(dataset_root):
        out.extend(find_pdfs(img))
    return out


def sidecar_path(pdf_path: str) -> str:
    return os.path.splitext(pdf_path)[0] + ".json"


def chunks_path(pdf_path: str) -> str:
    return os.path.splitext(pdf_path)[0] + ".chunks.json"


def default_store_dir(dataset_root: str) -> str:
    """The embed store location for a dataset (Handoff §2a): ``DataSet-NN/.embeddings`` for DS-1..8 and
    DS-10/11/12; ``VOL*/IMAGES/.embeddings`` for DS-9 (whose embed ran scoped to IMAGES). We pick whichever
    exists, preferring the dataset root."""
    root = os.path.abspath(dataset_root)
    cand = [os.path.join(root, ".embeddings")]
    for img in find_images_dirs(root):
        cand.append(os.path.join(img, ".embeddings"))
    for c in cand:
        if os.path.isdir(c):
            return c
    return cand[0]
