# Handoff: Stage 7 — Embed (text-chunk embedding; `src/embed`)

**Status:** **IMPLEMENTED** 2026-06-20 (`src/embed`, launcher `C:\epstein-ingest\run_embed.py`, tests
`tests/run_embed_tests.py` — all acceptance checks pass offline with an injected fake provider). Design
**committed** (this doc supersedes the "chunk + embed" wording in older specs). Stages 1–6 are **immutable**
(no code changes; re-running a stage with different env/config is allowed). DS-11 (1,252,915 child chunks)
and DS-12 are through Stage 6 and **embed-ready** (not yet embedded).

**Build notes (where the implementation makes a documented call):**
- **Store format:** 256 **append-log shard files** (`<store>/<xx>.f32v`) of fixed-size `digest(32B) +
  float32[dims]` records — NOT one file per `content_sha` and NOT whole-shard tmp+rename. Rationale: the run
  is driven from a Windows launcher over a `\\wsl.localhost` UNC path; 1.25M tiny files (or per-write shard
  rewrites) are pathologically slow there, while 256 append logs are not. Crash-safety (§4/§5) is preserved
  by truncating a partial **trailing** record on the next open/write (only the last append of a shard can be
  partial) — a crash loses at most the in-flight batch. `embed-store-manifest.json` stamps model/dims/
  api-version and the store **refuses** a mismatch.
- **Dedup memory (§5):** the heavy part — the embed **inputs** — is streamed (only a bounded window of
  batches is alive). The bounded structure we hold is the `content_sha` **digest** set (raw 32-byte `bytes`,
  ~80 MB at DS-11 scale), seeded from the on-disk store for resume. A far larger corpus would want a
  disk-backed sharded dedup; at DS-11 the in-RAM digest set is the simpler, honest choice.
- **dump_json:** Stage 7 keeps a **local** copy (`src/embed/util.py`) of stage1.walk's deterministic JSON
  writer instead of importing it, so this network-only stage does not transitively import PyMuPDF (`fitz`).
- **Env vars:** `AZURE_OPENAI_ENDPOINT`/`_API_KEY` + `AZURE_OPENAI_EMBED_DEPLOYMENT` (default
  `text-embedding-3-small`) + `AZURE_OPENAI_EMBED_API_VERSION` (default `2024-10-21`).

**Module name:** `src/embed` (functional name; the pipeline forks into PDF + media tracks after Stage 6, so
ordinal "Stage 7" is ambiguous — `embed` is the shared tail). CLI `python -m src.embed`.

**Read first:** `HANDOFF-stage8-ingest.md` (its consumer), `HANDOFF-stage6-chunk.md` (its producer), and the
**reuse sources** `~/repos/sv-kb/pipeline/shared/svkb_pipeline/{indexing.py,embeddings.py,chunking.py}`.

---

## 0. What it does (30-second version)

Stage 7 turns the staged **child chunks** (from PDF `chunks.json` and, later, media-transcript chunks) into
**text embeddings**, staged on disk as a **`content_sha`-keyed vector store** — *not* loaded to any DB (that
is Stage 8). It is the one shared, expensive, API-bound step between chunking and ingestion, deliberately
decoupled so embedding can run resumably over hours while the DB load (Stage 8) stays a fast, transactional
pass. **Text only** — image/vision embeddings are produced by Stage 4 (PDF) or the media-image stage, never
here. Append-only, idempotent, resumable, source-agnostic.

---

## 1. Core principles

- **Single responsibility: embed, don't ingest.** Stage 7 writes a local vector store and **nothing to
  Postgres/Azure Storage**. Stage 8 loads it.
- **Text only.** Embeds **child** chunks via `text-embedding-3-*`. Parents are never embedded (only
  `ChildChunk` carries a `content_sha`). Image vectors are out of scope (Stage 4 / media-image stage).
- **Source-agnostic.** Reads any `chunks.json` (PDF Stage 6 today; media-transcript chunker later) — same
  `parents_to_dicts` shape, same `content_sha` scheme — and **dedups globally across all sources**.
