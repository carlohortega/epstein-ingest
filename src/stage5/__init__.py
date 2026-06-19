"""text-first-extract Stage 5 — finalize the document summary (text + vision fusion, gpt-4.1-nano).

Stage 3 wrote a **provisional** ``document_summary`` from the text lane only (and ``null`` for docs with
no usable text). Stage 4 wrote per-image ``caption`` + ``description`` gated by ``has_visual_content``.
Stage 5 **fuses the two lanes into the final document summary** as cheaply as possible (Handoff §0):

  * **text-only** doc (no usable vision, has text)  -> PROMOTE provisional text to final, **no LLM call**;
  * **fused** doc (vision + text)                    -> ONE nano reduce over page summaries + captions;
  * **image-only** doc (vision, no text)             -> ONE nano reduce over captions/descriptions only;
  * **no-content** doc (neither)                     -> deterministic sentinel ``"No readable content."``.

Append-only / lanes are immutable: Stage 5 writes its OWN ``document_summary_final`` + a ``stage5`` block
and NEVER overwrites Stage 3's provisional ``document_summary`` (Handoff §1/§5). The single source of truth
for "is this image usable" is ``has_visual_content`` (pages AND artifacts), never ``page_kind`` (§2).

Local only: extends the sidecar + writes a run manifest; no Azure Storage, no DB (that is Stage 7).
Idempotent, resumable, fault-isolated. Reuses the Stage-3 nano plumbing (connection / client / ratelimit)
unchanged; the api KEY is env-only and is never written to a sidecar or manifest.

Spec: docs/HANDOFF-stage5-finalize-summary.md
"""

STAGE5_TOOL_NAME = "text-first-extract-stage5"
STAGE5_TOOL_VERSION = "1.0.0"

# Bumped whenever the fusion system prompt / response schema changes. Stamped into every finalized
# summary's provenance so a sidecar is self-describing (outputs are LLM-generated => provenance over
# byte-determinism, mirroring Stage 3/4).
PROMPT_VERSION = "stage5-2026-06-19.1"

# Positive assertion written into document_summary_final.text when a doc has nothing to summarize
# (Handoff §4). Keeps every finalized doc's summary field non-null (no special-casing downstream) and is
# honest/auditable — distinct from a `null` that conflates "not run" with "ran, empty".
NO_CONTENT_SENTINEL = "No readable content."

__all__ = ["STAGE5_TOOL_NAME", "STAGE5_TOOL_VERSION", "PROMPT_VERSION", "NO_CONTENT_SENTINEL"]
