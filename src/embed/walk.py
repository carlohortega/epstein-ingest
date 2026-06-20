"""The embed scheduler, the dry-run projection, and the Stage-7 run manifest (Handoff §4/§5/§6/§7).

Stage 7's unit of work is a BATCH of unique child ``content_sha``s (network-bound), so — like Stage 3/4 —
it uses a bounded THREAD pool with a shared RPM/TPM limiter (provider.py). The scheduler pulls unique items
from ``collect.iter_items`` (a stream — only a bounded window of batches is alive at once), submits each
batch of ``batch_size`` inputs to the pool, and on completion WRITES the vectors to the store on the MAIN
thread (so the store needs no lock). A failed batch is recorded and its shas are simply not stored, so a
re-run retries them (the store is the resume state).

The default store is ``<root>/.embeddings/`` (overridable with ``--store``); the run manifest
``embed-run-manifest.json`` is written at the run root.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Iterator

from .util import dump_json
from . import EMBED_TOOL_NAME, EMBED_TOOL_VERSION
from .collect import Item, iter_items
from .config import EmbedConfig
from .provider import EmbedCallError
from .store import EmbedStore

_MANIFEST_NAME = "embed-run-manifest.json"
DEFAULT_STORE_DIRNAME = ".embeddings"
_DEFAULT_PROGRESS_EVERY = 20000


def default_store_root(root: str) -> str:
    return os.path.join(os.path.abspath(root), DEFAULT_STORE_DIRNAME)


def _batched(items: Iterator[Item], n: int) -> Iterator[list[Item]]:
    batch: list[Item] = []
    for it in items:
        batch.append(it)
        if len(batch) >= n:
            yield batch
            batch = []
    if batch:
        yield batch


def _cost(tokens: int, cfg: EmbedConfig) -> float:
    return (tokens / 1_000_000) * cfg.cost_per_million_tokens


def _maybe_progress(embedded: int, tokens: int, stats: dict, started: float, cfg: EmbedConfig,
                    progress_every: int) -> None:
    """Print a progress line roughly every ``progress_every`` embedded vectors (the count advances in
    batch-sized steps, so we test a batch-wide window)."""
    if not progress_every or embedded % progress_every >= cfg.batch_size:
        return
    el = time.monotonic() - started
    rate = embedded / el if el else 0.0
    print(f"  ... embedded {embedded} unique (seen {stats.get('children_seen', 0)} children, "
          f"cached {stats.get('cached', 0)}) {rate:.0f}/s ~${_cost(tokens, cfg):.2f}", flush=True)


def run(root: str, cfg: EmbedConfig, generated_at: str, *, provider, api_version: str,
        deployment: str | None = None, store_root: str | None = None, force: bool = False,
        workers: int | None = None, progress_every: int = _DEFAULT_PROGRESS_EVERY) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    workers = workers or cfg.concurrency
    store_dir = os.path.abspath(store_root) if store_root else default_store_root(root)
    store_dirname = os.path.basename(store_dir) if os.path.dirname(store_dir) == root else None

    store = EmbedStore(store_dir, model=cfg.model, dimensions=cfg.dimensions,
                       api_version=api_version, generated_at=generated_at)
    seen = set() if force else store.load_seen()

    stats: dict = {}
    embedded = tokens = batches = batch_errors = 0
    errors: list[dict] = []

    # stream -> dedup -> drop cached (resume) -> batch the remaining NEW work
    new_items = (it for it in iter_items(root, store_seen=seen, force=force, stats=stats,
                                         store_dirname=store_dirname) if not it.cached)
    batch_iter = _batched(new_items, cfg.batch_size)

    try:
        if workers <= 1:
            for batch in batch_iter:
                batches += 1
                try:
                    vecs = provider.embed([it.embed_input for it in batch])
                except EmbedCallError as exc:
                    batch_errors += 1
                    errors.append({"code": exc.code, "message": str(exc)[:300], "children": len(batch)})
                    continue
                for it, vec in zip(batch, vecs):
                    store.put(it.content_sha, vec)
                    embedded += 1
                    tokens += it.tokens
                _maybe_progress(embedded, tokens, stats, started, cfg, progress_every)
        else:
            embedded, tokens, batches, batch_errors = _run_scheduler(
                batch_iter, provider, store, workers, errors, progress_state=(stats, started, cfg,
                                                                              progress_every))
        store.flush()
    finally:
        store.close()

    cost = _cost(tokens, cfg)
    manifest = {
        "tool": EMBED_TOOL_NAME,
        "tool_version": EMBED_TOOL_VERSION,
        "embedding_model": cfg.model,
        "dimensions": cfg.dimensions,
        "api_version": store.api_version,
        "deployment": deployment,
        "generated_at_utc": generated_at,
        "root": root,
        "store": store_dir,
        "force": force,
        "config": cfg.echo(),
        "files_seen": stats.get("files_seen", 0),
        "files_errored": stats.get("files_errored", 0),
        "children_seen": stats.get("children_seen", 0),
        "unique_content_sha": stats.get("unique", 0),
        "cached": stats.get("cached", 0),
        "embedded": embedded,
        "batches": batches,
        "batch_errors": batch_errors,
        "tokens": tokens,
        "estimated_cost_usd": round(cost, 6),
        "errors": errors,
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest


def _run_scheduler(batch_iter, provider, store, workers, errors, progress_state):
    """Bounded-window scheduler (mirrors stage3/6): never more than ``window`` batches in flight, so a
    1.25M-child run never buffers all work. The pool only EMBEDS; this (main) thread writes the store."""
    stats, started, cfg, progress_every = progress_state
    window = max(workers * 2, workers + 1)
    embedded = tokens = batches = batch_errors = 0
    inflight: dict = {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        def admit() -> None:
            nonlocal batches
            while len(inflight) < window:
                batch = next(batch_iter, None)
                if batch is None:
                    return
                batches += 1
                inflight[pool.submit(provider.embed, [it.embed_input for it in batch])] = batch

        admit()
        while inflight:
            done, _pending = wait(set(inflight), return_when=FIRST_COMPLETED)
            for fut in done:
                batch = inflight.pop(fut)
                try:
                    vecs = fut.result()
                except EmbedCallError as exc:
                    batch_errors += 1
                    errors.append({"code": exc.code, "message": str(exc)[:300], "children": len(batch)})
                else:
                    for it, vec in zip(batch, vecs):
                        store.put(it.content_sha, vec)
                        embedded += 1
                        tokens += it.tokens
                    _maybe_progress(embedded, tokens, stats, started, cfg, progress_every)
            admit()
    return embedded, tokens, batches, batch_errors


def dry_run(root: str, cfg: EmbedConfig, *, store_root: str | None = None, force: bool = False) -> dict:
    """Project the unique-``content_sha`` count + token/$ with NO API calls and NO writes (Handoff §7).
    Reports the corpus-wide unique count, how many are already embedded (``cached``), the NEW work
    (``to_embed``) and its token/cost projection, plus the pre-dedup ceiling cost."""
    root = os.path.abspath(root)
    store_dir = os.path.abspath(store_root) if store_root else default_store_root(root)
    store_dirname = os.path.basename(store_dir) if os.path.dirname(store_dir) == root else None

    # A dry-run only projects work, so it reads the existing store's resume set WITHOUT validating its
    # model/dims against this run (must never raise StoreModelMismatch). read_seen uses the store's own
    # recorded geometry and returns an empty set if no store exists yet.
    seen = set() if force else EmbedStore.read_seen(store_dir)

    stats: dict = {}
    to_embed = to_embed_tokens = unique_tokens = 0
    for it in iter_items(root, store_seen=seen, force=force, stats=stats, store_dirname=store_dirname):
        unique_tokens += it.tokens
        if not it.cached:
            to_embed += 1
            to_embed_tokens += it.tokens

    return {
        "files_seen": stats.get("files_seen", 0),
        "files_errored": stats.get("files_errored", 0),
        "children_seen": stats.get("children_seen", 0),
        "unique_content_sha": stats.get("unique", 0),
        "cached": stats.get("cached", 0),
        "to_embed": to_embed,
        "to_embed_tokens": to_embed_tokens,
        "estimated_cost_usd": round(_cost(to_embed_tokens, cfg), 6),
        "ceiling_unique_tokens": unique_tokens,
        "ceiling_cost_usd": round(_cost(unique_tokens, cfg), 6),
    }