- **Fidelity = the embed input + the model, exactly as sv-kb's indexer.** The bar is that a `content_sha`'s
  vector is what `svkb_pipeline.indexing.index_document` would have produced (§3). The dedup/cache key is the
  `content_sha`; the vector value need not be bit-identical (Azure floats vary), and that is **moot** because
  Stage 8 inserts with `ON CONFLICT DO NOTHING` — first-writer-per-`content_sha` wins (§ ingest handoff).
- **Idempotent / resumable.** The vector store **is** the resume state: a `content_sha` already present is
  skipped. A crash mid-run loses at most an in-flight batch.

---

## 2. Inputs

- Every sibling **`<name>.chunks.json`** under the run root (PDF Stage 6 output), loaded via
  `parents_from_dicts`. Iterate **`parent.children`**; collect each child's `content_sha`,
  `context_prefix`, `text`. Empty docs (`chunks_ref:null`) contribute nothing.
- (Future) media-transcript `chunks.json` — identical handling (`chunk_kind:"transcript"` doesn't affect
  `content_sha`, which is `sha256(model\nprefix\ntext)`).
- **No PDF, no sidecar mutation, no network except the embedding endpoint.**

---

## 3. The embed recipe (the fidelity core — get these exactly right)

1. **Embed input string — mirror `indexing.py` verbatim:**
   ```python
   embed_input = f"{child.context_prefix} {child.text}"   # context_prefix + ONE space + text
   ```
   This is **not** `child.text` and **not** the `content_sha` preimage
   (`f"{model}\n{context_prefix}\n{text}"`). Embedding the wrong string silently yields vectors that don't
   match sv-kb and (combined with a correct `content_sha` key) poisons the shared cache. **Add a test** that
   recomputes `content_sha` from `(model, context_prefix, text)` and confirms it equals the chunk's stored
   `content_sha`, so the inputs are provably the ones the sha was built from.
2. **Model + dimensions — locked, stamped:** default **`text-embedding-3-small`, 1536-d** (must match the
   **target tenant's** `embeddings` rows — the model name is inside `content_sha`, and the column is
   `embedding_small vector(1536)` vs `embedding_large vector(3072)`). Reuse `embeddings.py` conventions:
   REST `…/embeddings?api-version=2024-10-21`, send `dimensions` only when ≠ 1536, batch 32.
3. **Dedup by `content_sha`, globally.** One unique `content_sha` → one `embed_input` → one vector. The same
   child text anywhere in the corpus (PDF or media) shares one embedding — this is the cost lever.

---

## 4. Output — the staged vector store (decided)

A **corpus-global, `content_sha`-keyed** store — **not** per-doc (per-doc would duplicate shared vectors and
defeat dedup):

- **Layout:** shard by `content_sha` prefix (e.g. first 2 hex chars → 256 shards) under a store root
  (default `<corpus_root>/.embeddings/` or `--store DIR`). Each shard is an append-only record of
  `content_sha → vector` (+ `embedding_model`).
- **Format: binary float32**, serialized so Stage 8 inserts the **same** value `indexing._vector_literal`
  would (`repr(float(x))`). Prefer a compact binary record (e.g. `.npy`/packed floats) over JSON
  (3–4× bloat, slow). 1536×float32 = 6 KB/vector.
- **Run manifest** `embed-run-manifest.json`: `embedding_model`, `dimensions`, `api_version`,
  `children_seen`, `unique_content_sha`, `embedded`, `cached(skipped)`, tokens, **`estimated_cost_usd`**,
  wall-clock, errors. Self-describing so a model change is detectable.

---

## 5. Dedup, resumability, scale

- ~1.25M children (DS-11) → the **unique `content_sha`** count is the real workload (and the cost). Don't
  hold all shas+inputs in RAM; use a disk-backed/sharded dedup (the store itself doubles as the seen-set).
- **Resume:** before embedding a `content_sha`, check the store; present ⇒ skip. So a re-run only embeds new
  shas; `--force` re-embeds.
- Atomic shard writes (tmp + rename) so a crash never leaves a half-written vector.

---

## 6. Throughput / client

- The **vector does not depend on the client code** (Azure computes it), so — unlike the chunker — Stage 7
  need not reuse `embeddings.py` *verbatim* for fidelity. Reuse its **URL/dims/api-version conventions**, but
  wrap a **stage3/4-style concurrency + RPM/TPM limiter** (sv-kb's built-in retry is only 4 attempts — thin
  for a multi-hour bulk run). Keys env-only (`AZURE_OPENAI_*`).

