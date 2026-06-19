# Handoff: Stage 2 — Render pages, classify `page_kind`, register image assets

**Status:** requirements / handoff (no implementation yet).
**Goal:** for each PDF already processed by Stage 1, **render its pages to images locally**, **deterministically
classify each page** as text / image / mixed / blank / redacted, **extract embedded photo artifacts**, generate a
**single per-PDF thumbnail**, and **extend the sidecar JSON** with a manifest of image assets — all **on local
disk, no Azure / no DB**. This produces the staged inputs that Stage 4 (vision) and Stage 7 (ingestion) consume.

**Context:** companion to [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md) and
[`HANDOFF-text-first-pdf-extraction.md`](HANDOFF-text-first-pdf-extraction.md). Implement in the existing
`~/text-first-extract/` codebase (it already renders pages and computes the coverage/text metrics this stage needs).

---

## Update — built & run; DataSet-09 reality (2026-06-18)

Stage 2 is **implemented** (`src/stage2`) and has run on DataSet‑09 (528,995 PDFs, 0 errored). This amends
several "requirements" below to match how it actually behaves and what this corpus looks like:

- **"Render *every* page" (§1/§5) is now optional.** A `--no-stage-pages` mode renders **only `image` pages**
  (+ artifacts + thumbnails) and **defers `text`/`mixed`/`blank`/`redacted` renders to Stage 7**. DS09 ran
  `--no-stage-pages` for **449,403 of 528,995** PDFs. **After the post-passes below the corpus is UNIFORM: no
  `<name>/pages/` exists anywhere, `render_path` is `null` for every non-image page, and Stage 7 renders the
  display PNGs on ingest for the whole corpus.** `images/`, `artifacts/`, `thumbnail.png` are always staged.
- **Two new idempotent post-pass CLI modes (`src/stage2/escalate.py`):**
  - **`--escalate-vision`** renders `low_quality_text` pages to `<name>/images/` and registers them in
    `image_assets[]` as `kind:"page", route_reason:"low_quality_text"` — forward-fills the over-inclusive
    vision set after a `--no-stage-pages` run. DS09: 27,141 pages / 8,666 PDFs; `stage2-escalate-manifest.json`.
  - **`--purge-staged-pages [--confirm]`** deletes the deferred `<name>/pages/` display renders (dry-run
    unless `--confirm`) and nulls dangling non-image `render_path`/`render_dpi`. DS09: 73,964 dirs /
    314,226 PNGs / 68.6 GB; `stage2-purge-pages-manifest.json`. Never touches `images/`/`artifacts/`/`thumbnail.png`.
- **The corpus is ~all scanned image + OCR text layer:** `is_full_page_image` True on ~93% of pages,
  `max_image_coverage` ≈ constant **0.9778**. Classification turns on **text sufficiency only** — coverage/
  `is_full_page_image` are NOT discriminating here.
- **`image` is ~7.8% of pages**, but **~63% of `image` pages are blank/broken/slip-sheet** (the
  `full_page_image_low_text`/0.9778 class). Operator decision: **stay over-inclusive, don't pre-filter**; let
  Stage 4 caption them and Stage 7 correct the sidecar from the caption.
- **Vision routing is over-inclusive (Option A):** Stage 4 set = **`image ∪ mixed ∪ low_quality_text`**
  (≈10% of pages), not image-only — amends §4's "only `image` … require vision" and §9 acceptance #3.
- **Caption AND description:** Stage 4 stores both `image_assets[].caption` and `image_assets[].description`.
- Full detail + cost/DPI notes live in the STRATEGY doc's **"Update — DataSet-09 Stage 2"** section.

---

## 1. Scope

