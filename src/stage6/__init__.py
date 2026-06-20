"""text-first-extract Stage 6 — chunk the document body for retrieval (chunk-only; embedding is Stage 7).

Stages 1–5 produced, per document: verbatim page text (``pages[].text.content``), image captions
(``image_assets[]``), and a finalized ``document_summary_final``. Stage 6 turns the document BODY into
retrieval chunks by reusing sv-kb's production chunker VERBATIM (``svkb_pipeline.chunking.chunk_pages`` +
``parents_to_dicts``) and writing a sibling ``<name>.chunks.json`` — the exact artifact the live
``indexer-job`` consumes — referenced from a new ``stage6`` block. (Spec: docs/HANDOFF-stage6-chunk.md.)

The bar is **chunks indistinguishable from what sv-kb would produce**, which means replicating sv-kb's
``consolidate.py`` input assembly exactly (Handoff §1a), not merely calling the function:

  * each text-layer page's markdown is the EXACT wrapper ``f"## Transcription\n\n{text}"`` — the heading is
    load-bearing (it sets ``heading_path=["", "Transcription"]`` and ``Section: Transcription`` in every
    child's ``context_prefix``); feeding bare text would change every child's heading_path AND content_sha;
  * ``brief_description=""`` and ``embedding_model="text-embedding-3-small"`` — the defaults
    ``consolidate.py`` uses. We do NOT pass the Stage-5 summary as ``brief_description`` (sv-kb doesn't, so
    it would change every ``context_prefix``/``content_sha`` and make 100% of chunks distinguishable);
  * identifiers (``document_name`` / ``case_key`` / ``dataset_key``) feed ``content_sha`` so they must match
    sv-kb's ``IngestionEvent`` convention (derived from the path — see ``chunk.derive_identifiers``).

Chunk-only: Stage 6 makes **zero API calls** (chunking is deterministic + local), needs **no Azure creds**,
and writes no DB/Storage (that is Stage 7). Append-only — it writes its own ``stage6`` block + the sibling
``chunks.json`` and never modifies Stage 1–5 fields. Idempotent, resumable, fault-isolated.

Two fidelity calls were decided at build time (Handoff §1a.4 / §11) and are stamped into every ``stage6``
block so a sidecar is self-describing about its divergence from a hypothetical full-sv-kb run:

  * ``TEXT_SOURCE = "stage1-scrubbed"`` — we feed ``pages[].text.content`` (PyMuPDF text with redaction
    scrubbing -> ``[REDACTED]``), NOT a re-extracted raw ``page.get_text``. Never leaks under-bar content;
    byte-divergent from sv-kb only where a redaction differs.
  * ``IMAGE_PAGE_POLICY = "skip-no-text"`` — pages with no text layer (empty ``text.content``) are NOT
    chunked. sv-kb would feed a full vision-to-markdown transcription for such pages; our cost-optimized
    Stage 4 is caption-only and full-page scans are not captioned at all, so there is nothing faithful to
    feed. We do NOT inject captions into the chunker (sv-kb wouldn't -> distinguishable). A future
    vision-transcription stage can fill the gap; ``--force`` re-chunks.
"""

STAGE6_TOOL_NAME = "text-first-extract-stage6"
STAGE6_TOOL_VERSION = "1.0.0"

# The sv-kb defaults consolidate.py relies on. Passed EXPLICITLY (same values) for clarity; they feed every
# child's content_sha, so changing either makes 100% of our chunks distinguishable + breaks the shared
# embedding cache/dedup (Handoff §1a.2).
EMBEDDING_MODEL = "text-embedding-3-small"
BRIEF_DESCRIPTION = ""

# sv-kb's exact per-text-page wrapper (consolidate.py). The heading is load-bearing (Handoff §1a.1).
TRANSCRIPTION_HEADING = "## Transcription"

# The two decided fidelity calls (Handoff §1a.4 / §11), recorded verbatim in every stage6 block.
TEXT_SOURCE = "stage1-scrubbed"
IMAGE_PAGE_POLICY = "skip-no-text"
FIDELITY_NOTES = (
    "Chunks mirror sv-kb's text-layer path exactly (## Transcription wrapper, brief_description='', "
    "embedding_model='text-embedding-3-small', per-page no-stitch). Two documented divergences: (1) page "
    "text is the Stage-1 redaction-scrubbed PyMuPDF text ([REDACTED]) rather than sv-kb's raw get_text, so "
    "content_sha differs only where a redaction differs; (2) pages with no text layer are skipped (sv-kb "
    "would feed a vision-to-markdown transcription; Stage 4 is caption-only)."
)


def page_md(content: str) -> str:
    """sv-kb's exact text-layer page markdown: ``## Transcription`` heading + the raw page text. Raw (not
    stripped) to byte-match consolidate.py's ``f"## Transcription\n\n{ocr_text or ''}"``."""
    return f"{TRANSCRIPTION_HEADING}\n\n{content}"


__all__ = [
    "STAGE6_TOOL_NAME", "STAGE6_TOOL_VERSION", "EMBEDDING_MODEL", "BRIEF_DESCRIPTION",
    "TRANSCRIPTION_HEADING", "TEXT_SOURCE", "IMAGE_PAGE_POLICY", "FIDELITY_NOTES", "page_md",
]