---

## 7. Architecture — mirror `src/stageN`

Package `src/embed/`: `config.py` (model/dims/api-version, concurrency, RPM/TPM, store root), `collect.py`
(walk `chunks.json` → stream children → dedup by `content_sha`), `provider.py` (the Azure embed client +
limiter), `store.py` (sharded float32 vector store; read/write/seen), `walk.py` (scheduler + manifest),
`cli.py`. Reuse `src/stage6/walk.find_pdfs_pruned` to find sidecars/`chunks.json`, `src/stage1/walk.dump_json`
for the manifest. Launcher `C:\epstein-ingest\run_embed.py` mirrors `run_stage6.py` **plus** loads
`AZURE_OPENAI_*` (this stage *does* call the network) and adds `sv-kb/pipeline/shared` to `sys.path`.

```
python -m src.embed <ROOT> [--store DIR] [--model … --dimensions 1536] [-j N] [--rpm N --tpm N]
                          [--force] [--dry-run]
python -m src.embed <ROOT> --dry-run   # unique-content_sha count + token/$ projection, NO API calls
```

---

## 8. Cost

text-embedding-3-small ≈ **$0.02 / 1M tokens**; children ≈ 125 tok. Pre-run, project
`unique_content_sha × ~125 × $0.02/1M` (the dataset-summary `--chunks` mode computes the unique count) and
**confirm model/dims before the run** — re-embedding the corpus is the expensive redo. DS-11 ceiling
(pre-dedup) ≈ 1.25M × 125 × $0.02/1M ≈ **~$3**; dedup cuts it.

---

## 9. Acceptance criteria

1. **Text/children only:** parents never embedded; image assets untouched.
2. **Embed input == `f"{context_prefix} {text}"`**; a `content_sha` round-trip test passes for sampled chunks.
3. **Model/dims locked + stamped**; the store and manifest record `embedding_model`/`dimensions`/`api_version`.
4. **Global `content_sha` dedup:** each unique sha embedded once; store is `content_sha`-keyed, float32.
5. **Idempotent/resumable:** re-run embeds only new shas; `--force` re-embeds; crash-safe shard writes.
6. **Read-only to the data tree** (writes only the vector store + manifest; never a sidecar/`chunks.json`).
7. **`--dry-run`** reports unique-sha/token/$ projection with **no API calls**.
8. **Source-agnostic:** consumes PDF and media `chunks.json` identically.

---

## 10. Decisions (committed)

- Split embed (S7) from ingest (S8); functional module names `src/embed` / `src/ingest`.
- Text/children only; image embeddings stay with Stage 4 / media-image stage.
- Embed input = `f"{context_prefix} {text}"`; model/dims = `text-embedding-3-small@1536` (match the target
  tenant; lock before running).
- Vector store = corpus-global, `content_sha`-keyed, sharded, **float32**.
- Offline dedup only (no live-DB pre-pass); Stage 8 handles DB-level dedup via `ON CONFLICT`.
- Reuse `embeddings.py` conventions, not verbatim; wrap a real rate limiter.

---

## 11. References

- **Reuse / mirror:** `svkb_pipeline/indexing.py` (the **`f"{context_prefix} {text}"`** embed input; the
  `WHERE content_sha = ANY(...) AND embedding_model` cache check; `_vector_literal`), `svkb_pipeline/
  embeddings.py` (URL/dims/api-version/batch), `svkb_pipeline/chunking.py` (`content_sha`,
  `parents_from_dicts`). Templates: `src/stage6/{walk,config,cli}.py`.
- **Consumer:** `HANDOFF-stage8-ingest.md` (pre-load → `index_document` with a guard provider).
- **Schema:** `sql/migrations/0003_chunks_embeddings.sql` (`embeddings` table: `content_sha`,
  `embedding_model`, `embedding_small/large`, `UNIQUE(tenant, content_sha, embedding_model)`).

**Bottom line:** read `chunks.json`, embed each unique child `content_sha`'s `f"{context_prefix} {text}"`
with the tenant's locked model/dims, and stage the vectors in a `content_sha`-keyed float32 store — text
only, dedup-global, resumable, no DB. Stage 8 loads it.
