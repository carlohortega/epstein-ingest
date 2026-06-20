"""text-first-extract Stage 7 — embed the staged text child-chunks (functional name ``src/embed``).

After Stage 6 the corpus carries, per document, a sibling ``<name>.chunks.json`` whose **child** chunks each
hold a ``content_sha``, a ``context_prefix`` and a clean ``text``. Stage 7 turns those children into
**text embeddings** and stages them on disk as a **``content_sha``-keyed float32 vector store** — it writes
NOTHING to Postgres/Azure Storage (that is Stage 8). It is the one shared, API-bound step between chunking
and ingestion, decoupled so embedding can run resumably over hours while the DB load stays a fast pass.
(Spec: docs/HANDOFF-stage7-embed.md.)

The fidelity core (Handoff §3), replicated EXACTLY so a ``content_sha``'s vector is what
``svkb_pipeline.indexing.index_document`` would have produced:

  * **embed input = ``f"{context_prefix} {text}"``** (context_prefix + ONE space + text) — NOT ``text``,
    NOT the ``content_sha`` preimage (``f"{model}\\n{prefix}\\n{text}"``). Embedding the wrong string
    silently yields vectors that don't match sv-kb and, with a correct ``content_sha`` key, poisons the
    shared cache. A round-trip test recomputes the sha from ``(model, prefix, text)`` and asserts it equals
    the stored ``content_sha`` (the inputs are provably the ones the sha was built from).
  * **model + dimensions are LOCKED + stamped:** default ``text-embedding-3-small`` @ ``1536`` — the model
    name is inside ``content_sha`` and the target column is ``embedding_small vector(1536)``. The store and
    both manifests record ``embedding_model``/``dimensions``/``api_version`` so a model change is detectable.
  * **global ``content_sha`` dedup:** one unique sha -> one embed input -> one vector, shared across every
    source (PDF Stage 6 today, media-transcript chunks later). This is the cost lever.

Text/children ONLY — parents are never embedded (only ``ChildChunk`` carries a ``content_sha``); image
vectors stay with Stage 4 / the media-image stage. Append-only, idempotent, resumable (the store IS the
resume state: a ``content_sha`` already present is skipped), source-agnostic.
"""

EMBED_TOOL_NAME = "text-first-extract-embed"
EMBED_TOOL_VERSION = "1.0.0"

# The locked model/dims (Handoff §3.2/§10). Defaults match Stage 6's EMBEDDING_MODEL (the model name is
# baked into every child's content_sha) and the target tenant's ``embedding_small vector(1536)`` column.
# CLI flags can override, but they must match what Stage 6 stamped or the sha cache breaks — lock before a run.
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"
DEFAULT_DIMENSIONS = 1536

# text-embedding-3-small list price (Handoff §8). Configurable; used only for the manifest cost projection.
DEFAULT_COST_PER_MILLION_TOKENS = 0.02

__all__ = [
    "EMBED_TOOL_NAME", "EMBED_TOOL_VERSION", "DEFAULT_EMBEDDING_MODEL", "DEFAULT_DIMENSIONS",
    "DEFAULT_COST_PER_MILLION_TOKENS",
]