**In scope:**
- Render **every** page of each PDF to PNG, on local disk.
- Compute and record a deterministic **`page_kind`** per page.
- Write **image-page** renders (page_kind = image) to `<name>/images/`; other renders to `<name>/pages/`.
- Extract **embedded raster images** (photo artifacts) to `<name>/artifacts/`, deduped by content hash.
- Generate **one thumbnail per PDF** (`<name>/thumbnail.png`).
- Extend the sidecar JSON: per-page `page_kind` + `render_path`, top-level `image_assets[]` + `thumbnail_path`.
- 100% local, idempotent, resumable.

**Out of scope (later stages):** any Azure Storage upload or DB write (Stage 7); any LLM call — no summaries
(Stage 3), no captions/safety (Stage 4); chunking/embedding (Stage 6).

---

## 2. Inputs

- The PDF `<dir>/<name>.pdf`.
- Its Stage‑1 sidecar `<dir>/<name>.json` (status `ok`). Stage 2 **reads** the per-page `metrics`
  (`max_image_coverage`, `is_full_page_image`, `embedded_image_count`, `text_layer_type`,
  `ink_coverage`/`dark_coverage`), `text.char_count`, `redaction`, and `needs_vision_reason`.
- Config (thresholds in §4/§5), recorded into the JSON like Stage 1.

Skip a PDF whose Stage‑1 sidecar is missing or `status != ok` (nothing to render-classify against;
record it in the run manifest).

---

## 3. Directory layout & file naming (exact)

```
<dir>/<name>.pdf
<dir>/<name>.json
<dir>/<name>/
    images/      page-<NNNN>.png            # page_kind = image  (image-asset renders)
    artifacts/   page-<NNNN>-img-<MM>.png   # embedded photo artifacts
    pages/       page-<NNNN>.png            # text/mixed/blank/redacted display renders
    thumbnail.png                           # one per PDF
```
- `<NNNN>` = **1-based**, zero-padded to 4 (page 7 → `page-0007`). Match `pages[].page_number`.
- `<MM>` = zero-padded to 2, the image’s index on that page (first image → `01`).
- A page render lives in **exactly one** of `images/` or `pages/` (no duplication): image-pages →
  `images/`, everything else → `pages/`.
- **Decision to confirm:** staging `pages/` (display renders for non-image pages) is included by default so
  Stage 7 is a pure upload. If local storage is a concern, these may be **deferred to Stage 7** (render on
  ingest); image-page renders and artifacts must still be staged here. Recommended: keep `pages/`.

---

## 4. `page_kind` classification (deterministic — the heart of this stage)

Per page, first match wins. All thresholds tunable and echoed into the JSON.

1. **`blank`** — Stage‑1 already flagged it (`needs_vision_reason == "blank"`, or the blank gate:
   `ink_coverage < blank_ink_max` or `dark_coverage > 1 − blank_ink_max`). No content.
2. **`redacted`** (fully redacted) — redaction bars cover ~the whole page and the **visible non-bar text**
   is below `min_chars` (just a Bates footer). Detect: `redaction.redaction_runs > 0` **AND** bar area
   coverage ≥ `fully_redacted_bar_frac` (default 0.60) **AND** `text.char_count` (post-scrub, which excludes
   `[REDACTED]`) < `min_chars`. Nothing readable remains.
3. **`image`** — a genuine image page: `max_image_coverage ≥ full_page_threshold` (default 0.80) **AND**
   the page is **not** text-sufficient (`text.char_count < min_chars`, i.e. no usable text beyond a stamp).
   This is a scanned photo / image-only page → **becomes an image asset**.
4. **`mixed`** — text-sufficient **and** carries a mid-size embedded figure: `embedded_image_count ≥ 1`
   AND `figure_min_coverage ≤ max_image_coverage < full_page_threshold`. Text is the content; the figure is
   captured as an **artifact** (image-embed only, no vision).
5. **`text`** — everything else: text-sufficient, with at most small images (logos). A logo is a small
   embedded image (`max_image_coverage` well below `figure_min_coverage`) on a text page — **not** an image
   page.

