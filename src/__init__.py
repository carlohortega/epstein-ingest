"""epstein-ingest — staged PDF ingestion pipeline.

Each stage is a self-contained subpackage with its own CLI (``python -m src.stageN``):

- :mod:`src.stage1` — deterministic, offline text-first pre-extraction (sidecar JSON).
- :mod:`src.stage2` — render + classify + register pages (page_kind routing).
- :mod:`src.stage3` — nano text summaries + safety triage (the only network stage).

Stages 2 and 3 reuse Stage 1's deterministic ``*.pdf`` walker and atomic JSON writer
(``src.stage1.walk``).

Specs: docs/STRATEGY-pdf-ingestion-stages.md and the per-stage HANDOFF-*.md docs.
"""
