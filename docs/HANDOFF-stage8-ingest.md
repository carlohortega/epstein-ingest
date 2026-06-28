# Handoff: Ingest — the shared load tail (`src/ingest`)

**Status:** **SPLIT INTO TWO STEPS (decided 2026-06-25): S8 = prepare, S9 = apply.** S8 builds an offline,
no-creds, reviewable **apply-package** (a per-document apply-plan + `COPY` data files for vectors + a blob
path-manifest + a run manifest); **S9** consumes it and is the *only* step that writes the live system
(Postgres + Azure Storage) — via the **verbatim `index_document`** + bulk vector pre-load + `azcopy`.

**BUILD STATUS (updated 2026-06-28): both steps now implemented; S8 validated offline, S9 creds-gated.**
- **S8 prepare — built + tested.** `record.py` (full tenant-agnostic apply record), `emit.py` (writes
  `apply-plan.jsonl` + `embeddings.copy` + `image_embeddings.copy` + `blobs.manifest.jsonl` +
  `prepare-manifest.json`), CLI `--prepare`. The earlier offline core (`discover`/`safety`/`store_reader`/
  `plan`/`--dry-run`) is preserved + still validated on real DS-12. `tests/run_ingest_tests.py` PASSES (incl.
  the new prepare acceptance section).
- **S9 apply — built, NOT yet run live.** `event.py`, `tenancy.py`, `register.py`, `load.py` (bulk COPY
  pre-load + `index_document` verbatim + guard provider + `image_occurrences`), `blobs.py` (azcopy + §4a
  placeholders), `apply.py` (per-doc scheduler + manifest), CLI `--apply`. The pure logic is unit-tested
  offline in `tests/run_ingest_s9_tests.py` (PASSES); the live integration is **gated on creds + a live DB +
  the staged corpus being mounted** (none available in the build session — the Epstein datasets were not on a
  mounted drive). Run the preflight checklist below before the first live apply.
- **§3a tenancy = A (implemented default):** require pre-created `pipeline.cases`/`pipeline.datasets`, fail
  loud via a read-only preflight before any write; `--ensure-tenancy` opts into upsert-on-first-sight.

**Implementation notes / refinements made during the build (each preserves the handoff's intent):**
1. **The package is tenant-agnostic** (§6 invariant). The COPY vector files carry NO `tenant_id`/model and the
   blob manifest carries LOGICAL refs (case/dataset/document + artifact suffix + §4a action). S9 projects the
   `tenant_id`, the `embedding_model`, the dim-correct vector column, and the tenant blob path **at apply**.
2. **`image_occurrences` are emitted per-document in `apply-plan.jsonl`, not as a standalone `.copy`** — they
   need the apply-time `document_id` (a UUID), so they can't be a static content-keyed COPY. Only the two
   genuinely content-keyed bulk inserts (text `embeddings`, `image_embeddings`) are COPY files.
3. **Withheld/blank units:** the placeholder BYTES are uploaded to the unit's own blob path (real bytes never
   uploaded; vector absent), so any fetch of that path returns safe content; the shared placeholder is also
   seeded once under `_system/redaction/`.
4. **Image `content_hash`** (the `image_embeddings` dedup key) is NOT in the sidecar — S8 computes it as the
   sha256 of the asset PNG bytes (the `.emb.json` supplies the 1024-d vector). So a live apply needs the
   staged PNGs present (same mount that azcopy reads blobs from).
5. **First-live-apply preflight:** (a) set `DATABASE_URL` + `AZURE_STORAGE_ACCOUNT` (+ `AZURE_STORAGE_SAS` or
   `azcopy login`); (b) pre-create the `epstein` case + each `DataSet-NN` dataset (or pass `--ensure-tenancy`);
   (c) confirm `azcopy` is on PATH; (d) **spot-check the field mapping against a real merged sidecar** —
   `record.py`'s later-stage field reads (`document_summary_final`, per-page `summary_json`, `image_assets`,
   `.emb.json`) are grounded in the producer code but were not re-validated against live DS data this session;
   (e) run on ONE small dataset (DS-12) first and diff the apply-manifest against the dry-run/prepare manifest.

Stages 1–6 immutable; `embed` produced the staged vectors (all 12 datasets embedded). DS-12 dry-run: 149/152
to ingest, **3,720/3,720 vectors present (0 missing)**, 3 CSAM docs held out, 1 content-filter withhold, 154
blank pages.

**Naming / the "two tails" model (decided this cycle).** The pipeline is two producer tracks that converge on
two shared tails — ordinals are retired in favor of functional names (consistent with `embed`):

