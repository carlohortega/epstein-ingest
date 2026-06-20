"""Read-only reader for Stage 7's staged vector store (so the summary can report S7 embed coverage).

Mirrors the documented format of ``src/embed/store.py`` (256 ``<xx>.f32v`` append-log shards of
``digest(32B) + float32[dims]`` records, + a self-describing ``embed-store-manifest.json``) WITHOUT importing
``src.embed`` — that package's ``__init__`` pulls the Azure embedding client (``requests``), and the summary
stays pure-stdlib and dependency-free (Handoff §1/§5). We only ever read: the 32-byte ``content_sha`` key
prefix of each record (seeking past the vector), exactly like ``EmbedStore.load_seen``.

The store is **corpus-global** (default ``<corpus_root>/.embeddings/`` or ``--store DIR``): one keyspace
shared across datasets, so the seen-set is loaded once per scan and reused for every dataset's S7 coverage."""

from __future__ import annotations

import json
import os

_STORE_MANIFEST = "embed-store-manifest.json"
_SHARD_EXT = ".f32v"
_SHA_BYTES = 32
_FLOAT_BYTES = 4


def default_store_dir(corpus_root: str) -> str:
    return os.path.join(os.path.abspath(corpus_root), ".embeddings")


def read_manifest(store_dir: str) -> dict | None:
    """The store's self-describing manifest (embedding_model/dimensions/api_version/record geometry), or None
    if no store exists at ``store_dir``."""
    mpath = os.path.join(store_dir, _STORE_MANIFEST)
    try:
        with open(mpath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return None


def load_seen(store_dir: str, manifest: dict | None = None) -> set[bytes]:
    """The set of embedded ``content_sha`` digests (raw 32-byte ``bytes``) — the resume/coverage state. Reads
    only the 32-byte key of each fixed-size record (seeks past the vector), so it is cheap relative to the
    store size. Trailing partial bytes from a crash are ignored (whole records only). Empty set if no store."""
    store_dir = os.path.abspath(store_dir)
    manifest = manifest or read_manifest(store_dir)
    if not manifest:
        return set()
    dims = int(manifest.get("dimensions") or 0)
    rec = int(manifest.get("record_bytes") or (_SHA_BYTES + _FLOAT_BYTES * dims))
    if rec <= _SHA_BYTES:
        return set()
    seen: set[bytes] = set()
    try:
        shards = sorted(fn for fn in os.listdir(store_dir) if fn.endswith(_SHARD_EXT))
    except OSError:
        return seen
    for fn in shards:
        path = os.path.join(store_dir, fn)
        try:
            size = os.path.getsize(path)
            whole = size - (size % rec)
            with open(path, "rb") as f:
                off = 0
                while off < whole:
                    seen.add(f.read(_SHA_BYTES))
                    f.seek(rec - _SHA_BYTES, os.SEEK_CUR)
                    off += rec
        except OSError:
            continue   # a shard vanished/locked mid-scan — skip, never crash
    return seen
