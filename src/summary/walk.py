"""The pruned, stat-harvesting walk and dataset discovery — the performance foundation (Handoff §1/§6).

Why a bespoke walk instead of reusing ``stage6.walk.find_pdfs_pruned``:
  1. Importing ``src.stage6`` drags sv-kb into every worker (it imports the chunker at module load); we are
     pure stdlib (see ``util.py``).
  2. ``find_pdfs_pruned`` returns only path strings; the incremental cache needs ``(mtime, size)`` per
     sidecar, and ``os.scandir`` hands us a ``DirEntry`` whose ``.stat()`` is **already cached on Windows**
     (the values come back with the directory listing — no extra syscall). So we harvest stat for free here
     and an incremental "refresh" never has to ``stat()`` 1.4M files a second time.

The prune is identical to the stages': skip ``images``/``pages``/``artifacts`` and any ``<stem>/`` render
dir that has a sibling ``<stem>.pdf`` — so DS-09's 528,740 per-doc render dirs are never descended into
(acceptance §5). Traversal is deterministic (sorted dirs + files).

``--sample K`` is honest: every PDF is still *enumerated* (cheap — names only), but only the first K per leaf
folder are marked ``selected`` for parsing. The engine counts both, so the report can state an exact
``sampled_pct`` and never silently reads as complete (acceptance §7)."""

from __future__ import annotations

import os
from collections import namedtuple

from .util import sidecar_path

# selected: within the per-folder sample budget (always True in full mode). exists: a sibling sidecar is
# present (False -> PDF with no Stage-1 output yet -> a health metric, not an error). mtime/size: the
# sidecar's, for the incremental cache key (None when no sidecar).
# in_pipeline: the doc lives under an ``IMAGES`` segment — the ONLY place the Stage 1-8 pipeline operates.
# DS-09 (uniquely) also carries non-pipeline sibling buckets (MISSING-NATIVES = "No Images Produced"
# placeholders, EMPTY, NATIVES, CORRUPTED, DATA, DB-EXTRACT) that the stages never process; those are
# yielded with in_pipeline=False so the scanner can exclude them from the rollup (not silently — counted).
SidecarEntry = namedtuple("SidecarEntry", "pdf_path sidecar_path exists mtime size selected in_pipeline")

_PRUNE_NAMES = {"images", "pages", "artifacts"}


def _under_images(dirpath: str) -> bool:
    """True if any path component is ``IMAGES`` (case-insensitive) — i.e. the doc is in the pipeline tree.
    The lowercase ``images`` render subdir is already pruned from traversal, so this matches the dataset's
    uppercase ``VOLxxxxx/IMAGES`` doc dir."""
    return any(p.lower() == "images" for p in dirpath.replace("/", os.sep).split(os.sep))


def iter_sidecars(root: str, sample: int = 0):
    """Yield a :class:`SidecarEntry` per ``*.pdf`` under ``root``, pruning the render trees. ``sample`` > 0
    marks only the first ``sample`` PDFs (sorted) in each leaf folder as ``selected``; the rest are still
    yielded (so totals stay exact) but flagged for skip. Each entry carries ``in_pipeline`` (under IMAGES)."""
    root = os.path.abspath(root)
    stack = [root]
    while stack:
        dirpath = stack.pop()
        try:
            with os.scandir(dirpath) as it:
                entries = list(it)
        except (NotADirectoryError, FileNotFoundError, PermissionError):
            continue  # a dir vanished/locked mid-walk (safe to run during an active stage) — skip it

        files: dict[str, os.DirEntry] = {}
        subdirs: list[str] = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.name)
                else:
                    files[e.name] = e
            except OSError:
                continue

        # Recurse into subdirs that are not render trees (push sorted-desc so the stack pops ascending).
        for name in sorted((d for d in subdirs
                            if d not in _PRUNE_NAMES and (d + ".pdf") not in files), reverse=True):
            stack.append(os.path.join(dirpath, name))

        pdfs = sorted(n for n in files if n.lower().endswith(".pdf"))
        in_pipeline = _under_images(dirpath)
        for i, name in enumerate(pdfs):
            pdf_path = os.path.join(dirpath, name)
            side = sidecar_path(pdf_path)
            sde = files.get(os.path.basename(side))
            mtime = size = None
            if sde is not None:
                try:
                    st = sde.stat()  # cached from the scandir on Windows -> free
                    mtime, size = st.st_mtime, st.st_size
                except OSError:
                    sde = None
            yield SidecarEntry(pdf_path, side, sde is not None, mtime, size,
                               selected=(sample <= 0 or i < sample), in_pipeline=in_pipeline)


def discover_datasets(root: str) -> list[str]:
    """Return the ``DataSet-*`` dataset roots directly under a corpus ``root`` (sorted). If ``root`` itself
    is a ``DataSet-*`` dir (or has no ``DataSet-*`` children), return ``[root]`` so a single dataset root or
    an arbitrary subtree still scans."""
    root = os.path.abspath(root)
    base = os.path.basename(root)
    if base.lower().startswith("dataset-"):
        return [root]
    out = []
    try:
        with os.scandir(root) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False) and e.name.lower().startswith("dataset-"):
                    out.append(os.path.join(root, e.name))
    except OSError:
        pass
    return sorted(out) if out else [root]