```
PDF track:    stage-1 … stage-6        extract → render → summarize → vision(caption+image-embed) →
                                        finalize → chunk.   PDF-embedded images handled HERE.
Media track:  media-stage-1 … media-n  (NOT BUILT) probe → transcribe A/V → caption+embed standalone
                                        images → chunk transcripts.   Standalone dataset images handled HERE.
                              ↓  both tracks emit the SAME artifacts: <name>.chunks.json + image vectors
Shared tail:  embed    text vectors → content_sha-keyed float32 store (source-agnostic, global dedup, re-runnable)
              S8 prepare  → reviewable apply-package (plan + COPY vectors + blob path-manifest); NO creds  ◄── THIS DOC
              S9 apply    → verbatim index_document + bulk pre-load + azcopy; the ONLY live-writing step  ◄── THIS DOC
```

`ingest` is **source-agnostic** by design (one loader for PDF and, later, media) and runs **last** — it needs
a fully-staged document. **PDF-first is fine:** `embed` and `ingest` are both re-runnable and idempotent, so
the PDF corpus can be embedded + ingested before the media track exists; media is folded in later by re-running
the same tails. (The filename keeps the `stage8` ordinal only to preserve existing cross-references.)

**Module name:** `src/ingest`. CLI `python -m src.ingest`.

**Read first:** `HANDOFF-stage7-embed.md` (the text vector store), and the **reuse sources**
`~/repos/sv-kb/pipeline/shared/svkb_pipeline/{indexing.py,models.py,db.py,blob.py,blob_paths.py}` and the
schema under `~/repos/sv-kb/sql/migrations/` (`0001_core_tenancy.sql`, `0002_documents.sql`,
`0003_chunks_embeddings.sql`, `0009_asset_metadata_context.sql`, `0100_rls_session.sql`).

---

## 0. What it does (30-second version)

Loading a fully-staged corpus into the live sv-kb system is split into **two steps**:
- **S8 (prepare)** — offline, **no creds**, deterministic, reviewable. Reads the staged sidecars/chunks/embed
  store, applies the §4a safety gate, resolves identifiers (`case_key='epstein'`/`DataSet-NN`/stem), and emits
  an **apply-package**: a per-document apply-plan, `COPY` data files for the text + image vectors, a blob
  **path-manifest** (`staged_file → tenant blob path`, §4a-resolved), and a run manifest. Makes NO API calls.
- **S9 (apply)** — the **only** step that writes the live system. Consumes the package: resolves/ensures
  tenancy, **uploads blobs via `azcopy`** from the path-manifest, bulk-loads the vectors (`COPY`), and calls
  **`svkb_pipeline.indexing.index_document` VERBATIM** per document (embeddings already present ⇒ it embeds
  nothing). Idempotent per document, restartable; can run on a different host/session from S8.

The result is **indistinguishable from a native sv-kb ingest** for everything staged (documented divergences:
scanned-page text gap; redaction-scrubbed text; §4a content-safety mechanism — §11).

---

## 0a. The two-step model — S8 prepare → the apply-package → S9 apply

