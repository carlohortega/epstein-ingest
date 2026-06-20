# Handoff: Stage 8 — Unified Ingest (load to Postgres + Azure Storage; `src/ingest`)

**Status:** requirements / build handoff — **NOT yet implemented.** Design **committed.** Stages 1–6
immutable; Stage 7 (`src/embed`) produces the staged text vectors this stage loads. **This is the only stage
that writes to the live system** (Postgres + Azure Storage). It is **source-agnostic**: one ingester for PDF
sidecars+files **and** media sidecars+files.

**Module name:** `src/ingest`. CLI `python -m src.ingest`.

**Read first:** `HANDOFF-stage7-embed.md` (vector store), and the **reuse sources**
`~/repos/sv-kb/pipeline/shared/svkb_pipeline/{indexing.py,models.py,db.py,blob.py,blob_paths.py}` and the
schema under `~/repos/sv-kb/sql/migrations/`.

---

## 0. What it does (30-second version)

Stage 8 loads a fully-staged document (any `source_kind`) into the live sv-kb system: it **registers** the
document rows, **uploads blobs** to Azure Storage, **pre-loads** Stage-7's staged text vectors + Stage-4/
media image vectors, and calls **`svkb_pipeline.indexing.index_document` verbatim** to write the parent/child
chunks — with the embeddings already present so the indexer embeds nothing. The result is **indistinguishable
from a native sv-kb ingest** for everything the staged pipeline produced (the known divergences are the
documented scanned-page text gap and redaction-scrubbed text). Idempotent per document, resumable.

---

## 1. Core principles

- **Reuse `index_document` VERBATIM.** Don't re-implement chunk/embedding writes. The whole staging design
  exists so this stage is a thin loader.
- **Single writer / idempotent per document.** `index_document` deletes+rebuilds a document's chunks, so a
  re-ingest is safe. Parallelize across documents; one writer per document.
- **Source-agnostic.** The same loader handles PDF and media — `index_document` already takes
  `(IngestionEvent, parents)` and is `source_kind`-blind; media transcript chunks flow through identically.
- **Unify images at the table, by `content_hash`.** PDF image vectors (Stage 4) and media image vectors load
  into one `pipeline.image_embeddings`, deduped by image `content_hash` — `ON CONFLICT DO NOTHING`.
- **Tenant-scoped.** Every row is tenant-scoped under RLS; staged artifacts are tenant-agnostic so the
  tenant is chosen here.

---

## 2. The load order (per document)

1. **Resolve identifiers** (`derive_identifiers`: `document_name`=stem, `case_key`=`VOLxxxxx`,
   `dataset_key`=`DataSet-NN`) and build an **`IngestionEvent`** (`tenant_id`, those keys, `document_id`,
   `ingestion_id`, `source_kind`, `correlation_id`, blob refs). See `models.IngestionEvent`.
2. **Register rows** (Stage 8 owns this — `index_document` REQUIRES the `documents` row or raises
   `document_missing`): insert/upsert `pipeline.documents` (+ typed cols + the JSONB **metadata bag**, §4),
   `pipeline.document_pages` (`summary`/`summary_json`/`safety_tags`/`page_kind`; `vision_md_blob_path` is
   **null** for our corpus — caption-only), and `pipeline.asset_context` (from `document_summary_final` +
   per-page `summary_json`). Obtain `document_id`.
3. **Upload blobs** (Azure Storage, via `blob.py`/`blob_paths.py`): canonical PDF (`pdf_blob_path`), page/
   image/artifact PNGs, thumbnail, the `chunks.json` (`chunks_blob_path`), `asset_context` context blob.
   Record paths on the rows.
4. **Pre-load embeddings** (the parity trick): bulk-insert Stage-7's staged **text** vectors into
   `pipeline.embeddings` (`content_sha`, `embedding_model`, dims, `embedding_small/large`; `ON CONFLICT
   (tenant, content_sha, embedding_model) DO NOTHING`) and Stage-4/media **image** vectors into
   `pipeline.image_embeddings` (`content_hash`, `azure-ai-vision-multimodal`, `vector(1024)`; `ON CONFLICT …
   DO NOTHING`).
5. **Index chunks — `index_document` verbatim**, passing a **guard provider** (`EmbeddingProvider` whose
   `.embed()` raises). Because every child's `content_sha` was pre-loaded in step 4, the indexer's
   `WHERE content_sha = ANY(...) AND embedding_model=…` finds them all ⇒ `to_embed` is empty ⇒ the provider
   is never called. `index_document` writes the parent/child `pipeline.chunks` rows and marks
   `documents.status='complete'` + `document_progress.current_stage='index'`.

