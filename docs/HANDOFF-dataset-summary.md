# Handoff: Dataset Summary — cross-stage progress, cost & metadata dashboard (read-only)

**Status:** **IMPLEMENTED** 2026-06-20 (`src/summary`, launcher `C:\epstein-ingest\run_summary.py`, tests
`tests/run_summary_tests.py` — all checks pass; validated on the live corpus, 1.36M docs cold ≈ 24 min,
warm ≈ 2.4 min). A **read-only scanner** that pruned-walks the sidecars and rolls up an at-a-glance dashboard
of **progress, cost, volume, safety, routing, vision-embedding coverage, and S7/S8** across every dataset —
as **JSON + Markdown + a self-contained HTML dashboard**.

**Build notes (where the implementation makes a documented call):**
- **Self-contained / pure stdlib:** does NOT import `src.stage1` (`import fitz`) or `src.stage6` (imports
  sv-kb at load) — the three helpers needed (atomic `dump_json`, the pruned walk, dataset/case derivation)
  are reimplemented in `src/summary/util.py`/`walk.py` so process-pool workers never drag in PyMuPDF/sv-kb.
- **Walk:** a bespoke `os.scandir` pruned walk (not `find_pdfs_pruned`) that harvests `(mtime,size)` for free
  (cached in the Windows `DirEntry`), so the incremental refresh is a stat-only sweep.
- **Incremental cache** keyed `(path,mtime,size)` at `<dataset_root>/.summary-cache.json`, versioned by
  `SCHEMA_VERSION`/`CACHE_VERSION` (a bump discards it). Sound because sidecars are append-only + atomically
  replaced. Warm re-scan ≈ 10× faster.
