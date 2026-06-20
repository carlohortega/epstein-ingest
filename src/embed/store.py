"""The staged vector store — corpus-global, ``content_sha``-keyed, sharded, float32 (Handoff §4/§5).

A per-doc store would duplicate the shared vectors and defeat the global dedup, so the store is ONE
corpus-global structure keyed by ``content_sha``:

  * **Layout:** 256 shards by the first 2 hex chars of the ``content_sha`` (``<store>/<xx>.f32v``), under a
    store root (default ``<corpus_root>/.embeddings/`` or ``--store DIR``).
  * **Format:** each shard is an APPEND-ONLY log of fixed-size binary records
    ``digest(32 bytes) + float32[dimensions]`` (little-endian). 1536-d => 6176 bytes/record (~6 KB/vector,
    Handoff §4). The vectors are float32 and need NOT be bit-identical to a live sv-kb embed — Stage 8
    inserts with ``ON CONFLICT DO NOTHING`` (first-writer-per-sha wins), so the value is moot; the
    ``content_sha`` KEY is what matters (Handoff §1/§3). Stage 8 serializes each vector with
    ``indexing._vector_literal`` (``repr(float(x))``) on read-back.
  * **Self-describing:** ``embed-store-manifest.json`` records ``embedding_model``/``dimensions``/
    ``api_version`` + the record geometry, so a model/dims change against an existing store is detected and
    REFUSED (mixing models under one ``content_sha`` keyspace would silently corrupt the cache).

**Crash safety (Handoff §4/§5):** sequential appends mean only the LAST write of a shard can be partial, so
opening/loading a shard truncates any trailing bytes that don't complete a whole record. A crash therefore
loses at most the in-flight batch's trailing partial record (those shas are simply re-embedded on the next
run). This append-log + truncate-on-open recovery is the scale-appropriate alternative to per-vector
tmp+rename: at ~1.25M vectors written from a Windows launcher over a ``\\wsl.localhost`` UNC path, 1.25M
tiny files (or whole-shard rewrites) would be pathologically slow, while 256 append logs are not.

Single-writer: only the scheduler's main thread calls ``put`` (workers only embed), so no lock is needed.
"""

from __future__ import annotations

import os
import struct

from .util import dump_json

_STORE_MANIFEST = "embed-store-manifest.json"
_SHARD_EXT = ".f32v"
_SHA_BYTES = 32                  # sha256 digest
_FLOAT_BYTES = 4                 # float32


class StoreModelMismatch(RuntimeError):
    """The existing store was built for a different model/dimensions/api-version. Refuse rather than mix
    incompatible vectors under one ``content_sha`` keyspace (Handoff §4)."""


