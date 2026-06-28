"""Vector pre-load + the verbatim ``index_document`` call (Handoff §4/§5) — the parity trick.

Two responsibilities, both live (lazy sv-kb/psycopg imports):
  1. **Bulk pre-load the staged vectors** (once per apply): stream ``embeddings.copy`` → ``pipeline.embeddings``
     and ``image_embeddings.copy`` → ``pipeline.image_embeddings`` through TEMP staging tables, then
     ``INSERT … SELECT … ON CONFLICT DO NOTHING`` — injecting the tenant_id + embedding_model + the
     dim-correct vector column here (the package is tenant-agnostic). Text and image vectors go to DISTINCT
     tables/keys/columns (§4) and are NEVER conflated.
  2. **Index a document's chunks** by calling ``svkb_pipeline.indexing.index_document`` VERBATIM with a
     **guard provider** whose ``.embed()`` raises. Because every child ``content_sha`` was pre-loaded, the
     indexer's cache check leaves ``to_embed`` empty ⇒ the provider is never called. A missing sha ⇒ the
     guard raises loudly (don't silently re-embed live).

Plus the per-document ``image_occurrences`` insert (needs the apply-time ``document_id``).
"""

from __future__ import annotations

import os


class GuardProvider:
    """An ``EmbeddingProvider`` (Protocol: model/dimensions/last_usage/embed) that must NEVER be invoked.
    If ``index_document`` finds an un-cached child (a content_sha the pre-load didn't cover), it calls
    ``embed()`` — we raise instead of doing a live embed, turning a silent miss into a fixable failure (§5)."""

    def __init__(self, model: str, dimensions: int) -> None:
        self.model = model
        self.dimensions = dimensions
        self.last_usage: dict | None = None

    def embed(self, texts):
        if not texts:
            return []
        raise RuntimeError(
            f"guard provider invoked for {len(texts)} un-cached chunk(s): a child content_sha was not in the "
            f"pre-loaded embed store. Re-embed (Stage 7) so every content_sha is present, then re-apply.")


def preload_vectors(db, tenant_id: str, pkg_dir: str, cfg, *, user_sub: str | None = None) -> dict:
    """Bulk-load the package's COPY vector files into their two distinct tables. Idempotent (ON CONFLICT)."""
    text_copy = os.path.join(pkg_dir, "embeddings.copy")
    img_copy = os.path.join(pkg_dir, "image_embeddings.copy")
    counts = {"text_rows": 0, "image_rows": 0}

    with db.tenant(tenant_id, user_sub) as cur:
        if os.path.exists(text_copy) and os.path.getsize(text_copy):
            cur.execute("CREATE TEMP TABLE _stage_text (content_sha text, dims int, vec text) ON COMMIT DROP")
            with cur.copy("COPY _stage_text (content_sha, dims, vec) FROM STDIN") as cp:
                with open(text_copy, "r", encoding="utf-8") as f:
                    for line in f:
                        cp.write(line)
            # route by dims into the dim-correct pgvector column (CHECK enforces exactly one populated)
            cur.execute(
                """INSERT INTO pipeline.embeddings
                     (tenant_id, content_sha, embedding_model, embedding_dimensions, embedding_small)
                   SELECT %s, content_sha, %s, dims, vec::vector FROM _stage_text WHERE dims=1536
                   ON CONFLICT (tenant_id, content_sha, embedding_model) DO NOTHING""",
                (tenant_id, cfg.embedding_model))
            cur.execute(
                """INSERT INTO pipeline.embeddings
                     (tenant_id, content_sha, embedding_model, embedding_dimensions, embedding_large)
                   SELECT %s, content_sha, %s, dims, vec::vector FROM _stage_text WHERE dims=3072
                   ON CONFLICT (tenant_id, content_sha, embedding_model) DO NOTHING""",
                (tenant_id, cfg.embedding_model))
            cur.execute("SELECT count(*) FROM _stage_text")
            counts["text_rows"] = int(cur.fetchone()[0])

    with db.tenant(tenant_id, user_sub) as cur:
        if os.path.exists(img_copy) and os.path.getsize(img_copy):
            cur.execute("CREATE TEMP TABLE _stage_img (content_hash text, dims int, vec text) ON COMMIT DROP")
            with cur.copy("COPY _stage_img (content_hash, dims, vec) FROM STDIN") as cp:
                with open(img_copy, "r", encoding="utf-8") as f:
                    for line in f:
                        cp.write(line)
            cur.execute(
                """INSERT INTO pipeline.image_embeddings (tenant_id, content_hash, embedding_model, embedding)
                   SELECT %s, content_hash, 'azure-ai-vision-multimodal', vec::vector FROM _stage_img
                   ON CONFLICT (tenant_id, content_hash, embedding_model) DO NOTHING""",
                (tenant_id,))
            cur.execute("SELECT count(*) FROM _stage_img")
            counts["image_rows"] = int(cur.fetchone()[0])

    return counts


def insert_occurrences(cur, plan: dict, *, tenant_id: str, document_id: str, processed_prefix: str) -> int:
    """Insert ``image_occurrences`` for this document — turns a deduped image vector hit into a located
    citation. ``image_blob_path`` is the processed-prefix-rooted path of the (placeholder-or-real) image."""
    ident = plan["identifiers"]
    n = 0
    for occ in (plan.get("image_occurrences") or []):
        page = occ.get("page_number")
        page = page if isinstance(page, int) else -1
        ts = occ.get("timestamp_ms")
        ts = ts if isinstance(ts, int) else -1
        frame = occ.get("frame_index")
        frame = frame if isinstance(frame, int) else -1
        cur.execute(
            """INSERT INTO pipeline.image_occurrences
                 (tenant_id, content_hash, document_id, case_key, dataset_key, document_name, artifact_kind,
                  page_number, timestamp_ms, frame_index, image_blob_path, embedding_model)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, content_hash, document_id, artifact_kind, page_number, frame_index)
               DO NOTHING""",
            (tenant_id, occ.get("content_hash"), document_id, ident["case_key"], ident["dataset_key"],
             ident["document_name"], occ.get("artifact_kind") or "pdf_image", page, ts, frame,
             f"{processed_prefix}{occ.get('image_artifact')}", occ.get("embedding_model")
             or "azure-ai-vision-multimodal"))
        n += 1
    return n


def index_document_verbatim(db, event, parents, *, model: str, dimensions: int, page_count, batch_size: int):
    """Call the sole chunk/embedding writer VERBATIM with the guard provider (it embeds nothing)."""
    from svkb_pipeline.indexing import index_document        # lazy: sv-kb on path only at apply
    return index_document(db, event, parents, GuardProvider(model, dimensions),
                          page_count=page_count, batch_size=batch_size)
