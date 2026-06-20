"""The scan engine: pruned walk -> incremental cache check -> process-pool parse of misses -> fold digests.

Shape (Handoff §5/§6): the **main process** walks (``iter_sidecars`` harvested mtime/size for free) and for
each selected sidecar asks the cache by ``(path, mtime, size)``. Cache **hits** fold instantly. Cache
**misses** are submitted to a ``ProcessPoolExecutor`` (JSON parse is CPU-bound) with a **bounded in-flight
window**, so a 528k-doc dataset never buffers all futures. Each worker opens the sidecar ``"r"``, parses,
runs the collector registry, and returns only the compact digest — never the whole sidecar. A
missing/corrupt/half-written sidecar becomes a counted ``missing``/``parse_error`` digest, never an
exception (safe to run while a stage is writing the same tree — acceptance §6).

A worker NEVER writes: read-only is enforced by construction (acceptance §10.1)."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ProcessPoolExecutor, FIRST_COMPLETED, wait

from .cache import DigestCache
from .collectors import extract_digest
from .config import SummaryConfig
from .util import chunks_path
from .walk import iter_sidecars
from . import CACHE_NAME


def _parse_sidecar(sidecar_path: str, want_chunks: bool, want_shas: bool) -> dict:
    """Worker body (module-level so it pickles): parse one sidecar -> digest. Read-only; isolates failures."""
    try:
        with open(sidecar_path, "r", encoding="utf-8") as f:
            sc = json.load(f)
    except (FileNotFoundError, OSError):
        # Vanished/locked between the walk and the open (active stage renamed it) — treat as missing S1.
        return {"status": "missing"}
    except ValueError:
        # Half-written / truncated / not-JSON — count it, never crash the scan.
        return {"status": "parse_error"}
    digest = extract_digest(sc)
    if want_chunks and digest.get("s6") and not digest.get("s6_empty"):
        digest.update(_chunk_metrics(sidecar_path, want_shas))
    return digest


def _chunk_metrics(sidecar_path: str, want_shas: bool) -> dict:
    """Heavy mode (--chunks): open the sibling chunks.json for the dedup/Stage-7-cost projection (Handoff
    §3 heavy mode). content_sha dedup ratio = the embedding-cache win Stage 7 will get. When ``want_shas``
    (store-aware S7), also return the unique child content_shas so the main process can check them against the
    embed store's seen-set — returned (not the derived S7 verdict) so it can be cached and re-checked against
    a changed store on a warm re-scan."""
    cpath = chunks_path(os.path.splitext(sidecar_path)[0] + ".pdf")
    try:
        with open(cpath, "r", encoding="utf-8") as f:
            parents = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {}
    shas, children = set(), 0
    for p in parents or []:
        for c in (p.get("children") or []):
            children += 1
            s = c.get("content_sha")
            if s:
                shas.add(s)
    out = {"chunk_children": children, "chunk_unique_sha": len(shas)}
    if want_shas:
        out["chunk_shas"] = sorted(shas)
    return out


def _worker(task):
    sidecar_path, want_chunks, want_shas = task
    return sidecar_path, _parse_sidecar(sidecar_path, want_chunks, want_shas)


def scan_dataset(dataset_root: str, cfg: SummaryConfig, aggregator, embed_seen: set | None = None) -> dict:
    """Walk + parse one dataset, folding every selected doc's digest into ``aggregator``. Manages the
    dataset's own incremental cache file. ``embed_seen`` (when given) is the staged vector store's set of
    embedded ``content_sha`` digests (raw 32-byte bytes) — enables store-aware per-doc S7 coverage. Returns
    a ``scan`` meta dict for the report's ``scan`` block."""
    dataset_root = os.path.abspath(dataset_root)
    started = time.monotonic()
    cache_path = os.path.join(dataset_root, CACHE_NAME)
    cache = DigestCache.load(cache_path, enabled=cfg.use_cache, rebuild=cfg.rebuild_cache)

    workers = cfg.workers or (os.cpu_count() or 4)
    want_shas = embed_seen is not None and cfg.chunks    # store-aware S7 needs per-doc child shas
    docs_total = docs_scanned = sampled_out = 0
    live_paths: set[str] = set()

    def fold(sidecar_path, mtime, size, digest):
        nonlocal docs_scanned
        docs_scanned += 1
        # Cache the RAW sidecar-derived digest first (may carry chunk_shas; never the store-dependent S7
        # verdict — the store changes independently of the sidecar, so S7 must be recomputed each run).
        if sidecar_path is not None and digest.get("status") != "parse_error":
            cache.put(sidecar_path, mtime, size, digest)
        if embed_seen is not None and digest.get("chunk_shas") is not None:
            emb = 0
            for h in digest["chunk_shas"]:
                try:
                    if bytes.fromhex(h) in embed_seen:
                        emb += 1
                except ValueError:
                    pass
            n = len(digest["chunk_shas"])
            digest = {**digest, "s7_child_total": n, "s7_child_embedded": emb,
                      "s7_done": n > 0 and emb == n}
        aggregator.add(dataset_root, digest)

    def _usable(cached: dict) -> bool:
        # A cached digest from an earlier, lighter scan may lack chunk metrics this scan needs. For a doc with
        # children: --chunks needs chunk_children/chunk_unique_sha; store-aware S7 also needs chunk_shas.
        # Re-parse rather than silently report zero dedup / zero S7 coverage.
        if not (cfg.chunks or want_shas):
            return True
        if cached.get("s6") and not cached.get("s6_empty") and cached.get("s6_children", 0) > 0:
            if cfg.chunks and "chunk_children" not in cached:
                return False
            if want_shas and "chunk_shas" not in cached:
                return False
        return True

    # Stream selected misses through the pool; cache hits + missing-sidecar docs fold inline.
    def iter_misses():
        nonlocal docs_total, sampled_out
        for e in iter_sidecars(dataset_root, sample=cfg.sample):
            docs_total += 1
            if not e.selected:
                sampled_out += 1
                continue
            if not e.exists:
                fold(None, None, None, {"status": "missing"})   # PDF with no Stage-1 sidecar yet
                continue
            live_paths.add(e.sidecar_path)
            cached = cache.get(e.sidecar_path, e.mtime, e.size)
            if cached is not None and _usable(cached):
                fold(e.sidecar_path, e.mtime, e.size, cached)
            else:
                if cached is not None:      # counted a hit but it's unusable here -> reclassify as a miss
                    cache.hits -= 1
                    cache.misses += 1
                yield e

    if workers <= 1:
        for e in iter_misses():
            _, digest = _worker((e.sidecar_path, cfg.chunks, want_shas))
            fold(e.sidecar_path, e.mtime, e.size, digest)
    else:
        _run_pool(iter_misses(), cfg.chunks, want_shas, workers, fold)

    if cfg.use_cache:
        cache.prune_to(live_paths)
        cache.save(cache_path)

    pct = round(100.0 * docs_scanned / docs_total, 2) if docs_total else 100.0
    return {
        "mode": "sample" if cfg.sample else "full",
        "sample_k": cfg.sample,
        "docs_total": docs_total,
        "docs_scanned": docs_scanned,
        "docs_sampled_out": sampled_out,
        "sampled_pct": pct,
        "extrapolated": bool(cfg.sample) and docs_scanned < docs_total,
        "cache_hits": cache.hits,
        "cache_misses": cache.misses,
        "wall_clock_s": round(time.monotonic() - started, 3),
    }


def _run_pool(misses, want_chunks, want_shas, workers, fold):
    """Bounded-window scheduler (mirrors stage6.walk._run_scheduler): never more than ``window`` sidecars in
    flight, so memory stays flat over a 1.4M-file corpus."""
    window = max(workers * 2, workers + 1)
    pending = {}   # future -> (sidecar_path, mtime, size)
    src = iter(misses)

    with ProcessPoolExecutor(max_workers=workers) as pool:
        def admit():
            while len(pending) < window:
                e = next(src, None)
                if e is None:
                    return
                fut = pool.submit(_worker, (e.sidecar_path, want_chunks, want_shas))
                pending[fut] = (e.sidecar_path, e.mtime, e.size)

        admit()
        while pending:
            done, _ = wait(set(pending), return_when=FIRST_COMPLETED)
            for fut in done:
                path, mtime, size = pending.pop(fut)
                try:
                    _, digest = fut.result()
                except Exception:   # pragma: no cover - a worker death must not abort the dataset
                    digest = {"status": "parse_error"}
                fold(path, mtime, size, digest)
            admit()