class EmbedStore:
    def __init__(self, root: str, *, model: str, dimensions: int, api_version: str,
                 generated_at: str | None = None) -> None:
        self.root = os.path.abspath(root)
        self.model = model
        self.dimensions = int(dimensions)
        self.api_version = api_version
        self.record_bytes = _SHA_BYTES + _FLOAT_BYTES * self.dimensions
        self._fmt = f"<{self.dimensions}f"
        self._handles: dict[str, object] = {}
        os.makedirs(self.root, exist_ok=True)
        self._verify_or_write_manifest(generated_at)

    # -- manifest -----------------------------------------------------------------------------------------
    @property
    def manifest_path(self) -> str:
        return os.path.join(self.root, _STORE_MANIFEST)

    def _verify_or_write_manifest(self, generated_at: str | None) -> None:
        import json
        if os.path.exists(self.manifest_path):
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                m = json.load(f)
            for key, want in (("embedding_model", self.model), ("dimensions", self.dimensions),
                              ("api_version", self.api_version)):
                if m.get(key) != want:
                    raise StoreModelMismatch(
                        f"store {self.root} was built with {key}={m.get(key)!r}, run wants {want!r}; "
                        f"use a fresh --store or matching model/dims/api-version")
            return
        dump_json({
            "embedding_model": self.model,
            "dimensions": self.dimensions,
            "api_version": self.api_version,
            "record_bytes": self.record_bytes,
            "sha_bytes": _SHA_BYTES,
            "float_dtype": "float32",
            "endianness": "little",
            "created_at_utc": generated_at,
        }, self.manifest_path)

    @classmethod
    def read_seen(cls, store_dir: str) -> set[bytes]:
        """Load an existing store's resume seen-set WITHOUT opening it for writing or validating its model
        against any run (used by the dry-run, Handoff §7). Reads the store's own recorded record geometry;
        returns an empty set if no store exists yet."""
        import json
        store_dir = os.path.abspath(store_dir)
        mpath = os.path.join(store_dir, _STORE_MANIFEST)
        if not os.path.exists(mpath):
            return set()
        with open(mpath, "r", encoding="utf-8") as f:
            m = json.load(f)
        reader = cls.__new__(cls)
        reader.root = store_dir
        reader.record_bytes = int(m.get("record_bytes")
                                  or (_SHA_BYTES + _FLOAT_BYTES * int(m["dimensions"])))
        return reader.load_seen()

    # -- read (resume seen-set + Stage-8 export) ----------------------------------------------------------
    def _shard_files(self) -> list[str]:
        return sorted(os.path.join(self.root, fn) for fn in os.listdir(self.root)
                      if fn.endswith(_SHARD_EXT)) if os.path.isdir(self.root) else []

    def load_seen(self) -> set[bytes]:
        """The set of ``content_sha`` digests already embedded (the resume state, Handoff §5). Reads only the
        32-byte key prefix of each record (seeking past the vector), so it is cheap. Trailing partial bytes
        from a crash are ignored."""
        seen: set[bytes] = set()
        rec = self.record_bytes
        for path in self._shard_files():
            size = os.path.getsize(path)
            whole = size - (size % rec)
            with open(path, "rb") as f:
                off = 0
                while off < whole:
                    seen.add(f.read(_SHA_BYTES))
                    f.seek(rec - _SHA_BYTES, os.SEEK_CUR)
                    off += rec
        return seen

    def iter_vectors(self):
        """Yield ``(content_sha_hex, vector)`` for Stage 8, deduped per shard (a sha's prefix is fixed, so
        all its records live in one shard) keeping the LAST occurrence (so a ``--force`` re-embed wins).
        Streams shard-by-shard — never holds the whole store in RAM."""
        rec = self.record_bytes
        for path in self._shard_files():
            size = os.path.getsize(path)
            whole = size - (size % rec)
            latest: dict[bytes, bytes] = {}
            with open(path, "rb") as f:
                off = 0
                while off < whole:
                    blob = f.read(rec)
                    latest[blob[:_SHA_BYTES]] = blob[_SHA_BYTES:]
                    off += rec
            for d, vbytes in latest.items():
                yield d.hex(), list(struct.unpack(self._fmt, vbytes))

    # -- write (single-writer; main thread only) ----------------------------------------------------------
    def _handle(self, shard: str):
        fh = self._handles.get(shard)
        if fh is not None:
            return fh
        path = os.path.join(self.root, shard + _SHARD_EXT)
        if os.path.exists(path):                       # recover: drop a trailing partial record before append
            size = os.path.getsize(path)
            extra = size % self.record_bytes
            if extra:
                with open(path, "r+b") as f:
                    f.truncate(size - extra)
        fh = open(path, "ab")
        self._handles[shard] = fh
        return fh

    def put(self, content_sha: str, vector: list[float]) -> None:
        if len(vector) != self.dimensions:
            raise ValueError(f"vector dim {len(vector)} != store dim {self.dimensions}")
        d = bytes.fromhex(content_sha)
        if len(d) != _SHA_BYTES:
            raise ValueError(f"content_sha is not a 32-byte sha256: {content_sha!r}")
        self._handle(content_sha[:2]).write(d + struct.pack(self._fmt, *vector))

    def flush(self) -> None:
        for fh in self._handles.values():
            fh.flush()
            os.fsync(fh.fileno())

    def close(self) -> None:
        for fh in self._handles.values():
            try:
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                fh.close()
        self._handles.clear()

    def __enter__(self) -> "EmbedStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
