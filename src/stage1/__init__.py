"""Stage 1 — deterministic, offline PDF text pre-extraction.

Walks a folder of PDFs and writes a sidecar ``<name>.json`` next to each ``<name>.pdf`` that
recommends, per page, whether the page can be ingested from its embedded text layer (cheap) or
must still go to vision/OCR (redacted, garbage, or image-dominant pages).

100% local: reads PDFs, writes JSON. No network, no Azure, no database.

Run:  python -m src.stage1 <ROOT> [flags]
Spec: docs/HANDOFF-text-first-pdf-extraction.md
"""

SCHEMA_VERSION = "1.0"
TOOL_NAME = "text-first-extract"
TOOL_VERSION = "1.2.0"

__all__ = ["SCHEMA_VERSION", "TOOL_NAME", "TOOL_VERSION"]