**Only `page_kind == image` pages require vision (Stage 4).** `blank` and `redacted` never do. `text`/`mixed`
never do (their text is canonical; figures get image-embeddings, not captions, unless escalated later).

> **Amended 2026-06-18 (DS09):** vision routing is now **over-inclusive** — `image ∪ mixed ∪
> low_quality_text` — so `mixed` figures and poor-OCR/handwriting pages *are* sent to vision (while keeping
> their canonical text). See the top-of-doc update.

> This supersedes Stage‑1’s `needs_vision` *for vision routing*: a redacted page **with a usable text layer**
> is `text`/`mixed`, not `image`, so it is **not** sent to vision.

> **Known false-negative (not fixed):** a **full-page image with an overlaid OCR/annotation text layer**
> (`char_count ≥ min_chars`) matches neither rule 3 (`image` needs `char_count < min_chars`) nor rule 4
> (`mixed` needs coverage `< full_page_threshold`) → it falls to rule 5 `text` and never reaches vision.
> Empirically rare on DS09 (0 of 200 random `text` pages were photos; ≤~1.5%). `dark/ink_coverage` are null
> on `text` pages, so it can't be caught from metadata — only by rendering pixels. Accepted as a small miss;
> the `low_quality_text` inclusion above recovers the handwriting subset.

---

## 5. Rendering requirements

- **Render every page** to PNG via PyMuPDF (`page.get_pixmap`). Reuse Stage‑1’s DPI clamp
  (`clamp_render_dpi`) so large-format pages don’t OOM.
- **DPI:** `render_dpi` default **200** for page/asset renders (legible for display and for gpt‑5‑mini on
  image pages). Configurable. (Distinct from Stage‑1’s cheap 72/150 analysis renders.)
- **Destination:** `page_kind == image` → `<name>/images/page-<NNNN>.png`; else → `<name>/pages/page-<NNNN>.png`.
- **Thumbnail:** one per PDF → `<name>/thumbnail.png`, a downscaled render of a representative page
  (default: first page; `thumbnail_max_px` default 512, longest side). No per-page thumbnails.
- **Determinism:** identical input ⇒ identical PNG bytes (fixed DPI, no timestamps in pixels).

---

## 6. Embedded photo artifact extraction

- For pages with `embedded_image_count > 0`, extract each embedded raster image via PyMuPDF
  (`get_images(full=True)` → `extract_image(xref)`), normalize to PNG, write to
  `<name>/artifacts/page-<NNNN>-img-<MM>.png`.
- **Dedup by content hash** (sha256 of image bytes): identical images (a logo repeated on every page)
  are written **once**; record every occurrence (page, index) in the manifest pointing at the single file.
- **Optional min-size filter** (`artifact_min_px`, default 64): skip tiny/icon images that carry no
  content and would only waste a downstream caption/safety call. Record skipped count.
- This is pure extraction/normalization — **no captioning or safety here** (Stage 4).

---

## 7. Sidecar JSON extensions (Stage 2 writes)

Add to each page object:
```jsonc
"page_kind": "image",                         // text | image | mixed | blank | redacted
"render_path": "<name>/images/page-0007.png", // relative; where this page was rendered
"render_dpi": 200
```
Add at the top level:
```jsonc
"thumbnail_path": "<name>/thumbnail.png",
"image_assets": [
  { "kind": "page",     "page_number": 7,  "path": "<name>/images/page-0007.png",
    "width_px": 2550, "height_px": 3300, "coverage": 0.98 },
  { "kind": "artifact", "page_number": 12, "image_index": 1,
    "path": "<name>/artifacts/page-0012-img-01.png", "content_sha": "…",
    "width_px": 900, "height_px": 1200, "occurrences": [{ "page_number": 12, "image_index": 1 }] },
  { "kind": "page",     "page_number": 3,  "path": "<name>/images/page-0003.png",
    "width_px": 1700, "height_px": 2200, "route_reason": "low_quality_text" }  // added by --escalate-vision
],
"stage2": {
  "tool_version": "…", "generated_at_utc": "…",
  "config": { /* echo of thresholds */ },
  "counts": { "pages_rendered": 183, "image_pages": 8, "artifacts": 14, "artifacts_skipped": 3 }
}
```
- `image_assets[]` is **the** manifest Stage 4 iterates (image-pages + photo artifacts + escalated
  `low_quality_text` pages, the last carrying `route_reason`), all **relative paths**.
