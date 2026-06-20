"""Incremental digest cache — the reason a refresh is *seconds* not minutes (Handoff §6).

Sidecars are **append-only and atomically replaced** (each stage adds a block via ``os.replace``), so any
advance bumps BOTH the file's ``mtime`` and ``size``. That makes ``(path, mtime, size)`` a *sound* cache
key: a content change can never collide with a cached entry (a false hit is impossible), and a cached digest
for an unchanged file is valid forever. So an incremental scan is a stat-only sweep (the walk already
harvested mtime/size for free) that re-parses ONLY the handful of changed sidecars.

The cache is versioned by both ``CACHE_VERSION`` and ``SCHEMA_VERSION``: bump either (e.g. a new collector
field) and the whole cache is discarded, so a digest computed under an old shape can never be served
(acceptance §9). It lives at ``<dataset_root>/.summary-cache.json`` (gitignored; outside the repo anyway)."""

from __future__ import annotations

import json

from . import CACHE_VERSION, SCHEMA_VERSION
from .util import dump_json


class DigestCache:
    """A path -> (mtime, size, digest) map persisted as one JSON file per dataset."""

    def __init__(self) -> None:
        self._entries: dict[str, dict] = {}
        self.hits = 0
        self.misses = 0

    @classmethod
    def load(cls, path: str, *, enabled: bool = True, rebuild: bool = False) -> "DigestCache":
        """Load the cache from ``path``. Returns an empty cache if disabled, rebuilding, missing, corrupt, or
        stamped with a different cache/schema version (never raises — a bad cache just means a cold scan)."""
        c = cls()
        if not enabled or rebuild:
            return c
        try:
            with open(path, "r", encoding="utf-8") as f:
                blob = json.load(f)
        except (FileNotFoundError, ValueError, OSError):
            return c
        if (blob.get("summary_cache_version") != CACHE_VERSION
                or blob.get("schema_version") != SCHEMA_VERSION):
            return c  # shape changed -> drop everything, recompute clean
        ents = blob.get("entries")
        if isinstance(ents, dict):
            c._entries = ents
        return c

    def get(self, path: str, mtime, size) -> dict | None:
        """Return the cached digest for ``path`` iff (mtime, size) match exactly; else None. Counts hits/
        misses for the report's ``scan`` block."""
        e = self._entries.get(path)
        if e is not None and e.get("mtime") == mtime and e.get("size") == size:
            self.hits += 1
            return e.get("digest")
        self.misses += 1
        return None

    def put(self, path: str, mtime, size, digest: dict) -> None:
        self._entries[path] = {"mtime": mtime, "size": size, "digest": digest}

    def prune_to(self, live_paths: set[str]) -> int:
        """Drop entries for sidecars that no longer exist (deleted/renamed) so the cache file doesn't grow
        unbounded over a corpus's lifetime. Returns the number dropped."""
        dead = [p for p in self._entries if p not in live_paths]
        for p in dead:
            del self._entries[p]
        return len(dead)

    def save(self, path: str) -> None:
        dump_json({
            "summary_cache_version": CACHE_VERSION,
            "schema_version": SCHEMA_VERSION,
            "entries": self._entries,
        }, path)