> If a `content_sha` is unexpectedly missing from the store, the guard provider **raises** — turning a silent
> re-embed into a loud, fixable failure (don't fall back to a live embed client here).

---

## 3. Image-embedding ownership (decided)

Stage 8 **loads** image vectors; it never creates them. PDF image vectors come from **Stage 4**
(`image_assets[].embedding_ref → <stem>/embeddings/*.emb.json`, `azure-ai-vision-multimodal`, 1024-d);
non-PDF image vectors come from the **media-image stage** (same format). Both land in
`pipeline.image_embeddings` deduped by image `content_hash`, so a photo that appears in a PDF and as a
standalone upload collapses to one vector. **Coverage gap remediation:** if a dataset's Stage 4 ran without
AI Vision env (null `embedding_ref`), **re-run Stage 4** (config/env, not a code change) — do **not** backfill
PDF images in a media-stage. The dataset-summary module reports `has_visual_content_true` vs `embedded` to
surface such gaps.

---

## 4. Metadata & metadata bags (decided — `sql/migrations/0009`)

Populate **both**:
- **Typed `documents` columns** (filter/sort/cite fast path): `source_kind`, `document_created_at`, bates,
  `pdf_blob_path`, `page_count`, etc.
- **The JSONB metadata bag** `documents.metadata` (GIN-indexed, open-ended): map the sidecar's `source`
  (`sha256`, `bytes`, `pdf_producer`, `pdf_metadata`), `document` (bates), and `generator` (tool/version/
  config) fields — the lossless provenance record.
- **`asset_context`** (`summary_text`, `summary_json`, `model`, `version`, `context_blob_path`) from
  `document_summary_final` + the per-page `summary_json` (key_points/entities/topics). Media adds
  `media_metadata` (typed cols + its own `metadata` bag from probe output).

---

## 5. Tenant / environment (decided posture)

Staged artifacts are **tenant-agnostic**; the **`tenant_id` + live-vs-staging DB are chosen at Stage 8**
(`db.tenant(tenant_id, user_sub)` sets the RLS context). Lock these before a bulk load — tenancy is baked into
every row.

---

## 6. Architecture — mirror `src/stageN`

Package `src/ingest/`: `config.py` (DB/Storage/tenant, batch sizes), `event.py` (`IngestionEvent` builder),
`register.py` (documents/pages/asset_context + metadata bag), `blobs.py` (uploads via `blob.py`),
`load.py` (pre-load embeddings + `index_document` with the guard provider), `walk.py` (per-document scheduler
+ run manifest), `cli.py`. Reuse `svkb_pipeline.indexing/db/blob/blob_paths/models` and
`src/stage6/walk.find_pdfs_pruned`. Launcher `C:\epstein-ingest\run_ingest.py` loads DB + Storage creds.

```
python -m src.ingest <ROOT> --tenant <UUID> [--source-kind pdf|media] [-j N] [--dry-run] [--force]
python -m src.ingest <ROOT> --dry-run   # rows/blobs/vectors to write, NO DB/Storage writes
```

---

## 7. Cost & scale

- Ingest itself is **infra cost, ~$0** (no model calls — embeddings are pre-staged). Bound by DB write
  throughput + blob upload bandwidth across ~1.4M docs/chunks.
- Resumable per document (`document_progress`/`documents.status`); a re-run skips completed docs;
  `index_document`'s delete+rebuild keeps re-ingest safe.

---

## 8. Acceptance criteria

1. **`index_document` reused verbatim**, provider never invoked (guard), `to_embed` empty (pre-load worked).
2. **Registration before index** (documents/pages/asset_context exist; no `document_missing`).
3. **Metadata bag + typed cols + asset_context** populated; `image_embeddings` deduped by `content_hash`.
4. **Idempotent/resumable:** re-ingest rebuilds a doc's chunks without duplicate embeddings (`ON CONFLICT`);
   completed docs skipped.
5. **Source-agnostic:** PDF and media docs load through the same path.
6. **Tenant-scoped:** every row under the chosen `tenant_id`/RLS.
7. **`--dry-run`** reports the write plan with **no DB/Storage writes**.
8. **Indistinguishable** from a native sv-kb ingest except the documented gaps (scanned-page text;
   redaction-scrubbed text).

---

## 9. Decisions (committed)

- S8 reuses `index_document` verbatim via **pre-load + guard provider**.
- S8 **owns registration** (creates `documents`/`pages`/`asset_context`).
- Populate **both** typed columns and the JSONB **metadata bag**; plus `asset_context`.
- Image vectors **loaded** (Stage 4 / media-image), unified in `image_embeddings` by `content_hash`;
  coverage gaps fixed by re-running Stage 4.
- Tenant + environment chosen at S8 (staged artifacts tenant-agnostic).
- One unified ingester for all `source_kind`s.

---

## 10. References

- **Reuse:** `svkb_pipeline/indexing.index_document` (chunk writes, cache check, status updates),
  `svkb_pipeline/{db,blob,blob_paths,models}.py`. **Schema:** `0002_documents.sql`,
  `0003_chunks_embeddings.sql`, `0009_asset_metadata_context.sql`.
- **Producers:** Stage 7 (`HANDOFF-stage7-embed.md`) text vectors; Stage 4 / media-image image vectors.

**Bottom line:** register the document rows + metadata bag, upload blobs, pre-load the staged text + image
vectors, then call `index_document` with a guard provider so it writes chunks and embeds nothing — one
tenant-scoped, idempotent, source-agnostic loader that makes our staged corpus indistinguishable from a
native sv-kb ingest (bar the documented scanned-text gap).
