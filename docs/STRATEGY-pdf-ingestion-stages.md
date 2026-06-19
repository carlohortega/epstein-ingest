# Strategy: Staged, Cost-Optimized PDF Ingestion (Stages 1–7)

**Status:** Stages 1–**4 built** (Stage 3 validated on DataSet‑12, running on DataSet‑11; **Stage 4 built +
gate-validated 2026-06-19**, all Azure services live — see the Stage‑4 handoff's "Stage 4 built" update box);
Stages 5–7 specified. See the **"Update — Stages 1–3 built"** section directly below for amendments.
**Audience:** implementers of the staging pipeline and the sv-kb ingestion (“emulator”) step.
**Companion docs:** [`HANDOFF-text-first-pdf-extraction.md`](HANDOFF-text-first-pdf-extraction.md) (Stage 1),
[`HANDOFF-stage2-render-classify-register.md`](HANDOFF-stage2-render-classify-register.md) (Stage 2),
[`HANDOFF-stage3-text-summaries.md`](HANDOFF-stage3-text-summaries.md) (Stage 3 design) +
[`HANDOFF-stage3-run-ds09-12.md`](HANDOFF-stage3-run-ds09-12.md) (Stage 3 run handoff — current 4-dataset state),
[`HANDOFF-stage4-image-vision.md`](HANDOFF-stage4-image-vision.md) (Stage 4 — built 2026-06-19).

---

## Update — Stages 1–3 built (2026-06-17)

Stages 1–3 are implemented and running on real data (Stage 3 validated on DataSet‑12; DataSet‑11 ~331k docs
in progress). This amends several assumptions below.

**Repo / paths (amends §9 and body).** The codebase moved to **`~/repos/epstein-ingest`**; the package was
renamed `text_first_extract` → **`src`**; each stage is a subpackage (`src/stage1`, `src/stage2`,
`src/stage3`); invoke `python -m src.stageN`. Where the body/§9 say `~/text-first-extract/` /
`text_first_extract`, read as the above.

**Measured outcomes (amends §2/§3).**
- **Vision fraction is lower than the §3 estimate:** DS12 ≈ **13%** `page_kind=image` (190 image + 8 mixed of
  1,525), under the "~20–30%" guess. **`redacted` page_kind effectively never fires** (DS12: 0) — redacted
  pages keep a readable footer → `text`/`mixed`.
- **Stage‑3 cost validated** on gpt‑4.1‑nano: DS12 **$0.17** (1,084 eligible pages); DS11 **~$0.26 / 1,000
  docs** (~$85 full‑corpus realtime). Cost posture holds.
- **Model deployment is auto‑detected.** Point the nano slot at a real **`gpt-4.1-nano`** deployment (chat
  shape `max_tokens`+`temperature=0`+`seed`; deterministic; ~6× cheaper than gpt‑5‑mini). The client falls
  back to the reasoning shape for a gpt‑5/o‑series deployment. (The env "nano" slot initially pointed at
  gpt‑5‑mini; corrected to gpt‑4.1‑nano.)
- A Stage‑2 `text` page may carry a **`low_quality_text`** flag (garbage‑OCR kept in the text lane); Stage 3
  **skips these by default** (`--skip-low-quality-text`).

**Azure content‑filter blocking — a safety path the strategy didn't anticipate (amends §7 + the Stage‑3 row).**
- A real fraction of genuinely‑sensitive text pages are **blocked by Azure's content‑management policy at the
  nano call itself** and never get a summary (DS12: **45/1,084 ≈ 4%**, all `sexual:high`; ~84% with minor
  indicators). Stage 3 treats the block as a **decisive `review` signal**: sets
  `text_safety={flag:"review", content_filtered:true}`, writes no `summary`, and logs file/page/timestamp/
  error to **`content-filter-failures.json`** (path via `--content-filter-log`).
- The safety triad therefore has a **4th path**: Azure‑blocked pages are the strongest signal *and* the
  CSAM/minors **quarantine bucket**. **Stage 7 must handle pages with no `summary` but
  `content_filtered=review`.**
- Recovering *adult* high‑severity pages requires the Azure **Limited Access "modified content filter"**
  approval; minors‑related content is hard‑blocked regardless. Also disable **Prompt Shields / jailbreak** on
  the deployment — it false‑positives on evidence OCR and caused the majority of the *initial* blocks until
  turned off.
- **`text_safety` is `ok | review` only — there is no `block`** (sv‑kb policy: label, do not block).

**Review‑rate is corpus‑ and model‑dependent (amends §7's "escalate only review‑flagged" premise).** DS12
flagged **57%** of summarized pages `review`; DS11/0001 only **9%**. gpt‑4.1‑nano flags `review` more
liberally than gpt‑5‑mini. Downstream Content‑Safety escalation is **not** always a tiny fraction.

**Stage 5 — scope to vision docs only (refinement).** Only documents with ≥1 `image` page (that gained a
Stage‑4 caption) need re‑summarizing. A doc whose pages are all `text`/`mixed` gained nothing new → its
Stage‑3 provisional summary is simply **promoted to final** (no nano call).

**Stage 6 — chunk with sv‑kb's chunker, not a new one (hard requirement).** Reuse
**`sv-kb/pipeline/shared/svkb_pipeline/chunking.py`** (`chunk_pages(...)`) so staged chunks are identical to
what the live system produces. It is **parent‑child**: parent target 500 / max 650 tokens (**not** embedded —
returned to the LLM at generation); child target 125 / max 150 tokens (embedded + retrieved); children packed
by sentence; `heading_path` tracked; each child stamped with a deterministic `context_prefix` +
`content_sha = sha256(embedding_model + prefix + text)` for embed‑cache/dedup. Stage 6 feeds it
`(page_number, text)` pairs plus `document_name`/`case_key`/`dataset_key`, `embedding_model=
"text-embedding-3-small"`. (This chunker emits a *template* `context_prefix`; the §8 "deferred LLM
context_prefix" is the richer future version.)

**Batch mode at back‑catalog scale needs request‑chunking (operational gap).** Azure caps a batch at ~100k
requests; DS11 ≈ 873k calls → ~9 jobs. Not yet implemented, so DS11 runs **realtime** (resumable). Add
multi‑job splitting before using `--batch` on a full dataset.

---

## Update — DataSet-09 Stage 2 + vision-routing decisions (2026-06-18)

Stage 2 ran on **DataSet-09** (528,995 PDFs, clean finish, 0 errored/0 no-sidecar). Findings below amend
§1/§2/§3/§4/§5/§6/§7 and are **requirements for downstream (Stages 4–7) builders**.

**The corpus is almost entirely scanned images + an OCR text layer.** `is_full_page_image` is **True on ~93%
of pages**, and `max_image_coverage` is a near-constant **0.9778** — i.e. *every* page is a full-page scan,
and the routing turns entirely on **text sufficiency** (OCR `char_count` vs `min_chars=40`), not on image
coverage. Do **not** use `is_full_page_image`/`max_image_coverage` as an "is this a picture" signal — they
are ~constant here.

**Measured vision fraction (DS09, from the manifest's 449,403 rendered PDFs = ~85% of corpus):** of 878,118
pages — **`image` 7.8%** (68,197), `mixed` 0.6% (5,522), `text` 91.6% (804,194), `blank` 205, `redacted` 0.
`low_quality_text` **1.8%** (15,699). `vision_workload` (image pages + kept artifacts) = **111,072**; artifacts
kept 42,875. Whole-set ≈ image ~80k, vision_workload ~130k (exact whole-set needs the §7 pruned-parallel
sidecar tally — manifest excludes the 79,579 first-batch PDFs skipped on resume).

**~63% of `image` pages are NOT useful pictures.** The `full_page_image_low_text` class (coverage == 0.9778)
is dominated by **blank scans, broken-image placeholders** (email/web→PDF with linked/missing images), and
**exhibit slip-sheets** ("Exhibit C"). A minority are real photos (some under a redaction box). **Operator
decision (2026-06-18): be OVER-inclusive — do NOT pre-filter these.** Let Stage 4 caption them; the caption
itself states "blank/no content."
- **Requirement (Stage 7):** must accept a vision caption indicating blank/no-content and **correct the
  sidecar** for that page (e.g. demote `page_kind`→`blank`, drop/flag the empty `image_asset`).
- If pre-filtering is ever wanted: within the 0.9778 class, `dark_coverage < 0.01` cleanly flags ~50% as
  blank at zero cost (do NOT apply that rule to the `<0.5`-coverage embedded-photo class — small photos read
  as low-dark).

**Over-inclusive vision routing = Option A (adopted, amends §3 routing table + §4 + §7).** Stage 4's input
set is **`image ∪ mixed ∪ low_quality_text`** (≈**10%** of pages: 7.8 + 0.6 + 1.8), **not** image-only.
Rationale: `mixed` carries real figures and `low_quality_text` is poor-OCR/handwriting that vision recovers —
both cheap to add (~+33% vision pages). `mixed` and `low_quality_text` pages still keep their canonical text;
they are *added* to the vision set, not removed from the text lane.

**Known classifier false-negative (amends §4; not fixed).** A full-page image with an **overlaid** OCR/
annotation text layer (`char_count ≥ min_chars`) matches neither the `image` rule nor `mixed` → it falls to
`text` and never reaches vision. Empirically **rare in DS09** (0 of 200 random `text` pages were photographs;
95% upper bound ≈ 1.5%). The `dark/ink_coverage` metrics are **null on `text` pages** (Stage 1 only computes
them for image candidates), so this can't be caught from metadata — only by rendering pixels. Option A's
`low_quality_text` inclusion catches the handwriting subset; the photo-with-overlay subset is accepted as a
small known miss.

**Caption AND description (amends §4/§6 schema).** Stage 4 emits BOTH per image asset in one gpt-5-mini call:
a short **`caption`** and a longer **`visual_description`**, plus safety tags + embedding. The §6 / Stage-2
§7 schemas previously named only `caption`; add **`image_assets[].description`** (a.k.a. `visual_description`).

**Staging reality — NOT 100% page renders; corpus now UNIFORM (amends §5 + Stage-2 §1/§5).** DS09 ran
`--no-stage-pages` for 449,403 of 528,995 PDFs (image pages + artifacts + thumbnails only; the first 79,579
were full-staged). Two post-passes on **2026-06-18** then made the corpus uniform (new Stage-2 CLI modes —
see Stage-2 spec / `src/stage2/escalate.py`):
- **`--escalate-vision`** rendered the **27,141 `low_quality_text` pages** (8,666 PDFs) into `images/` and
  registered them in `image_assets[]` with **`route_reason:"low_quality_text"`** — forward-filling the
  Option-A vision set so Stage 4 has every vision page materialized.
- **`--purge-staged-pages --confirm`** deleted **all `<name>/pages/`** display renders (73,964 dirs / 314,226
  PNGs / 68.6 GB reclaimed) and nulled the 302,737 dangling non-image `render_path`s.

Resulting state every downstream stage can rely on:
- **`images/` (image pages + low_quality_text) + `artifacts/` + `thumbnail.png` are fully staged** for the
  whole corpus → Stage 4 has its complete Option-A vision set. **No `pages/` exists anywhere.**
- **Stage 7 must render `text`/`mixed` display PNGs on ingest** for the *entire* corpus (none are staged);
  non-image `render_path` is `null` everywhere.

**gpt-5-mini cost notes (image line).** A full-page image bills ~**2,490 tokens** (1,536-patch cap × 1.62
multiplier). **Downsampling 200→150 DPI saves nothing** — both exceed the 1,536-patch cap and rescale to it;
you must go **below ~130 DPI** to cut tokens (legibility risk). **Batch API = 50% off.** Whole-set Option A
vision ≈ **~$130 realtime / ~$65 batched** (input-dominated; output adds on top).

---

## 0. Why this exists

We are ingesting multi-hundred-thousand–page litigation productions (Epstein DataSet‑11/12, etc.) into
sv‑kb. The two forces that shape every decision:

1. **Cost.** Generative vision (gpt‑5‑mini) is the only expensive operation at this scale. Everything
   is designed to **minimize vision calls** and push as much as possible onto free/local work,
   cheap text models (gpt‑4.1‑nano), and embeddings.
2. **Fidelity & safety.** This is evidence. Extracted text must be **verbatim** (no LLM “cleanup”),
   redactions must never leak, and image-based safety (CSAM/explicit) must be covered.

## 1. Core principles

- **Stage 1’s text is canonical.** The deterministic, verbatim reading-order text from Stage 1 is the
  source of truth that gets indexed. We do **not** let an LLM rewrite OCR into “nicer” markdown — that
  risks silently altering content, fabricating structure, or reconstructing redacted text. (See
  Stage‑1 handoff §6: honesty over polish.)
- **Stage everything locally, ingest once.** Stages 1–6 write only to the local staging tree next to
  each PDF. **No Azure / no DB writes until Stage 7**, a single specialized ingestion pass. This keeps
  the expensive/idempotent compute inspectable and resumable, and makes ingestion a clean coordinated load.
- **Minimize vision.** gpt‑5‑mini runs **only** on pages/artifacts that are *genuinely images* — and
  never on blank or fully‑redacted pages. The deterministic `page_kind` classification (below) decides this.
- **Bundle, don’t multiply, LLM calls.** One nano call per page returns summary **+** key_points **+**
  entities **+** topics **+** a text-safety flag. One vision call per image asset returns caption **+**
  visual description **+** safety tags. No separate calls for those sub-fields.
- **Safety with no dedicated per-page call.** nano text-flag (text pages) + vision tags (image assets)
  + cheap Azure Content Safety on extracted photo artifacts. That triad covers text and image risk.

## 2. Model selection & cost posture

| Work | Model | Input | Cost posture |
|---|---|---|---|
| Text extraction, render, classify, chunk | **none** (PyMuPDF / local) | PDF | ~free |
| Page & document **summaries** (+ key_points/entities/topics + text-safety flag) | **gpt‑4.1‑nano** | text | cheap; input-dominated |
| **Image** caption/description + image safety tags | **gpt‑5‑mini** (vision) | page/artifact image | **the only premium line** — minimized to true image assets |
| Text + image embeddings | text‑embedding‑3 / Azure AI Vision | text / image | cheap |
| Image content-safety on photo artifacts | Azure **Content Safety** | image | cheap; closes the CSAM gap |

**Not used / deliberately avoided:** LLM “OCR→markdown” rewrite of canonical text; a vision call on
every page; a dedicated safety call per page.

## 3. `page_kind` — the routing key (deterministic, no LLM)

Computed from Stage‑1 metrics (`max_image_coverage`, `is_full_page_image`, text sufficiency, blank/
redaction signals). Finalized at render time (Stage 2) and written to the JSON. **This single field
decides what, if anything, needs vision.** *(Amended 2026-06-18: the DS09 vision set is over-inclusive —
`image ∪ mixed ∪ low_quality_text`, not image-only. See the DataSet-09 update above.)*

| `page_kind` | Definition | Vision? | Notes |
|---|---|---|---|
| `text` | text-sufficient; little/no image coverage (a logo is fine) | no | nano summary |
| `mixed` | text-sufficient **and** a mid-size embedded figure/chart | no | nano summary; figure → image-embed only |
| `image` | one image covers ≥ `full_page_threshold` (~0.80) **and** no usable text | **yes** | the genuine “page is a photo/scan” → image asset |
| `blank` | white or black blank / slip-sheet (Stage‑1 blank gate) | no | nothing to read |
| `redacted` | bars cover ~everything; only a footer of visible text | no | fully redacted → nothing to read |

> **Routing refinement vs. Stage‑1’s original `needs_vision`:** a `redaction_suspected` page **no longer
> forces vision by itself**. Vision is driven by `page_kind = image` only. On DataSet‑11 this is the
> difference between ~73% “needs vision” (redacted emails with text) and a realistic ~20–30% true images.

## 4. The stages (end-to-end)

| Stage | Name | Model | Reads | Writes (local until 7) |
|---|---|---|---|---|
| **1** ✅ | Text-first extraction | none | PDF | `<name>.json` (verbatim text, `[REDACTED]`, metrics, routing) |
| **2** | Render + classify + register | none | PDF + S1 JSON | page PNGs, image-asset files, artifacts, thumbnail; JSON gains `page_kind`, `image_assets[]` |
| **3** | Text summaries | gpt‑4.1‑nano | page text | JSON gains per-page `summary`/`summary_json` + `text_safety`; **provisional** doc summary |
| **4** | Image-asset processing | gpt‑5‑mini + AI Vision + Content Safety | image assets + artifacts (`image ∪ mixed ∪ low_quality_text`, per 2026-06-18 update) | JSON gains image **captions + descriptions**, image **safety**, image **embeddings**; (skips blank/redacted) |
| **5** | Finalize doc summary | gpt‑4.1‑nano | page text + image captions | JSON gains **final** document summary |
| **6** | Chunk + embed (staging) | text‑embedding‑3 | final text + captions | staged chunk + vector files |
| **7** | **Specialized ingestion → system** | (load) | all staged artifacts | **Azure Storage uploads + Postgres writes + events** |

Stages 1–6 are local staging; **Stage 7 is the only step that writes to Azure Storage or the database.**

## 5. Directory & artifact layout (per PDF)

```
<dir>/<name>.pdf
<dir>/<name>.json                 # the evolving sidecar (extended each stage)
<dir>/<name>/
    images/      page-<NNNN>.png            # vision assets: page_kind=image + escalated low_quality_text pages
    artifacts/   page-<NNNN>-img-<MM>.png   # embedded sub-image photos
    pages/       page-<NNNN>.png            # display renders for text/mixed pages — DEFERRED to Stage 7 by
    #                                         default; PURGED entirely on DS09 (2026-06-18). See DS09 update.
    thumbnail.png                           # ONE per PDF asset (no per-page thumbnails)
```
- `NNNN` = 1-based zero-padded page number; `MM` = zero-padded image index on that page.
- Thumbnails are **per asset** (PDF, audio, video) — never per page.
- The JSON `image_assets[]` is the manifest of everything that becomes a standalone image asset
  (image-pages + photo artifacts + **escalated `low_quality_text` pages**, the last tagged
  `route_reason:"low_quality_text"`), each with a **relative path**.

## 6. Sidecar JSON evolution

The Stage‑1 sidecar is **extended in place** by each stage (never rewritten destructively):

- **Stage 1:** `pages[].text.content` (verbatim, `[REDACTED]`), `metrics`, `redaction`, routing.
- **Stage 2:** `pages[].page_kind`, `pages[].render_path`, top-level `image_assets[]`, `thumbnail_path`.
- **Stage 3:** `pages[].summary` + `summary_json` (text/mixed only) + `text_safety`; `document_summary` (provisional).
- **Stage 4:** `image_assets[].caption` + **`description`** (visual_description) + `safety` + `embedding_ref`.
- **Stage 5:** `document_summary` finalized (covers all pages incl. vision captions).
- **Stage 6:** `chunks[]` + embedding references (or a sibling staged file).

Stage 7 maps these onto sv‑kb tables: `documents`, `document_pages` (`summary`/`summary_json`,
`vision_md_blob_path`, `safety_tags`), `asset_context` (`summary_text`/`summary_json`, model =
`gpt-4.1-nano`), `chunks`, `embeddings`, `image_embeddings`, `content_safety_findings`.

## 7. Safety architecture (no dedicated per-page safety call)

- **Text pages:** nano returns a `text_safety` triage flag in the Stage‑3 summary call (free-riding).
  Optionally escalate only `review`-flagged pages to a precise Content Safety *text* check.
- **Image assets (pages + artifacts):** gpt‑5‑mini returns explicit/illegal-activity tags in Stage 4;
  **and** a cheap Azure Content Safety scan runs on extracted photo artifacts — the **CSAM net** for
  photos embedded inside otherwise-textual pages. **Non-negotiable in this domain.**
- **Blank / fully-redacted pages:** no readable content → no safety scan needed.

## 8. Deferred (Phase 2+ — all nano/text, not now)

GraphRAG entities+relations, RAPTOR hierarchical summaries, LLM-generated `context_prefix` (Contextual
Retrieval — the bigger *search-quality* lever if/when prioritized), semantic/heading-aware chunking,
auto-title when client metadata is absent. Schema exists for most (see Stage‑2 handoff references).

## 9. Operational notes

- The Stage‑1 tool lives at `~/text-first-extract/` (standalone; PyMuPDF + stdlib). Stages 2–6 should
  extend that codebase (it already renders pages and computes the image/coverage metrics).
- Runs are **idempotent and resumable** per stage (skip work whose output already exists). At
  hundreds of thousands of files this is mandatory; the dataset may still be growing during a run.
- Determinism: same input ⇒ same staged output (modulo a caller-supplied timestamp and the
  non-deterministic LLM fields, which are stamped with model + version).