- **S7 embed coverage:** store-aware. Reads Stage-7's `embed-store-manifest.json` + the `.f32v` seen-set
  (`src/summary/embed_store.py`, mirrors `src/embed/store.py`'s format without importing it); with `--chunks`
  it cross-checks each doc's child `content_sha`s for exact per-doc coverage. S7 isn't a sidecar block.
- **S8 ingest:** live-DB only, so offline it renders **n/a** with a note (no false "not started").
- **Vision-embedding coverage** uses the per-asset `embedding_ref` (null ⇒ not embedded) ground truth — NOT
  the `stage4.counts.embedded` tally — so a "Stage-4 ran without AI-Vision env" gap is caught and called out.
- **Cost projected-remaining** sums per-dataset estimates (corpus-wide re-averaging hid DS-09's backlog).
- **Console note:** CLI/test prints use unicode; set `PYTHONIOENCODING=utf-8` on Windows. Report FILES are
  always UTF-8.

_Original requirements (still accurate) follow._ Stages 1–7 are built (S7 = `src/embed`); DS-11/DS-12 are
through Stage 6, DS-09/DS-10 mid-pipeline; no embed (S7) run executed yet.

**Read first:** the per-stage handoffs (`HANDOFF-stage1..6`) for the exact fields each stage writes, and
`STRATEGY-pdf-ingestion-stages.md` (§6 sidecar evolution). This module **reads** every block those stages
emit; it writes **nothing** into the data tree except its own report files. Mirror the `src/stageN/` shape
(walk / config / cli) — but there is **no LLM, no Azure, no sv-kb dependency**: pure stdlib over local JSON.

---

## 0. What it does (30-second version)

The pipeline scatters its state across **one `<name>.json` sidecar per PDF** (plus a sibling
`<name>.chunks.json` after Stage 6), each accreting a per-stage block (`stage2`…`stage6`,
`document_summary`, `document_summary_final`, `image_assets[]`). Today the only way to answer "how far is
DS-09 through Stage 3? what has the corpus cost so far? how many docs are quarantined for safety? how many
chunks exist?" is to hand-grep sidecars (as we did this session). **The summary module makes that a single
command.** It does a **pruned, parallel, read-only scan** of a dataset (or all of them), extracts a compact
per-doc digest from each sidecar, and aggregates into **per-dataset and corpus-wide reports** —
`dataset-summary.json` (canonical) + a human Markdown table/dashboard. It is the **source of truth for
progress** because it derives state from the **per-sidecar blocks**, not from the unreliable, inconsistently
located run-manifests. Idempotent, resumable, incrementally cached, and honest about partial/sampled data.

---

## 1. Core principles (requirements, not style)

- **Read-only / never mutate.** The scanner opens sidecars `"r"` and **never writes back**. Its only writes
  are the report artifacts, and those go to a **reports location**, never on top of a sidecar (acceptance
  §10.1 verifies sidecar bytes are unchanged). It must be safe to run **while a stage is actively writing**
  the same tree (e.g. mid-Stage-3 on DS-09) — so tolerate half-written/locked/just-renamed files (skip +
  count, never crash).
- **Source of truth = per-sidecar blocks, NOT manifests.** Run-manifests are partial, can be stale, and sit
  in different places per dataset (DS-12's `stage4-run-manifest.json` is at the dataset root, not under
  `IMAGES`; DS-11 has several `stage2-*-manifest.json` variants; an in-flight run has **no** final
  manifest). Derive every progress/cost number from the **blocks actually present in sidecars**. Manifests
  are an **optional cross-check / throughput hint only** (§7).
- **Pruned walk — never enumerate the render trees.** The staged PNGs live under per-doc `<stem>/` dirs and
  `images`/`pages`/`artifacts` subdirs; DS-09 alone has **528,740** such `<stem>/` dirs. A naïve recursive
  walk would enumerate millions of PNGs and take forever. **Reuse `find_pdfs_pruned`** (it prunes
  `images`/`pages`/`artifacts` and any `<stem>/` dir that has a sibling `<stem>.pdf`) — the same walk Stage
  4/5/6 use.
- **Scale-first.** ~1.4M sidecars across the corpus. Parse in a **process pool** (JSON parse is CPU-bound),
  stream a bounded window (never buffer 1.4M parsed docs), and keep an **incremental cache** keyed by
  `(path, mtime, size)` so a re-scan only re-reads changed sidecars (a refresh after a stage advances should
  be seconds, not minutes).
- **Honest about partial / sampled.** A dataset mid-stage is **"in progress (N%)"**, never silently rounded
  to done or not-started. A `--sample` fast mode must **state what it sampled and that the numbers are
  extrapolated** (no silent caps — a truncated scan that reads as "complete" is a bug).
- **Extensible by construction.** The user explicitly wants room to grow ("I haven't thought through all the
  possibilities"). Structure metrics as a **registry of per-stage collectors** (§5) so adding a new metric,
  a Stage 7, or a new rollup is a small, local change — not a rewrite. Stamp a `schema_version` on the
  report.

---

## 2. Inputs — the sidecar, block by block (what to read; all verified against real data)

The scanner reads each `<name>.json` and pulls a compact digest. Below is the **field catalog per stage** —
the raw material for every rollup in §3. Presence of a block is the **done-signal** for that stage; the
counts/usage inside it are the metrics. (Fields confirmed against DS-11/DS-12 sidecars this session.)

**Base / Stage 1 (always present when `status=="ok"`):**
- `status` (`ok`|`error`), `error.code` (`encrypted`|`corrupt`|…) — the top-level health gate.
- `source`: `filename`, `relative_path`, `bytes`, `sha256`, `page_count`, `pdf_producer`.
- `document.source_kind` / `content_type`.
- `extraction_summary`: `total_text_chars`, `visible_chars`, `invisible_chars`, `scrubbed_chars`,
  `pages_text_sufficient`, `pages_need_vision`, `pages_redaction_flagged`, `pages_blank`,
  `text_quality.pass`, `routing_recommendation` (`text`|`mixed`|`vision`).
- `pages[]`: `page_kind`, `needs_vision`(+`needs_vision_reason`), `text.char_count` /
  `visible_chars`/`invisible_chars`, `redaction.{suspected,scrubbed_char_count,redaction_runs}`, `flags[]`
  (`image_safety_required`, `heavy_invisible_layer`, `blank_page`…), `text_safety.flag` (`ok`|`review`).
- `image_assets[]` (registered pre-Stage-4; see Stage 4 for the enriched fields).

**Stage 2** — `stage2.counts`: `pages_rendered`, `text_pages`, `image_pages`, `mixed_pages`, `blank_pages`,
`redacted_pages`, `text_quality_suspect`, `artifacts`, `artifacts_skipped`, `artifacts_full_page_skipped`,
`vision_workload`.

**Stage 3** (text lane) — presence ⇒ S3 done for the doc.
- `stage3.counts`: `pages_summarized`, `pages_skipped`, `pages_errored`, `low_confidence`, `safety_review`,
  `content_filtered`, `doc_summary_calls`.
- `stage3.usage`: `prompt_tokens`, `completion_tokens`, `calls`, **`estimated_cost_usd`**, `batch`.
- `stage3.{model,deployment,prompt_version}`; `document_summary` (provisional): `covered_pages`,
  `safety_rollup.{review,pages_flagged}`, `input/output_token_count`, `reduce_depth`.

**Stage 4** (vision lane; **only docs with non-empty `image_assets`**) — presence ⇒ S4 done for a vision doc.
- `stage4.counts`: `assets`, `captioned`, `pregated_blank`, `has_visual_content_true`, `content_filtered`,
  `safety_review`, `embedded`, `embed_errors`, `content_safety_scanned`, `content_safety_review`, `errored`.
- `stage4.usage`: tokens + **`estimated_cost_usd`** (vision is the cost driver — gpt-5-mini).
- enriched `image_assets[]`: `kind` (`page`|`artifact`), `has_visual_content`, `content_class`
  (`blank`|…), `caption`/`description`, `safety.{flag,categories,minor_indicator,explicit}`,
  `verdict_source` (`pregate`|`vision`), `embedding_ref` (null ⇒ not embedded), `vision_confidence`.

**Stage 5** (finalize) — presence ⇒ S5 done.
- `stage5.usage` (0 tokens for promote/no-content routes), `stage5.consumed.{stage3,stage4 versions}`.
- `document_summary_final`: `source` (`promoted`|`fused`|`image_only`|`no_content`), `no_content`,
  `covered_text_pages`, `covered_vision_assets`, `safety.{flag,content_filtered}`.

**Stage 6** (chunks) — presence ⇒ S6 done.
- `stage6.counts`: `pages_chunked`, `parents`, `children`, `skipped_pages`.
- `stage6.{chunks_ref (null ⇒ empty doc), chunker_params, embedding_model, text_source,
  image_page_policy, identifiers}`.
- sibling **`<name>.chunks.json`** — read **only in an opt-in heavy mode** (§3 chunk-level: `content_sha`
  dedup ratio, token estimates); for the default scan, `stage6.counts` is enough and avoids 1.4M extra reads.

> **Dataset layout variants the walk must handle (mapped this session):**
> - **DS-09** — *flat*: `VOL00009/IMAGES/*.pdf` + `*.json` directly, with per-doc `<stem>/` render dirs
>   (528,740 of them) **and** sibling `CORRUPTED/ DATA/ DB-EXTRACT/ EMPTY/ NATIVES/` dirs to ignore.
> - **DS-10/11** — *numbered*: `VOLxxxxx/IMAGES/0001..NNNN/` (~1,000 docs each; 504 / 332 folders).
> - **DS-12** — single folder `VOL00012/IMAGES/0001` (152). Manifest at the **dataset root**.
> The sidecar is always `<stem>.json` beside `<stem>.pdf`; chunks at `<stem>.chunks.json`. Identify a
> dataset by its `DataSet-NN` dir and case by `VOLxxxxx` (same derivation Stage 6 uses).

---

## 3. The metric catalog — what the dashboard reports (rich, non-exhaustive)

Aggregations the module computes **per dataset** and **rolled up corpus-wide**. This is a starting catalog —
the registry (§5) is meant to grow.

**Progress (the headline status grid):** for each stage, `docs_total`, `docs_done` (block present),
`docs_applicable` (see §7 — S4 is vision-gated), `pct_complete = done/applicable`, a
`state ∈ {done, in_progress, not_started, n/a}`, `errored`, and `last_activity_utc` (max `generated_at`
seen, for "is a run live?"). Renders as the **S1…S7 table** (the style already in the README).

**Volume / content:** `docs`, `pages` (Σ `source.page_count`), `bytes` (Σ `source.bytes`), **text chars**
(Σ `extraction_summary.total_text_chars`, plus visible/invisible/scrubbed splits),
`pages_text_sufficient` vs `pages_need_vision` (the cost-routing split), `blank/redacted` page counts.

**Cost (Σ of the block-stamped `estimated_cost_usd` — authoritative, already reflects each run's rates):**
`stage3_usd` + `stage4_usd` + `stage5_usd` (+ `stage6_usd = 0`, local). Break out tokens
(prompt/completion/cached) and `calls` per stage. **Projected remaining $**: per-doc average cost × docs
not-yet-done (clearly labeled an estimate). This is how you answer "what has DS-09 cost so far, and what
will finishing it cost?"

**Safety (high-value):** pages with `text_safety.flag=="review"`; docs with any `content_filtered`
(quarantine candidates); image assets with `safety.minor_indicator` / `safety.explicit`;
`content_safety_review` count; `document_summary_final.safety` rollup distribution. A **quarantine list**
(doc paths) is a likely must-have output.

**Routing / mix (where the corpus's nature shows):** Stage 1 `routing_recommendation` distribution; doc-level
`needs_vision` rate; Stage 5 `source` mix (`promoted`/`fused`/`image_only`/`no_content`); Stage 4
`has_visual_content_true` vs `pregated_blank` (how much vision spend the pre-gate saved).

**Chunks (Stage 6):** Σ `parents`, Σ `children`, `pages_chunked` vs `skipped_pages`, avg parents/doc &
children/doc, count of **empty docs** (`chunks_ref==null`). Heavy mode (opt-in, reads `chunks.json`):
unique-`content_sha` ratio = the **embedding-cache/dedup win** Stage 7 will get, and Σ child-token estimate
= a **Stage-7 embedding-cost projection** (≈ tok × `$0.02`/1M for text-embedding-3-small).

**Redaction (the #1 Stage-1 concern):** Σ `pages_redaction_flagged`, Σ `scrubbed_chars`, docs with any
`redaction.suspected`.

**Health:** `error` sidecars by `error.code`; PDFs with **no sidecar** (Stage 1 not run); sidecars that
fail to parse (corruption) — each a counted category, never a crash.

---

## 4. Output — the report artifacts

Write to a **reports location** (default `<dataset_root>/dataset-summary.json` per dataset + a corpus
`<corpus_root>/corpus-summary.json`; or a central `--out` dir). Never touch sidecars. Use Stage 1's atomic
`dump_json`.

- **Canonical JSON** (`dataset-summary.json`): `schema_version`, `generated_at_utc`, `root`, `scan`
  (`mode: full|sample`, `docs_scanned`, `sampled_pct`, `wall_clock_s`, `cache_hits`), then a `stages` block
  (per-stage progress + counts + usage) and `totals`/`safety`/`routing`/`chunks`/`cost` sections from §3.
- **Human Markdown** (`dataset-summary.md`): the S1…S7 status grid + a per-dataset detail card (volume,
  cost, safety, routing, chunks). This is "the dashboard" for a terminal/PR.
- **Optional CSV** (`--csv`): one row per doc with the digest fields — for ad-hoc slicing in xlsx/pandas.
- **Quarantine list** (`--safety-list`): doc paths flagged review/`content_filtered`/minor/explicit.

Corpus rollup = the same shape summed across datasets, plus the cross-dataset status grid. Deterministic
output (stable key order, sorted lists) so reports diff cleanly over time.

---

## 5. Architecture — mirror `src/stageN`, but read-only

Package **`src/summary/`** (name TBD — §11), CLI `python -m src.summary`. No `connection.py`/client/
ratelimit (no network); no `prompts.py` (no LLM); no sv-kb dep.

| Need | Reuse / pattern | Specifics |
|---|---|---|
| Pruned PDF walk | `src/stage6/walk.find_pdfs_pruned` | the **only** safe way to enumerate; handles all 4 layouts |
| Atomic report write | `src/stage1.walk.dump_json` | reports only; never a sidecar |
| Identifiers | `src/stage6.chunk.derive_identifiers` | dataset/case keys from the path |
| Scan engine | new `collect.py` | process pool parses sidecars → per-doc **digest** (small dict); streamed |
| Per-stage metrics | new `collectors.py` | a **registry**: `{stage: fn(sidecar)->partial_digest}` — extensible |
| Aggregation | new `aggregate.py` | fold digests → dataset/corpus rollups (the §3 catalog) |
| Incremental cache | new (`.summary-cache.json`) | key `(path,mtime,size)`→digest; only re-parse changed sidecars |
| Render | new `report.py` | JSON + Markdown (+ CSV) emitters |
| CLI / config | `cli.py` / `config.py` | flags below; every knob echoed into the report for reproducibility |

**Scan engine:** `find_pdfs_pruned(root)` → submit sidecar paths to a `ProcessPoolExecutor`; each worker
`json.load`s the sidecar, runs the collector registry to build a **compact digest** (don't return the whole
sidecar — return only the extracted numbers), returns it; the main process folds digests into running
totals with a bounded in-flight window. A missing/parse-failing sidecar yields an `error`/`missing` digest,
never raises. **Incremental:** before parsing, check the cache by `(path,mtime,size)`; on hit, reuse the
cached digest. This makes "refresh the dashboard" cheap even at 1.4M files.

**Launcher** `C:\epstein-ingest\run_summary.py` mirrors `run_stage6.py` (puts the WSL repo on `sys.path`) but
needs **no `AZURE_*` and no sv-kb path** — pure stdlib. Either Python works (WSL `.venv` or native anaconda).

---

## 6. Performance & scale

- ~1.4M sidecars corpus-wide. Target: **full cold scan in single-digit minutes**, **incremental re-scan in
  seconds**. The lever is the pruned walk (§1) + process-pool parse + the mtime cache.
- **Do not** parse `chunks.json` in the default scan (that's another ~1.4M reads); gate chunk-level metrics
  (§3 heavy mode) behind `--chunks`.
- **`--sample K`** mode: scan the first K docs per folder (or every Nth) for a fast estimate; the report
  **must** carry `mode:"sample"`, `sampled_pct`, and label extrapolated totals as estimates.
- DS-09 is the stress test: 528,740 flat docs in one `IMAGES` dir **plus** 528,740 `<stem>/` dirs — confirm
  the prune keeps the walk to the `*.pdf`/`*.json` files and never descends the render dirs.

---

## 7. Stage-state logic (the subtle part — get this right)

Per-doc, per-stage **applicability** drives a correct `pct_complete` (presence alone is misleading):
- **S1/S2:** apply to every PDF; done if the block/sidecar is present (S1 = `status` set).
- **S3:** applies to every `status=="ok"` doc; done if `stage3` present.
- **S4 (vision-gated):** applies **only to docs with non-empty `image_assets`**; for text-only docs S4 is
  **n/a** (they never get a `stage4` block — by design). So `pct = stage4_present / docs_with_image_assets`.
  **Do not** compute S4% over all docs (you'll under-report badly — most docs are text-only). Treat
  `pregated_blank` assets as *processed*, not pending.
- **S5:** applies to every `status=="ok"` doc that has `stage3` (and `stage4` if it's a vision doc); done if
  `stage5` present.
- **S6:** applies to every `status=="ok"` doc with pages; done if `stage6` present (empty docs count as done,
  `chunks_ref==null`).
- **S7 (embed):** applies to every doc with ≥1 child chunk; done when all its child `content_sha`s are
  present in the staged vector store (`--chunks`/store-aware mode). Also report `has_visual_content_true` vs
  `embedded` (Stage-4 image-vector coverage) so a missing-AI-Vision-env gap is visible (→ re-run Stage 4).
- **S8 (ingest):** done when the doc's live rows exist (`documents.status=='complete'`) or a staged
  ingest-receipt is present — DB cross-check, optional/online only.

> **Source-kind aware:** scan **both** the PDF track and the media track, group every rollup by `source_kind`,
> and report the shared **embed (S7)** and **ingest (S8)** coverage per kind. The stage grid is S1…S8 (PDF) /
> M1…Mk + S7/S8 (media); the embed/ingest tail is shared.

Dataset-level `state`: `not_started` (0 done), `in_progress` (0 < done < applicable), `done` (done ==
applicable). **In-progress detection matches what we found by hand this session** (DS-09 S3 head-done /
tail-empty; DS-10 S4 early-folders-only) — the module formalizes it. Cross-check with the run-manifest's
`generated_at` / counts when present, but the **blocks win** on disagreement (manifests can be stale/partial).

---

## 8. CLI / operations

```
python -m src.summary [ROOTS...] [--all] [--out DIR] [--format json|md|csv|all]
                      [--sample K] [--chunks] [--safety-list] [--no-cache] [--rebuild-cache]
                      [--datasets DataSet-09,DataSet-11] [--generated-at ISO8601]
```
- `ROOTS`/`--all`: one or more dataset roots, or auto-discover `DataSet-*` under the corpus root.
- `--chunks`: opt into reading `chunks.json` for dedup/token projections (slower).
- `--sample K`: fast estimate (labeled).
- Default: full scan, JSON + Markdown, incremental cache on. Single-shot; a `--watch` live-refresh mode is a
  possible v2 (§11). Run posture mirrors the stages: foreground for one dataset; detached + logged for an
  all-corpus cold scan.

---

## 9. Acceptance criteria

1. **Read-only:** a scan leaves every sidecar **byte-identical** (verify a hash before/after); writes only
   the report files (to the reports location), never a sidecar/chunks file.
2. **Correct progress:** per-stage `done/in_progress/not_started/n/a` matches a hand-count on a fixture,
   **including S4 vision-gated applicability** (text-only docs are n/a, not pending).
3. **Cost rollup == Σ block `estimated_cost_usd`** across stage3/4/5 (and 0 for stage6); tokens/calls sum
   correctly.
4. **Safety rollups** (review pages, content_filtered docs, minor/explicit assets) match the source blocks;
   the quarantine list is complete.
5. **All four layouts** scan correctly (flat DS-09 + numbered DS-10/11/12) via the pruned walk; the render
   trees are never enumerated.
6. **Scale/robustness:** full corpus scan in minutes; incremental re-scan in seconds; a corrupt/half-written/
   missing sidecar is isolated + counted, never aborts the scan (safe to run **during** an active stage).
7. **Honest sampling:** `--sample` output is labeled `mode:"sample"` with `sampled_pct` and estimate flags;
   no silent truncation.
8. **Deterministic, reproducible:** stable ordering; config echoed into the report; re-run on an unchanged
   tree reproduces the report (modulo timestamp).
9. **Extensible:** adding a new metric or a Stage 7 collector is a localized change in the registry, with a
   bumped report `schema_version`.

---

## 10. Open questions / decisions

- **Package name** — `src/summary` vs `src/report` / `src/dashboard` / `src/stats` / `src/audit`. Recommend
  **`src/summary`** (matches the user's "dataset-summary"); confirm before building.
- **Report location** — per-dataset-root + corpus-root (recommended, lives with the data) vs a central
  `reports/` dir vs both via `--out`. Decide the default.
- **Incremental cache** — keyed by `(path,mtime,size)`; where to store it (`.summary-cache.json` at each
  dataset root, gitignored) and how to invalidate on schema-version bump. Confirm mtime is reliable over the
  Windows/WSL boundary (if not, fall back to size+sha-of-head).
- **"% complete" for vision-gated S4** — confirm the applicability denominator is `docs_with_image_assets`
  and that `pregated_blank` counts as processed (§7).
- **Chunk-level metrics** — default off (`--chunks` opt-in) to avoid 1.4M extra reads; confirm the
  dedup-ratio / Stage-7 cost projection are worth the heavy pass when asked.
- **Cost model** — trust the **block-stamped** `estimated_cost_usd` (recommended; it already encodes each
  run's configured rates) vs recompute from tokens × current rates. Decide; if recompute, where rates live.
- **Live mode** — one-shot now; a `--watch`/served-HTML dashboard later. Scope v1 = JSON + Markdown.
- **Multi-volume / future datasets** — all current datasets are single-`VOL`, but design the grouping for
  multiple `VOLxxxxx` per `DataSet-NN`.
- **Cross-dataset dedup** — same `content_sha`/`sha256` across datasets (corpus-wide dedup insight) is a
  tempting future rollup; out of scope for v1, but keep digests carrying the hashes so it's cheap to add.
- **Manifests as input** — use them only for `last_run`/throughput hints, or ignore entirely? Recommend
  read-if-present, blocks-win.

---

## 11. References

- **Sidecar schema (what to read):** `HANDOFF-stage1..6` (field-by-field), and real exemplars under
  `C:\Projects\Data\epstein\DataSet-12\VOL00012\IMAGES\0001\*.json` (text doc) and DS-11 (vision doc with a
  populated `stage4` + `image_assets[]`).
- **Reuse verbatim:** `src/stage6/walk.find_pdfs_pruned` (pruned walk), `src/stage1/walk.dump_json` (atomic
  write), `src/stage6/chunk.derive_identifiers` (dataset/case keys).
- **Templates to mirror:** `src/stage6/{__init__,config,walk,cli,__main__}.py` (the chunk-only, no-Azure
  stage is the closest shape) and the per-stage handoffs.
- **Manifests (cross-check only):** `extract-run-manifest.json`, `stage{3,4,5,6}-run-manifest.json`,
  `content-filter-failures*.json` — locations vary per dataset; never gate on them.

**Bottom line:** a read-only, scale-aware sibling module that pruned-walks the sidecars, extracts a compact
per-doc digest via an extensible collector registry, and rolls it up into per-dataset + corpus
**progress / cost / volume / safety / routing / chunk** reports (JSON + Markdown). Derive state from the
**blocks** (not manifests), get the **S4 vision-gating** right, keep it **incremental** so refreshes are
cheap, and **never mutate** the tree. Build the status grid first (it's the most-asked question), then layer
on cost and safety — and leave the registry open, because the dashboard's job is to keep growing.