- `blank`/`redacted`/`text`/non-escalated-`mixed` pages are **not** in `image_assets[]`. Their display
  renders are deferred to Stage 7 (and on DS09 the staged `pages/` were purged 2026-06-18 — see top update).

---

## 8. Automation requirements

- **Local only.** No network, no Azure, no DB. Writes only under `<dir>/<name>/` and the sidecar.
- **Idempotent + resumable.** Skip a page whose render already exists; skip an artifact whose file exists;
  skip a PDF whose `stage2` block is present and complete (unless `--force`). A crash loses at most the
  in-flight PDF.
- **Deterministic** (per §4/§5): stable file names, stable ordering, byte-identical renders.
- **Parallel + bounded:** one PDF per worker (PyMuPDF handles aren’t thread-safe); pages sequential within a PDF.
- **Fault-isolated:** a corrupt/encrypted PDF (already `status:error` from Stage 1, or fails to open now)
  is skipped with an entry in the run manifest; never aborts the walk.
- **Run manifest:** `stage2-run-manifest.json` at the root — PDFs seen/rendered/skipped/errored; totals for
  pages rendered, image pages, artifacts, blanks, redacted; the resulting **vision workload** =
  `count(page_kind == image) + count(artifacts kept)` (the operator’s cost preview for Stage 4); tool/lib
  versions; wall-clock.

---

## 9. Acceptance criteria

1. **Every page rendered** once; image-pages in `images/`, others in `pages/`; names match `pages[].page_number`.
2. **`page_kind` correct on a sample:** scanned photo pages → `image`; text pages with a logo → `text`
   (not image); figure-on-text pages → `mixed`; fully-redacted-with-footer → `redacted`; blanks → `blank`.
   (Spot-check with rendered images as the oracle, as in Stage‑1 validation.)
3. **Redacted email pages with a real text layer are `text`/`mixed`, not `image`** — i.e. they will **not**
   go to vision. This is the corpus-specific cost win; verify the vision-workload count is a small fraction.
4. **Artifacts deduped:** a logo repeated across N pages yields one file in `artifacts/` with N occurrences.
5. **`image_assets[]`** lists exactly the image-pages + kept artifacts, all relative paths that resolve.
6. **No Azure/DB writes**; only local files created. **Determinism:** re-run yields identical renders/JSON.
7. **Idempotency:** re-running skips completed PDFs/pages/artifacts; `--force` re-does them.

---

## 10. Open questions / notes for the implementer

- **`pages/` staging vs. deferral** (§3): keep by default; revisit if staging storage for ~1M page PNGs is
  prohibitive (then render non-image pages at Stage 7 instead).
- **Thumbnail source:** first page is the default; a “most representative” heuristic (first non-blank,
  non-cover page) is a nice-to-have, not required.
- **`fully_redacted` threshold** (§4 rule 2) is the fuzziest — validate against known fully-redacted pages
  before trusting; bias toward `image`/vision only if genuinely unsure (fail-safe), but note that a truly
  fully-redacted page has nothing for vision to read either, so `redacted` (skip) is the cost-right call.
- **Artifact min-size filter** trades a tiny chance of missing a small but meaningful image against many
  wasted downstream calls on icons; default 64 px, tunable.
- **`mixed` figures** are captured as artifacts and image-embedded (Stage 4) but **not** vision-captioned by
  default; escalate specific figures to a caption only if a later need arises.
