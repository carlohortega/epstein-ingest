"""Stage-6 tunable knobs (Handoff §7). Echoed verbatim into each sidecar's ``stage6.config`` so a JSON is
self-describing and a run is reproducible from its own output (mirrors Stage 1–5's ``*.config`` convention).

Stage 6 is **chunk-only**: deterministic, local, no API. So unlike Stage 3/4/5 there is NO model /
temperature / rate-limit / cost / Azure-connection config here — chunking touches no network. The only
real knob is throughput (``concurrency``). The sv-kb chunker's token budgets are NOT mirrored here on
purpose: they are read live from ``svkb_pipeline.chunking`` and recorded in the ``stage6`` block, so they
can never silently drift from the reused chunker (forking the budgets is explicitly forbidden — Handoff §1).
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Stage6Config:
    # Bounded in-flight per-document tasks. The work is read-sidecar + (tiny CPU) chunk + write-chunks.json,
    # i.e. I/O-bound at these doc sizes, so a thread pool mirrors Stage 5; raise for fast NVMe.
    concurrency: int = 8

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``stage6.config`` (stable key order => deterministic output)."""
        return asdict(self)