**Why split (decided 2026-06-25).** Same philosophy as the rest of the pipeline: keep the offline,
deterministic, reviewable part separate from the risky live part. S8 needs no creds and produces a
**sign-offable artifact** ("exactly these rows/blobs go live; these CSAM docs do not; these assets are
withheld"); S9 is a thin, restartable executor that can run later, elsewhere, by whoever holds the creds.

**The apply-package (the S8→S9 interface), written under `<corpus>/_apply/`:**
- **`apply-plan.jsonl`** — one record per ingestable document: identifiers (`epstein`/`DataSet-NN`/stem),
  the `documents`(+metadata bag)/`document_pages`/`asset_context`/`content_safety_findings` field sets, the
  child `content_sha` list, `source_withheld`, withheld units (`review_status='withheld'`), and the blob refs.
- **`embeddings.copy` / `image_embeddings.copy` / `image_occurrences.copy`** — `COPY`-format data files for
  the simple bulk inserts (compact + fast; `ON CONFLICT DO NOTHING` via a staging-table load at apply).
- **`blobs.manifest.jsonl`** — `{source_path, container, blob_path, content_type}`; withheld units point at
  the shared placeholder, CSAM docs are absent, blank pages → `page-blank`. **No file copying** — S9 `azcopy`s
  each from its staged location (don't duplicate hundreds of GB).
- **`prepare-manifest.json`** — counts, CSAM-skipped list, withheld-by-tier, tenancy pairs, vectors_missing.

**Two refinements that protect our invariants (do NOT hand-write SQL for everything):**
1. **Fidelity:** chunk + registration writes go through **`index_document` VERBATIM** at apply — it is
   read-then-write (`RETURNING` ids, the embedding-cache check) and cannot be faithfully frozen as static SQL.
   Only the *simple* bulk inserts (vectors/occurrences) are materialized as `COPY` files.
2. **Scale:** literal SQL for ~10M rows incl. 1536-float vector literals is tens of GB of text — `COPY` data
   files + `azcopy` for blobs are the compact, fast path.

**`index_document` makes NO embedding calls** (guard provider, §5); S9 only *routes* pre-computed vectors to
two tables (§4) + uploads blobs.

---

## 1. Core principles

- **Reuse `index_document` VERBATIM** (via pre-load + guard provider, §3/§5). The whole staging design exists
  so this stage is a thin loader; don't re-implement chunk/embedding writes.
- **`ingest` OWNS registration.** `index_document` REQUIRES the `documents` row (raises `document_missing`
  otherwise) and does **not** create tenancy/case/dataset/document/page rows — so `ingest` does (§3).
- **Single writer / idempotent per document.** `index_document` deletes+rebuilds a document's chunks; every
  other write is `ON CONFLICT DO NOTHING`/upsert. Parallelize across documents; one writer per document.
- **Text vs image embeddings are DISTINCT and must be routed correctly** (§4): different origin, file format,
  target table, dedup key, vector column, and dimensions. Conflating them corrupts retrieval.
- **Tenant-scoped under RLS.** Every row is written inside `db.tenant(tenant_id, user_sub)`; staged artifacts
  are tenant-agnostic so the tenant is chosen *here* (§6).
- **Source-agnostic.** The same loader handles PDF and media — `index_document` is `source_kind`-blind; media
  transcript chunks + standalone-image vectors flow through identically.

---

## 2. The S9 apply order (per document)

This is what **S9 (apply)** does per document, driven by the S8 apply-package (§0a). Steps 1–2 (identifiers,
register fields) and the §4a decisions are precomputed by **S8** into `apply-plan.jsonl`; S9 executes them.

1. **Resolve identifiers** (`discover.derive_identifiers`: `document_name`=stem, **`case_key`='epstein'**
   (constant — verified from the chunks' `context_prefix`), `dataset_key`=`DataSet-NN`) and build an
   **`IngestionEvent`** (§3 lists its required fields).
2. **Resolve tenancy → UUIDs (the new hard requirement, §3a).** `documents.case_id`/`dataset_id` are NOT-NULL
   FKs to `pipeline.cases`/`pipeline.datasets`, but staged data carries only the string keys. Look up (or
   create) the `tenant → case → dataset` rows to obtain `case_id`/`dataset_id` before the document insert.
3. **Register rows** — insert/upsert `pipeline.documents` (typed cols + the JSONB **metadata bag**, §4/§8),
   `pipeline.document_pages` (from the Stage-1 sidecar `pages[]`; `vision_md_blob_path` is **null** for our
   corpus — caption-only), and `pipeline.asset_context` (from `document_summary_final` + per-page
   `summary_json`). Obtain `document_id`.
4. **Upload blobs** (Azure Storage, via `blob.py`/`blob_paths.py`): canonical PDF (`pdf_blob_path`), page/
   image/artifact PNGs, thumbnail, the `chunks.json` (`chunks_blob_path`), the `asset_context` context blob.
   Record paths on the rows. **Every image/page/thumbnail blob passes the §4a safety-redaction gate first** —
   high-risk images are replaced with a placeholder and never uploaded.
5. **Pre-load embeddings** (the parity trick, §4): bulk-insert the staged **text** vectors into
   `pipeline.embeddings` and the **image** vectors into `pipeline.image_embeddings` — both `ON CONFLICT … DO
   NOTHING`.
6. **Index chunks — `index_document` verbatim**, passing a **guard provider** whose `.embed()` raises. Because
   every child's `content_sha` was pre-loaded in step 5, the indexer's `WHERE content_sha = ANY(...) AND
   embedding_model=…` finds them all ⇒ `to_embed` is empty ⇒ the provider is never called. `index_document`
   writes the parent/child `pipeline.chunks` rows and marks `documents.status='complete'` +
   `document_progress.current_stage='index'`.

> If a `content_sha` is unexpectedly missing from the store, the guard provider **raises** — turning a silent
> re-embed into a loud, fixable failure (don't fall back to a live embed client here).

---

## 2a. What to walk — the `IMAGES` directory (decided; DS-9 special; DS-1..8 have NO VOL layer)

**Invariant (confirmed across ALL 12 datasets):** legitimate PDF source files live ONLY under an **`IMAGES`**
directory; everything else (`DATA`, `NATIVES`, and DS-9's `CORRUPTED`/`DB-EXTRACT`/`EMPTY`/`MISSING-NATIVES`)
is excluded / non-PDF and MUST NOT be ingested. **But the IMAGES dir is at different depths per dataset**, so
`ingest` must **locate the `IMAGES` dir per dataset and walk it recursively** — do NOT hard-code `VOL*/IMAGES`
(that would silently MISS DS-1..8, which have no VOL layer).

**THREE layout shapes (verified; `chunks.json` only ever under IMAGES):**
- **DS-1..8 — IMAGES at the DATASET ROOT, no VOL dir:** `DataSet-NN/IMAGES/<NNNN>/<stem>.{pdf,json,chunks.json}`.
- **DS-9 — VOL layer, FLAT IMAGES:** `DataSet-09/VOL00009/IMAGES/<stem>.{pdf,json,chunks.json}` (no numbered
  subdirs), beside the excluded sibling dirs.
- **DS-10/11/12 — VOL layer, nested IMAGES:** `DataSet-NN/VOL000NN/IMAGES/<NNNN>/<stem>.…`.

Robust rule: for each dataset, **find its `IMAGES` dir** (`DataSet-NN/IMAGES` or `DataSet-NN/VOL*/IMAGES`) and
walk it recursively (reuse `src/stage6/walk.find_pdfs_pruned`). Excludes junk by construction; handles
root-level, flat, and nested shapes.

**Embed-store location is PER-DATASET config (not uniform) — pass `--store` explicitly:**
- **DS-1..8 and DS-10/11/12 → `DataSet-NN/.embeddings`** (store at the dataset root).
- **DS-9 → `VOL00009/IMAGES/.embeddings`** (DS-9's embed + Stage-6 ran scoped to IMAGES, so its store +
  run-manifest sit at the IMAGES level). The store is content-addressed and location-independent, so the path
  is the only thing `ingest` needs.

**All 12 datasets are Stage-7 embedded** (DS-1..8 embedded 2026-06-21; DS-9/10/11/12 this build).

**Decision — do NOT restructure DS-9 (or DS-1..8).** Moving ~529k flat files (DS-9) is the expensive, risky
op to avoid, and it fixes nothing — the per-dataset "find IMAGES + use its store path" rule handles every
shape. If uniform store paths are ever desired, move ONLY the 256-file `.embeddings` dir (a safe single-dir
move, no internal path deps) — never the corpus.

---

## 3. Mandatory fields & where they come from (provenance)

Everything below is grounded in the live schema (NOT-NULL + no-default = an insert MUST supply it). **No
mandatory field is without a staged source** — *except* the tenancy UUIDs (§3a), which must be resolved.

**`pipeline.documents` — must supply:** `tenant_id`, `case_id`, `dataset_id`, `case_key`, `dataset_key`,
`document_name`, `source_kind`. (`source`/`status`/`created_at`/`updated_at` have server defaults.)

| Field | Mandatory | Staged source |
|---|---|---|
| `tenant_id` | yes | **chosen at ingest** (CLI/config; sets RLS context) |
| `case_id` | yes | **resolved** from `case_key` via `pipeline.cases` (§3a) |
| `dataset_id` | yes | **resolved** from `dataset_key` via `pipeline.datasets` (§3a) |
| `case_key` | yes | **constant `'epstein'`** (`IngestConfig.case_key`; verified baked into the chunks' `content_sha` `context_prefix` as `epstein/DataSet-NN`) — NOT `VOL#####` |
| `dataset_key` | yes | path `DataSet-NN` (`discover.derive_identifiers`) |
| `document_name` | yes | PDF stem (`discover.derive_identifiers`) |
| `source_kind` | yes | `'pdf'` (chosen per track; media stages set their own) |
| `page_count`, `pdf_blob_path`, `document_created_at`, `filehash`, … | no | Stage-1 `source.{page_count,sha256,…}`, blob upload, bates |

**`pipeline.document_pages` — must supply:** `tenant_id`, `document_id`, `page_number`.
Source: RLS context, the inserted `document_id`, and Stage-1 `pages[].page_number`. (`page_class`/`ocr_text`/
`summary`/`safety_tags` etc. are nullable; populate from the sidecar where present. `vision_md_blob_path`
stays **null** — caption-only.)

**`pipeline.chunks` — must supply:** `tenant_id`, `document_id`, `case_key`, `dataset_key`, `document_name`,
`text` (+ defaulted `chunk_index`/`chunk_kind`/`heading_path`/`is_child`). **All written by `index_document`
from the `chunks.json`** — no caller action; `content_sha`/`context_prefix` already present from Stage 6.

**`pipeline.asset_context` — must supply:** `tenant_id`, `document_id` (PK). `summary_json` defaults `{}`;
populate from Stage-5 `document_summary_final` + per-page `summary_json`. (Native sv-kb leaves this for a
later summarizer — we fill it at ingest.)

**`IngestionEvent` required fields** (`models.py`, validated by `from_message`): `tenant_id`, `case_key`,
`dataset_key`, `document_name`, `document_id`, `ingestion_id`, `source_kind`, `correlation_id`. Optional:
`content_type`, `source` (default `"upload"`), `source_url`, `blob_container`, `blob_path`,
`ingested_by_user_sub`.

**RLS (`db.py`):** every insert runs inside `with db.tenant(tenant_id, user_sub):` which sets
`app.current_tenant_id` + `app.current_user_sub`. Missing context ⇒ deny-by-default (NULL tenant ⇒ no rows).
`user_sub` = `IngestionEvent.ingested_by_user_sub` (optional).

### 3a. The one real gap to decide before building: tenancy resolution (`case_id`/`dataset_id`)

`pipeline.documents` needs the **UUIDs** `case_id` + `dataset_id` (FK to `pipeline.cases`/`pipeline.datasets`,
unique on `(tenant_id, case_key)` and `(tenant_id, case_id, dataset_key)`), but the staged corpus only has the
**string keys**. Native sv-kb has these rows created by an out-of-band onboarding step. **Decide the policy:**
- **(a) Require pre-created** tenant/case/dataset rows; `ingest` looks them up and **fails loudly** if absent
  (safest for a controlled live load); OR
- **(b) Upsert-on-first-sight** — `ingest` creates the `cases`/`datasets` rows (idempotently) from the keys.

Recommendation: **(a) with an explicit `--ensure-tenancy` opt-in for (b)**, so a bulk load can't silently
fabricate org structure, but a dev/staging run can bootstrap. This is the only mandatory value with no
automatic staged source — call it out in the run preflight.

---

## 4. Text vs image embeddings — two tables, routed (do NOT conflate)

`ingest` loads two *already-computed* vector kinds into two different tables. It never calls an embedding API.

| | **Text embeddings** | **Image embeddings** |
|---|---|---|
| Produced by | `embed` (shared tail) via **Azure OpenAI** | Stage 4 (PDF) / media-image stage via **Azure AI Vision** |
| Staged form | `content_sha`-keyed float32 store (`<root>/.embeddings/*.f32v`) | per-asset `…/embeddings/*.emb.json` (Stage 4), image `content_hash` |
| Target table | `pipeline.embeddings` | `pipeline.image_embeddings` |
| Dedup / conflict key | `(tenant_id, content_sha, embedding_model)` | `(tenant_id, content_hash, embedding_model)` |
| Vector column | `embedding_small vector(1536)` **or** `embedding_large vector(3072)` | `embedding vector(1024)` |
| Dims / model name | 1536 (`text-embedding-3-small`) / 3072 (`-large`) | 1024 / `azure-ai-vision-multimodal` |
| Column choice | by `embedding_dimensions` (1536→small, 3072→large; CHECK enforces exactly one) | single column |
| Insert | `ON CONFLICT … DO NOTHING` (first-writer-per-key wins) | `ON CONFLICT … DO NOTHING` |

Both must carry the correct `embedding_model` string (it is part of the key and, for text, baked into
`content_sha`). The serialized text vector must read back as the value `indexing._vector_literal`
(`repr(float(x))`) would insert. **Coverage gap (image):** if a dataset's Stage 4 ran without AI Vision env,
`embedding_ref` is null and there is nothing to load — fix by **re-running Stage 4** (config, not a media
stage). The dataset-summary reports `has_visual_content_true` vs `embedded` to surface this.

---

## 4a. Content-safety handling — STAGED conformance (REQUIRED, fail-safe)

Canonical spec: **`sv-kb/docs/SAFETY-staged-vs-svkb.md`** (settled 2026‑06‑23→25). Staged ingest and native
sv-kb write the **same** data model and are served by the same gateway/orchestrator; they differ only in
*mechanism*: **staged REMOVES violations pre-ingest** (the corpus is company-curated), **sv-kb gates at
serving**. Reserve **"redaction"** for the source documents' own `[REDACTED]` black bars — the system action
here is **"withhold / suppress."** Three outcomes:

**1. CSAM-suspected → the WHOLE document is NOT ingested.** Trigger: vision `minor_indicator`, or a
`csam`/`csam_suspected` category. The document gets **no rows, no blobs, no vectors** and is **preserved +
reported in the staging system of record** (NEVER deleted — §6 of the canonical doc; obtain counsel). Listed
under `csam_skip` in the manifest. (sv-kb analog: `safety_status='quarantined'`, whole-doc 403, preserve + NCMEC.)

**2. Withhold (asset-level) → the asset is removed; its page gets a placeholder.** Trigger — HIGH-CONFIDENCE,
**never bare `review`** (DS-12 flags ~57% `review`): vision `explicit`; a `sexual`/`violence`/`hate`/`self_harm`
category; a Content-Safety severity ≥ threshold (4); an Azure content-filter block; or — **fail-safe** — an
un-vetted/errored image. (**hate & self-harm ARE withheld in both systems**, per 2026‑06‑25.) For each:
  - **Remove ALL derived blobs** — page PNG + page thumbnail + artifact PNG + artifact thumbnail — and serve
    the shared **`content-redacted` placeholder** in their place (gateway path
    `sv-kb-processed/_system/redaction/content-redacted.png`; committed `assets/redaction/content-redacted.png`,
    sha `408082bb…`, byte-identical in both repos).
  - **Vector ABSENT** — never load the 1024-d image vector (the asset never existed for retrieval); any
    `image_occurrences` row references the placeholder `content_hash`.
  - **Page-level rule:** if ANY asset on a page is withheld, the **whole page render** is replaced.
  - Set per-unit **`review_status='withheld'`** (migration `0013`, **TERMINAL** — NOT `pending_review`, which
    is sv-kb-only) and **`documents.source_withheld=true`** (gateway then refuses the downloadable source
    PDF/media). Do **NOT** set `safety_status='quarantined'` for non-CSAM (that value is CSAM-only/whole-doc).

**3. Blank / effectively-blank PDF page → `page-blank` placeholder** (gray; staged-only — no sv-kb analog;
`assets/redaction/page-blank.png`, sha `4c23fd44…`). Detected from `page_kind=="blank"` /
`has_visual_content==false` / Stage-1 blank metrics. Benign: no finding, no embedding to omit.

**Standalone media (image/audio/video)** flagged → **not uploaded at all** (no document, no bytes); logged in
the staging manifest; CSAM-suspected media → not ingested + preserve/report. (Media-track rule.)

**Also write the shared safety data model** (so a row is indistinguishable to the reader):
`pipeline.content_safety_findings` (`0011`: per-unit severities + `csam_suspected`),
`documents.content_safety`/`safety_status`/`csam_suspected`, `document_pages.safety_tags`. Text page
chunks/embeddings are unaffected (text is not the risk).

**Audit:** the manifest reports `csam_documents_NOT_ingested` (+ list), `images_withheld` by tier, and
`source_withheld_docs`; `--dry-run` projects all of it with no writes. **Validated on DS-12:** 149/152 to
ingest, **3 CSAM docs held out**, 1 content-filter withhold, 154 blank pages, 0 missing vectors.

---

## 5. Reuse `index_document` via pre-load + guard provider

`index_document` (sole writer of `chunks`/`embeddings`) checks `WHERE content_sha = ANY(...) AND
embedding_model=…` before embedding. Pre-load step 5 makes that set complete ⇒ `to_embed == []` ⇒ the provider
is never invoked. Pass a `EmbeddingProvider` whose `.embed()` **raises** (the guard) so any unexpected miss is
loud, not a silent live re-embed. `index_document` then writes the parent/child rows and flips
`documents.status='complete'` + `document_progress`.

---

## 6. Tenant / environment (decided posture)

Staged artifacts are **tenant-agnostic**; the **`tenant_id` + live-vs-staging DB are chosen at `ingest`**
(`db.tenant(tenant_id, user_sub)` sets the RLS context). Lock these before a bulk load — tenancy is baked into
every row, and tenancy resolution (§3a) depends on the chosen tenant.

---

## 7. Architecture — mirror `src/stageN`

Package `src/ingest/`, split by step.

**S8 prepare (BUILT — offline, no creds):**
- `discover.py` (find IMAGES per dataset + `derive_identifiers`, `case_key='epstein'`), `safety.py` (the §4a
  gate), `store_reader.py` (vectors by `content_sha`), `plan.py` (per-doc `DocPlan`), `config.py`,
  `walk.py` (`--dry-run`), `record.py` (**BUILT** — the full tenant-agnostic `ApplyRecord`), `emit.py`
  (**BUILT** — writes the **apply-package** §0a: `apply-plan.jsonl`, the `*.copy` vector data files,
  `blobs.manifest.jsonl`, `prepare-manifest.json` under `<corpus>/_apply/`), `cli.py` (`--dry-run`/`--prepare`).
  Self-contained: imports NO psycopg/azure/PyMuPDF.

**S9 apply (BUILT — the only live-writing step; creds-gated, not yet run live):** `tenancy.py` (resolve/ensure `cases`/`datasets`),
`event.py` (`IngestionEvent`), `register.py` (documents/pages/asset_context/content_safety_findings + metadata
bag), `load.py` (bulk `COPY` the vector files into staging→`ON CONFLICT`, then `index_document` **VERBATIM**
with the guard provider), `blobs.py` (drive **`azcopy`** from `blobs.manifest.jsonl`), `apply.py` (per-doc
scheduler + apply manifest). Reuse `svkb_pipeline.{indexing,db,blob,blob_paths,models}`. Lazy-import sv-kb/
psycopg/azure here only. Launcher `C:\epstein-ingest\run_apply.py` loads DB + `AZURE_STORAGE_*` creds.

```
# S8 prepare (no creds): emit the apply-package
python -m src.ingest <ROOT...> --prepare --out <corpus>/_apply
# S9 apply (creds): consume the package, write the live system
python -m src.ingest --apply <corpus>/_apply --tenant <UUID> [--ensure-tenancy] [-j N] [--force]
```

---

## 8. Metadata & metadata bag (decided — `0009`)

Populate **both**:
- **Typed `documents` columns** (filter/sort/cite fast path): `source_kind`, `document_created_at`, bates,
  `pdf_blob_path`, `page_count`, `filehash`, etc.
- **The JSONB metadata bag** `documents.metadata` (GIN-indexed, open-ended): map the sidecar's `source`
  (`sha256`, `bytes`, `pdf_producer`, `pdf_metadata`), `document` (bates), and `generator` (tool/version/
  config) — the lossless provenance record. **Same mapping native sv-kb's `normalize-job` uses** (merged on
  upsert), so the bag is parity, not a fork. For an Epstein-style corpus the bag fields are largely uniform
  across docs (producer/metadata/bates shape), so a single mapping covers the dataset.
- **`asset_context`** (`summary_text`, `summary_json`, `model`, `version`, `context_blob_path`) from
  `document_summary_final` + per-page `summary_json` (key_points/entities/topics). Media adds `media_metadata`
  later.

---

## 9. Cost & scale

- `ingest` itself is **infra cost, ~$0** — no model calls (embeddings pre-staged; images pre-staged). Bound by
  DB write throughput + blob upload bandwidth across ~1.4M docs/chunks.
- Resumable per document (`document_progress`/`documents.status`); a re-run skips completed docs;
  `index_document`'s delete+rebuild keeps re-ingest safe; all other writes `ON CONFLICT`.

---

## 10. Acceptance criteria

0. **S8 prepare emits a complete apply-package** (`apply-plan.jsonl` + `*.copy` vectors + `blobs.manifest.jsonl`
   + `prepare-manifest.json`) offline with **no creds**, deterministically, and is reviewable; **S9 apply**
   consumes it and is the only step that writes the live system. The package replays to the same end-state.
1. **`index_document` reused verbatim**, guard provider never invoked, `to_embed` empty (pre-load worked).
2. **Tenancy resolved** (`case_id`/`dataset_id` obtained per §3a policy) and **registration before index**
   (documents/pages/asset_context exist; no `document_missing`).
3. **Text vs image vectors routed correctly** (§4): text → `embeddings` (`content_sha`, small/large by dims);
   image → `image_embeddings` (`content_hash`, 1024-d); both deduped by their `ON CONFLICT` key.
4. **Metadata bag + typed cols + asset_context** populated; mandatory fields (§3) all sourced.
5. **Idempotent/resumable:** re-ingest rebuilds a doc's chunks without duplicate embeddings; completed docs
   skipped; tenancy upsert idempotent.
6. **Tenant-scoped:** every row under the chosen `tenant_id`/RLS (`db.tenant`).
7. **`--dry-run`** reports the write plan + tenancy preflight (would-create vs found) with **no DB/Storage
   writes**.
8. **Indistinguishable** from a native sv-kb ingest except the documented gaps (§11).
9. **Content-safety (§4a):** **CSAM-suspected → whole doc NOT ingested** (preserve/report); **withhold** (sexual/
   violence/hate/self-harm/explicit/content-filter/unvetted) → asset removed, all derived blobs replaced with
   `content-redacted`, **vector absent**, per-unit `review_status='withheld'` (terminal), `source_withheld=true`;
   **blank page → `page-blank`**; `content_safety_findings` + rollups written; bare `review` never triggers.
   Manifest reports `csam_documents_NOT_ingested` / `images_withheld` / `source_withheld_docs`; `--dry-run` projects it.

---

## 11. Divergences from a native sv-kb ingest (be explicit)

For a **text-layer PDF with no redactions, output is essentially indistinguishable** (same chunk assembly,
`content_sha`, `index_document`, embeddings, image-vector dedup, metadata bag). The bounded divergences:
- **★ Scanned / image-only pages (the material one):** native sv-kb transcribes each to full vision→markdown →
  text chunks + embeddings; our PDF track is **caption-only** and Stage 6 **skips no-text pages**, so those
  pages contribute little/no text chunks (cache *miss* vs native, not a match). The **media track does not fix
  this** — media images are standalone, not PDF pages. Closing it is a **PDF-track** concern (a future
  vision-transcription stage), not an `ingest` or media concern.
- **Redaction-scrubbed text:** chunks feed Stage-1 scrubbed page text (`[REDACTED]`), byte-different from raw
  `get_text` only where a redaction exists ⇒ different `content_sha` there (intentional: never leak under-bar
  content).
- **`asset_context` filled at ingest** (native leaves it for a later summarizer) — divergence in our favor.
- **Content-safety mechanism (§4a):** staged **removes** violations pre-ingest (CSAM doc not ingested;
  withheld asset → placeholder, vector absent, `review_status='withheld'`), sv-kb **gates at serving** — same
  data model, different mechanism (canonical: `sv-kb/docs/SAFETY-staged-vs-svkb.md`). Not a fidelity gap; a
  reader sees consistent rows.

---

## 12. Decisions (committed)

- **Split into S8 prepare + S9 apply (2026-06-25):** S8 emits an offline, no-creds, reviewable **apply-package**
  (apply-plan + `COPY` vector files + blob path-manifest + run manifest); S9 consumes it and is the only step
  that writes the live system (verbatim `index_document` + bulk pre-load + `azcopy`). Do NOT hand-write SQL for
  the chunk/registration writes (breaks verbatim fidelity + bloats); only the simple bulk inserts are `COPY`
  files. Blobs via a path-manifest + `azcopy` (no corpus duplication).
- `case_key='epstein'` (constant; verified from chunks' `context_prefix`); `dataset_key='DataSet-NN'`. §3a
  tenancy = **A** (require pre-created `cases`/`datasets`; `--ensure-tenancy` for upsert).
- Two tails: `embed` (text vectors) + `ingest` (live load) are shared, source-agnostic, re-runnable; PDF track
  = stage-1…6, media track = `media-stage-*` (built). Functional names, ordinals retired.
- `ingest` reuses `index_document` verbatim via **pre-load + guard provider**; makes **no** embedding calls.
- `ingest` **owns registration** (tenant/case/dataset lookup + documents/pages/asset_context).
- **Text and image embeddings are routed to distinct tables/keys/columns** (§4).
- Populate **both** typed columns and the JSONB **metadata bag**; plus `asset_context`.
- Image vectors **loaded** (Stage 4 / media-image), unified in `image_embeddings` by `content_hash`; coverage
  gaps fixed by re-running Stage 4.
- Tenant + environment chosen at `ingest`; tenancy resolution policy §3a = **(a) require pre-created**
  (implemented default, read-only fail-loud preflight); `--ensure-tenancy` → (b) upsert-on-first-sight.
- **Locate each dataset's `IMAGES` dir and walk it recursively** — `DataSet-NN/IMAGES` for DS-1..8 (no VOL),
  `DataSet-NN/VOL*/IMAGES` for DS-9..12. Do NOT hard-code `VOL*/IMAGES` (misses DS-1..8). Embed-store path is
  per-dataset config; **DS-9's store is at `VOL00009/IMAGES/.embeddings`**, all others at `DataSet-NN/.embeddings`.
  Don't restructure (§2a). All 12 datasets are Stage-7 embedded.
- **Content-safety = STAGED conformance (§4a, canonical `sv-kb/docs/SAFETY-staged-vs-svkb.md`):** CSAM-suspected
  → whole doc not ingested (preserve/report); withhold (sexual/violence/hate/self-harm/explicit/content-filter/
  unvetted, high-confidence — never bare `review`) → asset removed, all derived blobs → `content-redacted`,
  vector absent, `review_status='withheld'` (terminal), `source_withheld=true`; blank → `page-blank`. Staged
  removes; sv-kb gates at serving — same data model.

---

## 13. References

- **Reuse:** `svkb_pipeline/indexing.index_document` (chunk writes, cache check, status updates),
  `svkb_pipeline/{db,blob,blob_paths,models}.py`. **Schema:** `0001_core_tenancy.sql` (cases/datasets/RLS),
  `0002_documents.sql`, `0003_chunks_embeddings.sql`, `0009_asset_metadata_context.sql`, `0100_rls_session.sql`.
- **Producers:** `embed` (`HANDOFF-stage7-embed.md`) text vectors; Stage 4 / media-image image vectors.

**Bottom line:** resolve tenancy → register the document rows + metadata bag, upload blobs, pre-load the staged
**text** vectors (→ `embeddings`) and **image** vectors (→ `image_embeddings`) into their distinct tables, then
call `index_document` with a guard provider so it writes chunks and embeds nothing — one tenant-scoped,
idempotent, source-agnostic loader that makes our staged corpus indistinguishable from a native sv-kb ingest,
bar the documented scanned-page text gap.
