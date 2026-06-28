"""Read float32 vectors from a Stage-7 embed store by ``content_sha`` (Handoff §5 producer).

The store is 256 append-log shards ``<xx>.f32v`` of fixed records ``digest(32B) + float32[dims]`` (little-
endian), keyed by the first 2 hex chars of the sha (see ``src/embed/store.py``). For ingest we need the
vectors for ONE document's children (a few hundred shas), so we read only the shards those shas fall in —
never the whole 7–21 GB store. Self-contained reader (doesn't import the embed package) so ingest stays
dependency-light. Last occurrence of a sha wins (a ``--force`` re-embed appends).
"""

from __future__ import annotations

import os
import struct

_SHA_BYTES = 32
_FLOAT_BYTES = 4


class VectorStore:
    def __init__(self, store_dir: str, dimensions: int) -> None:
        self.store_dir = os.path.abspath(store_dir)
        self.dimensions = int(dimensions)
        self.record_bytes = _SHA_BYTES + _FLOAT_BYTES * self.dimensions
        self._fmt = f"<{self.dimensions}f"

    def exists(self) -> bool:
        return os.path.isfile(os.path.join(self.store_dir, "embed-store-manifest.json"))

    def get_many(self, content_shas) -> dict[str, list[float]]:
        """Return ``{content_sha_hex: vector}`` for the requested shas that are present in the store. Reads
        only the shards (by 2-hex prefix) that the requested shas fall in."""
        wanted: dict[bytes, str] = {}
        by_shard: dict[str, set[bytes]] = {}
        for sha in content_shas:
            try:
                d = bytes.fromhex(sha)
            except (TypeError, ValueError):
                continue
            if len(d) != _SHA_BYTES:
                continue
            wanted[d] = sha
            by_shard.setdefault(sha[:2], set()).add(d)

        out: dict[str, list[float]] = {}
        rec = self.record_bytes
        for shard, want in by_shard.items():
            path = os.path.join(self.store_dir, shard + ".f32v")
            if not os.path.exists(path):
                continue
            remaining = set(want)
            whole = os.path.getsize(path) - (os.path.getsize(path) % rec)
            with open(path, "rb") as f:
                off = 0
                while off < whole and remaining:
                    blob = f.read(rec)
                    d = blob[:_SHA_BYTES]
                    if d in want:                       # last occurrence wins -> don't drop from remaining
                        out[wanted[d]] = list(struct.unpack(self._fmt, blob[_SHA_BYTES:]))
                    off += rec
        return out
