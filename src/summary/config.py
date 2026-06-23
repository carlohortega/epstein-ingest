"""Summary scanner knobs (Handoff §8). Echoed verbatim into every report's ``scan`` block so a report is
self-describing and reproducible from its own output (mirrors the Stage 1–6 ``*.config`` convention).

There is no model / temperature / rate-limit / Azure config here — the scanner makes zero API calls. The
knobs are about *how the scan runs* (concurrency, cache, sampling, the optional heavy chunk pass)."""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class SummaryConfig:
    # Parallel JSON parse of cache-MISSES (parse is CPU-bound). 0 -> os.cpu_count(). The walk + stat harvest
    # and the fold stay single-threaded in the main process; only sidecar parsing fans out.
    workers: int = 0

    # Incremental cache keyed by (path, mtime, size). Sidecars are append-only + atomically replaced, so any
    # advance bumps BOTH mtime and size -> a hit is always sound and a refresh re-parses only changed files
    # (Handoff §1/§6). Disable to force a full cold re-parse.
    use_cache: bool = True
    rebuild_cache: bool = False

    # --sample K: read at most K sidecars per leaf folder for a fast ESTIMATE. 0 = full scan. A sampled
    # report is stamped mode="sample" with sampled_pct and its totals flagged extrapolated (no silent caps).
    sample: int = 0

    # --chunks: also open each <name>.chunks.json for dedup-ratio / Stage-7 cost projection. Off by default
    # — that is another ~1.4M reads corpus-wide (Handoff §6). Also required (with a store) for exact per-doc
    # S7 embed coverage (the child content_shas come from chunks.json).
    chunks: bool = False

    # --store DIR: Stage-7 staged vector store (default <corpus_root>/.embeddings). When present, S7 embed
    # coverage is reported; with --chunks it yields exact per-doc coverage. "" => auto-detect the default.
    store: str = ""

    # The pipeline only operates on docs under an IMAGES/ segment. Non-pipeline sibling buckets (DS-09's
    # MISSING-NATIVES "No Images Produced" placeholders, EMPTY, NATIVES, ...) are excluded from the rollup
    # and counted separately. --include-non-pipeline turns this off (count everything).
    images_only: bool = True

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for the report's ``scan.config`` (stable key order => deterministic)."""
        return asdict(self)
