"""Collect + globally dedup the child chunks to embed (Handoff §2/§3.3/§5).

Walks the run root for every sibling ``<name>.chunks.json`` (PDF Stage 6 output today; media-transcript
``chunks.json`` later — identical handling, §2/§8), loads each via ``parents_from_dicts``, iterates
``parent.children``, and STREAMS unique ``(content_sha, embed_input)`` work items, deduped GLOBALLY across
every source by ``content_sha`` (the cost lever, §3.3). Empty docs (``chunks_ref:null``) have no
``chunks.json`` file and contribute nothing.

Scale (Handoff §5): ~1.25M children for DS-11. We STREAM the inputs (the heavy part — never buffer all the
texts in RAM); only a bounded window of batches is alive at once (walk.py). The dedup key set is the one
bounded structure we hold: ``content_sha`` digests stored as raw 32-byte ``bytes`` (~80 MB at DS-11 scale),
seeded from the on-disk store for resume so the store doubles as the seen-set. A far larger corpus would
want a disk-backed sharded dedup; at DS-11 the in-RAM digest set is the simpler, honest choice.

Read-only: opens ``chunks.json`` files, never writes or mutates a sidecar/artifact.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Iterator, NamedTuple

# ``find_pdfs_pruned`` is the template for pruning the Stage-2 PNG trees; we walk for ``*.chunks.json``
# directly (source-agnostic — a media chunks.json need not sit beside a *.pdf) using the same pruning.
_PRUNE_DIRS = ("images", "pages", "artifacts")


def _import_parents_from_dicts():
    """Import sv-kb's ``parents_from_dicts`` (the loader the live indexer uses). The launcher (run_embed.py)
    and tests put ``~/repos/sv-kb/pipeline/shared`` on sys.path; for ``python -m src.embed`` we add it here
    too (WSL path, the Windows UNC path, or ``$SVKB_SHARED``) so the import resolves either way. Mirrors
    ``src/stage6/chunk._import_svkb``."""
    try:
        from svkb_pipeline.chunking import parents_from_dicts
        return parents_from_dicts
    except ModuleNotFoundError:
        pass
    for cand in (
        os.environ.get("SVKB_SHARED"),
        os.path.expanduser("~/repos/sv-kb/pipeline/shared"),
        r"\\wsl.localhost\Ubuntu-22.04\home\cortega\repos\sv-kb\pipeline\shared",
    ):
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    from svkb_pipeline.chunking import parents_from_dicts
    return parents_from_dicts


class Item(NamedTuple):
    content_sha: str        # 64-hex sha256 (the dedup/cache key + store key)
    embed_input: str        # f"{context_prefix} {text}" — the EXACT string sv-kb's indexer embeds (§3.1)
    tokens: int             # ~chars/4 estimate, for cost projection
    cached: bool            # True => already in the store (resume skip); False => new work to embed


def find_chunks_files(root: str, store_dirname: str | None = None) -> list[str]:
    """Recursively find ``*.chunks.json`` but prune the Stage-2 per-PDF output dirs (``images``/``pages``/
    ``artifacts`` and the ``<stem>/`` dirs) and the embeddings store dir, so the walk over a fully-staged
    corpus does not descend into the PNG trees or the vector store (mirrors stage6.walk.find_pdfs_pruned)."""
    out: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        file_set = set(files)
        dirs[:] = [d for d in dirs
                   if d not in _PRUNE_DIRS and d != store_dirname and (d + ".pdf") not in file_set]
        for fn in files:
            if fn.endswith(".chunks.json"):
                out.append(os.path.join(dirpath, fn))
    return sorted(out)


def iter_items(root: str, *, store_seen: set[bytes], force: bool, stats: dict,
               store_dirname: str | None = None) -> Iterator[Item]:
    """Stream unique ``Item``s across the whole corpus. Dedups by ``content_sha`` GLOBALLY (a sha emitted
    once is never emitted again, even across documents/sources); each unique sha is flagged ``cached`` when
    it is already in the store (and ``not force``). Updates ``stats`` in place: ``files_seen``,
    ``files_errored``, ``children_seen``, ``unique`` (distinct shas in this corpus), ``cached`` (distinct
    shas already embedded). The caller filters on ``Item.cached`` (run skips them; dry-run counts them)."""
    parents_from_dicts = _import_parents_from_dicts()
    processed: set[bytes] = set()        # distinct shas seen THIS run (prevents double work across in-flight)

    for cf in find_chunks_files(root, store_dirname):
        stats["files_seen"] = stats.get("files_seen", 0) + 1
        try:
            with open(cf, "r", encoding="utf-8") as f:
                parents = parents_from_dicts(json.load(f))
        except Exception:                # half-written / not-JSON / unexpected shape -> count, never crash
            stats["files_errored"] = stats.get("files_errored", 0) + 1
            continue
        for p in parents:
            for c in p.children:
                stats["children_seen"] = stats.get("children_seen", 0) + 1
                sha = c.content_sha
                try:
                    d = bytes.fromhex(sha)
                except (TypeError, ValueError):
                    # A child whose content_sha isn't 64-hex sha256 (should never happen from sv-kb's
                    # chunker). Count it and skip rather than crash the run.
                    stats["malformed_sha"] = stats.get("malformed_sha", 0) + 1
                    continue
                if d in processed:
                    continue             # duplicate content within the corpus -> already accounted for
                processed.add(d)
                stats["unique"] = stats.get("unique", 0) + 1
                cached = (not force) and d in store_seen
                if cached:
                    stats["cached"] = stats.get("cached", 0) + 1
                # embed input — the fidelity core (Handoff §3.1): context_prefix + ONE space + text.
                embed_input = f"{c.context_prefix} {c.text}"
                yield Item(sha, embed_input, max(1, len(embed_input) // 4), cached)
