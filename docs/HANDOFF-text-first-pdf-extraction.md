# Requirements: Text-First PDF Pre-Extraction (sidecar JSON) for low-cost ingestion

**Status:** requirements / handoff (no implementation yet).
**Goal:** for PDFs that already carry a usable text layer (the bulk of recent litigation productions, e.g. the
Epstein DataSet-12 volumes), extract their text **locally and for ~free** and skip the expensive Azure
vision/Document-Intelligence pass — while staying safe on **redacted** material. At multi-million-page
scale this is a five-to-six-figure saving (vision ≈ $10k–30k / 1M pages; DI ≈ $1.5k / 1M pages;
local text extraction ≈ $0).

This document specifies **Stage 1**: a deterministic, offline tool that walks a folder of PDFs and writes a
sidecar `<name>.json` next to each `<name>.pdf`. A separate **Stage 2 "emulator"** (defined here only as a
*contract*, not built) consumes those JSONs to populate the pipeline tables, bypassing OCR/vision for
text-sufficient pages.

---

## 0. Feasibility — is this real?

Yes, for text-rich productions — measured directly this session with read-only PyMuPDF probes (`~/mcp-test/epstein_*.py`):

- **DataSet-12 / VOL00012** (152 files, 1,525 pages): **1,579,077** characters of embedded text;
  **99.0%** carried as an invisible OCR layer under the scanned image. Quality: `wordlike_tokens ≈ 100%`,
  `mean_word_len 4.8` (normal English), `alpha_ratio 57.9%` — i.e. **genuine, readable text**, not OCR garbage.
- The pipeline today routes these to vision anyway: a page with **text ≥50 chars AND an embedded image**
  classifies as `mixed_content` → vision (`pipeline/shared/svkb_pipeline/pdf.py` `_classify`,
  `pipeline/ocr-job/main.py`). So all 1,525 pages would hit vision even though the text is already present.
  **That is the waste this process eliminates.**

It is **not** universal. Two corpora must still go to vision, and the design must detect them automatically:

- **PBB microfilm (project-blue-book):** `get_text` returns only frame-edge artifacts ("`SM-138 0548`",
  "`NORMALT 15 MARS`"), max ~71 chars/page of garbage. The **quality gate** (§8) must fail these → vision.
- **Redacted pages where the invisible OCR was generated pre-redaction:** the text under the black bars is
  recoverable from the layer (confirmed: `EFTA00003399` had 87% of its invisible glyphs sitting under
  redaction marks). Indexing that raw layer **leaks redacted content**. The **redaction scrubber** (§7) is
  therefore **mandatory**, not optional.

**Conclusion:** build it, but as a *gated hybrid* — text-first where the text is real and safe, automatic
fallback to vision otherwise. Never a blind "trust `get_text`."

---

## 1. Scope

**In scope (Stage 1, this spec):**
- Recursively walk a root folder; for every `*.pdf`, emit a sibling `<name>.json`.
- Per page: extract text → **scrub redaction-occluded/invisible-leak text** → convert to Markdown →
  compute metrics → emit a per-page **`needs_vision` recommendation**.
- Doc-level metadata + summary + a content hash for idempotency.
- 100% local: **no network, no Azure, no database**. Read PDFs; write JSON only.

**Out of scope (documented as contracts, not built here):**
- Stage 2 emulator that loads JSON → pipeline tables (§11 contract only).
- Image embedding / multimodal indexing and image content-safety (Stage 2; §10 records what it needs).
- Load-file (`.DAT`/`.OPT`) parsing for Bates/metadata (§9 — optional enrichment hook).

---

## 2. Architecture (two stages)

```
Stage 1  EXTRACTOR (this spec)                 Stage 2  EMULATOR (contract only, §11)
  walk folder                                    read <name>.json
  ├─ per PDF: open with PyMuPDF                   ├─ upsert pipeline.documents
  ├─ per page: text → scrub → md → metrics        ├─ per page: upsert document_pages
  ├─ decide needs_vision (gate + redaction)       │    - needs_vision=false → store md as text, SKIP vision
  └─ write <name>.json  (deterministic, local)    │    - needs_vision=true  → enqueue vision-page event
                                                   ├─ still: image-embed + image safety on flagged images
                                                   └─ chunk → embed → index   (unchanged tail of pipeline)
```

Stage 1 is **advisory + data-carrying**: it never makes the final ingestion decision, it *recommends* and
*supplies the text*. Stage 2 owns DB writes and may override (e.g. force-vision a dataset).

---

## 3. Extractor automation requirements

1. **Input:** a root path (folder). Recurse all subdirectories. Match `*.pdf` (case-insensitive).
2. **Output:** for each `<dir>/<name>.pdf`, write `<dir>/<name>.json` (same directory, same basename, `.json`
   suffix). Never write anywhere else.
3. **Idempotent + resumable:** skip a PDF whose sidecar JSON already exists *and* whose recorded
   `source.sha256` matches the current file. `--force` re-extracts. A crash mid-run loses at most the
   in-flight file.
4. **Deterministic:** identical input ⇒ byte-identical JSON (modulo the caller-supplied timestamp). No
   `random`, no wall-clock inside content; stable key ordering; stable page/word ordering. `generated_at_utc`
   is the **only** time field and is passed in by the caller/runner, not read from the clock inside the
   per-file logic (so re-runs diff cleanly).
5. **Parallel + bounded:** process N files concurrently (default = CPU-2). One PDF per worker; pages within a
   PDF are processed sequentially (PyMuPDF doc handles are not thread-safe).
6. **Fault-isolated:** a malformed/encrypted/corrupt PDF must **not** abort the walk. Emit a JSON with
   `status: "error"` and an `error` object; continue. Password-protected PDFs → `status: "error"`,
   `error.code: "encrypted"`.
7. **Run manifest:** write a single `extract-run-manifest.json` at the root summarizing: files seen,
   extracted, skipped, errored; aggregate `pages_total`, `pages_text_sufficient`, `pages_need_vision`,
   `pages_redaction_flagged`; tool/lib versions; wall-clock. This is the operator's go/no-go artifact.
8. **No external calls:** assert offline. The only dependency surface is PyMuPDF (+ optional `pymupdf4llm`)
   and the Python stdlib.
9. **Config surface (CLI flags / config file):** all thresholds in §8/§7 are tunable and **recorded into each
   JSON** under `generator.config` so a JSON is self-describing.

---

## 4. Per-page processing order (must be exact)

For each page, in this order:

1. **Render-free analysis first.** Pull `get_text("words")` (with bboxes), `get_texttrace()` (render mode +
   opacity per span), `get_images(full=True)`, `get_image_info()`, `get_drawings()`, `page.rect`,
   `page.rotation`. (No rasterization needed for the text decision — keep it cheap.)
2. **Redaction scrub (§7).** Compute occluder rectangles and invisible-under-dark glyphs; build the **scrub
   set** of word/span ids to drop. *(Detecting "under dark" requires one low-DPI grayscale render — see §7;
   do this only when occluders or a large invisible layer are present, to keep the common case render-free.)*
3. **Reconstruct text (§6)** from the surviving (non-scrubbed) spans/blocks, in the format chosen by
   `text_layer_type`. The emitted `text.content` **must already exclude** scrubbed content — it is the
   safe-to-index artifact.
4. **Metrics (§5 fields)** computed on the **scrubbed** text (so quality/length reflect what would be indexed).
5. **Quality gate (§8)** → `quality.pass`.
6. **Routing decision (§9 rules)** → `needs_vision` + `needs_vision_reason`.

---

## 5. Sidecar JSON schema (v1)

Top-level object. **All keys required unless marked optional.** Types in parentheses.

```jsonc
{
  "schema_version": "1.0",                         // (string) bump on breaking changes
  "status": "ok",                                  // (enum) "ok" | "error"
  "error": null,                                   // (object|null) {code, message} when status="error"

  "generator": {
    "tool": "text-first-extract",                  // (string)
    "tool_version": "1.0.0",                        // (string)
    "pymupdf_version": "1.27.2",                    // (string)
    "reconstruction": "adaptive",                  // (string) layer-type-adaptive (§6); per-page format below
    "generated_at_utc": "2026-06-15T00:00:00Z",    // (string, ISO8601) caller-supplied
    "config": { /* echo of all thresholds in §7/§8 */ }
  },

  "source": {
    "filename": "EFTA02730274.pdf",                // (string)
    "relative_path": "VOL00012/IMAGES/0001/EFTA02730274.pdf", // (string) relative to walked root
    "bytes": 150875909,                            // (int)
    "sha256": "…",                                 // (string) hex; idempotency key
    "page_count": 183,                             // (int)
    "pdf_producer": "…",                           // (string|null) from PDF /Producer
    "pdf_metadata": { /* (object) raw doc.metadata, optional keys */ }
  },

  "document": {
    "source_kind": "pdf",                          // (string) fixed "pdf"
    "content_type": "application/pdf",             // (string)
    "bates_begin": null,                           // (string|null) optional, from load file (§9 hook)
    "bates_end": null,                             // (string|null)
    "title": null                                  // (string|null)
  },

  "extraction_summary": {
    "total_text_chars": 350112,                    // (int) over scrubbed text
    "visible_chars": 13764,                        // (int)
    "invisible_chars": 1338746,                    // (int) BEFORE scrub (exposure signal)
    "scrubbed_chars": 412,                         // (int) removed by redaction scrub
    "pages_text_sufficient": 175,                  // (int) needs_vision=false (incl. blank pages)
    "pages_need_vision": 8,                         // (int) needs_vision=true
    "pages_redaction_flagged": 3,                  // (int) redaction.suspected=true
    "pages_blank": 12,                             // (int) needs_vision=false via the blank/slip-sheet gate (§9a)
    "text_quality": { "alpha_ratio": 0.58, "wordlike_ratio": 1.0, "mean_word_len": 4.8, "pass": true },
    "routing_recommendation": "text_primary"       // (enum) "text_primary" | "vision_primary" | "mixed"
  },

  "pages": [
    {
      "page_number": 1,                            // (int) 1-based
      "page_class": "mixed_content",               // (enum) mirrors pipeline _classify:
                                                   //   "ocr_backing_scan" | "full_page_visual"
                                                   //   | "mixed_content" | "embedded_subimage"
      "needs_vision": false,                       // (bool) RECOMMENDATION (Stage 2 may override)
      "needs_vision_reason": "text_sufficient",    // (enum) §9: "text_sufficient" | "low_text"
                                                   //   | "quality_fail" | "redaction_suspected"
                                                   //   | "image_dominant" | "has_figures" | "blank"

      "text": {
        "content": "….",                           // (string) SCRUBBED, safe-to-index text in `format`
        "format": "structured_text",               // (enum) "markdown" | "structured_text" | "plain"
                                                   //   chosen by text_layer_type (§6); NOT always markdown
        "char_count": 1850,                        // (int) chars in content (post-scrub)
        "visible_chars": 30,                       // (int)
        "invisible_chars": 1820                    // (int, pre-scrub on this page)
      },

      "metrics": {
        "text_layer_type": "ocr_layer",            // (enum) "born_digital" | "ocr_layer" | "mixed" | "none"
        "distinct_font_sizes": 17,                 // (int) typographic-noise signal (§6); high => OCR layer
        "distinct_fonts": 4,                       // (int)
        "embedded_image_count": 1,                 // (int) get_images(full=True) length
        "max_image_coverage": 0.98,                // (float 0..1) largest image area / page area
        "is_full_page_image": true,                // (bool) max_image_coverage >= full_page_threshold
        "ink_coverage": 0.0005,                    // (float|null) non-white pixel fraction; computed ONLY for
                                                   //   low-text pages (the blank/slip-sheet gate, §9a); else null
        "dark_coverage": 0.0001,                   // (float|null) near-black pixel fraction (same render); ~1.0
                                                   //   on a solid-black scanner blank; null off the low-text path
        "bates_stamp": "EFTA00003159",             // (string|null) Bates control number parsed from page text
        "width_pt": 612.0, "height_pt": 792.0,     // (float) page size in points
        "rotation": 0,                             // (int) degrees
        "quality": { "alpha_ratio": 0.57, "wordlike_ratio": 1.0, "mean_word_len": 4.8, "pass": true }
      },

      "redaction": {
        "occluder_rects": 4,                       // (int) opaque fill rects (vector candidate redactions)
        "dark_occluder_rects": 4,                  // (int) of those, near-black
        "invisible_glyphs_total": 1820,            // (int)
        "invisible_glyphs_under_dark": 0,          // (int) diagnostic: invisible glyphs inside a detected bar
        "redaction_runs": 0,                       // (int) detected redaction BARS marked [REDACTED] (§7)
        "scrubbed_char_count": 0,                  // (int) chars removed (text that was under a bar)
        "suspected": false                         // (bool) §7 trigger: redaction_runs > 0
      },

      "images": [                                  // (array) for Stage 2 image-embed + safety; NOT vision-OCR
        { "xref": 12, "bbox": [0,0,612,792], "width_px": 2550, "height_px": 3300,
          "coverage": 0.98, "is_full_page": true, "colorspace": "DeviceRGB" }
      ],

      "flags": []                                  // (array<string>) freeform warnings, e.g. ["heavy_invisible_layer"]
    }
  ]
}
```

Notes:
- `page_class` values are intentionally identical to `pipeline/shared/svkb_pipeline/pdf.py` so Stage 2 maps 1:1.
- `text.content` is the **only** text Stage 2 should index. Raw/unscrubbed text is never emitted.
- `invisible_chars` is reported (exposure signal) even when 0 chars are scrubbed — operators need visibility
  into how much hidden text exists, separate from how much was occluded.

---

## 6. Text reconstruction (NOT "always markdown") — layer-type adaptive

**Critical finding (measured this session, `~/mcp-test/_font_probe.py`):** the text in these productions is
overwhelmingly an **invisible OCR layer** (PyMuPDF render mode 3; font name `HiddenHorzOCR`). On such layers the
typography is **noise, not structure** — e.g. the redacted Contact Book shows **16–19 distinct font sizes per
page** (2–12 pt, no clean clusters) because the OCR engine sized each word to fit its scanned glyph box, not to
mark headings. **You cannot deterministically derive headings/sections/emphasis from this without an LLM/vision.**
Forcing "markdown" here would either fabricate structure or require the very LLM cost we are avoiding.

Therefore reconstruction is **adaptive on `text_layer_type`** (detected per page; see detection below):

- **`ocr_layer`** (high invisible-glyph ratio AND noisy size distribution): emit **`structured_text`** —
  faithful reading-order text with only the structure that geometry supports deterministically:
  - lines and paragraph breaks from word bboxes + vertical gaps;
  - reading columns via x-coordinate clustering (so 2-column pages don't interleave);
  - bullet/numbered lists from leading glyphs (`•`, `-`, `\d+[.)]`);
  - tables **best-effort by column alignment only** (do NOT rely on `find_tables` — the grid lines are in the
    raster image, not vector data).
  **Do not infer headings or bold/italic.** `format = "structured_text"`.
- **`born_digital`** (real vector text: low invisible ratio, few clean size clusters, meaningful bold flags):
  `pymupdf4llm` *does* produce good Markdown deterministically here. `format = "markdown"`.
- **`mixed`**: treat as `ocr_layer` (conservative) unless a clear born-digital block dominates.
- **`none`** (no usable text): `content = ""`, page will be `needs_vision=true` via §9.

**Layer-type detection (cheap, render-free):** from `get_texttrace()` per page compute
`invisible_ratio = invisible_glyphs / total_glyphs` and `distinct_font_sizes` (rounded to 0.5 pt).
- `invisible_ratio ≥ 0.5` AND `distinct_font_sizes ≥ size_noise_threshold` (default 8) ⇒ `ocr_layer`.
- `invisible_ratio < 0.1` AND `distinct_font_sizes ≤ 4` ⇒ `born_digital`.
- else ⇒ `mixed`. (All thresholds in `generator.config`.)

**Hard requirements (all formats):**
- **Scrub before assembly.** `text.content` is built from the **surviving span/word set** after the redaction
  scrub (§7) — operate at the span level so occluded/leaked words never reach output. If using `pymupdf4llm`
  (page-level) on a page with `redaction.suspected=true`, fall back to the span-level path so scrubbing is exact.
- **Determinism:** stable ordering (top-to-bottom, left-to-right within a column; columns left-to-right); no
  locale-dependent formatting.
- **Empty pages:** `text.content = ""` (not null).
- The pipeline indexes `text.content` for **retrieval**; faithful reading-order text is the goal, pretty
  structure is not. Honesty over polish: never emit structure the source doesn't deterministically support.

---

## 7. Redaction safety (MANDATORY) — positive bar detection

The #1 risk. A page "looks redacted" to a human while the OCR text under the black bar remains extractable.

**Detect the bars themselves (positive detection).** A redaction is a **solid dark rectangle** (a vector fill
OR burned into the scan). Detect them — independent of whether any OCR text sits under them — so a redaction is
caught and positionally marked even when nothing was OCR'd underneath:
- **Vector bars:** from `get_drawings()`, opaque dark filled rectangles (area between `min_occluder_area`
  (50 pt²) and `0.98 × page_area`; fill luminance < `dark_lum`). Solid by construction.
- **Raster bars:** render the page once at low DPI (≈150) to grayscale and scan for near-black horizontal runs
  (≥ `bar_min_width_pt`), merged vertically into rectangles. Filter by bar-like size
  (`bar_min/max_height_pt`, `bar_max_area_frac`).
- **Bar vs. dark PHOTO — the decisive test:** verify each candidate on its **interior** (inset
  `bar_interior_inset`, default 25%). A real bar's interior is **pure black** (mean < `redaction_solid_mean_max`,
  default 10) and ≥ `bar_solid_dark_frac` near-black; a dark photo/figure region's interior is dark **gray**
  (mean ≥ ~13 on real pages) and is rejected. *(Measured + vision-verified: pure-black interior cleanly
  separates real bars (mean ≈ 0) from magazine/courtroom photo panels (mean 13–55).)*

**Mark + scrub.** Place the `redaction_marker` (`[REDACTED]`) at each bar's position in reading order, and drop
every glyph whose bbox is ≥ `bar_overlap` inside a bar (a leak) — these never enter `text.content`. Count marked
bars in `redaction.redaction_runs`, scrubbed chars in `redaction.scrubbed_char_count`. Glyphs merely sitting over
a *photo* (not a solid bar) are **kept** as real text — they are not redactions.

**Flag `redaction.suspected = true`** when `redaction_runs > 0` (a real bar was detected). "Invisible glyphs over
dark pixels" alone is **not** a trigger — it false-positives on dark imagery (verified on real pages).

**Routing consequence:** a `suspected` page **must** set `needs_vision = true`,
`needs_vision_reason = "redaction_suspected"`. Rationale: if redaction was applied, only the **rendered**
(visibly-redacted) image is authoritative; Stage 2 must read it via vision, not trust the layer. The
`[REDACTED]`-marked text is still emitted (useful, lower-risk), but the page is not marked text-sufficient.

This makes the failure mode **fail-safe**: ambiguity ⇒ vision, never silent leak.

---

## 8. Quality gate

Prevents garbage OCR (PBB microfilm) from being mistaken for usable text. Computed on the **scrubbed** page text:

- `char_count >= min_chars` (default 40)
- `alpha_ratio >= min_alpha_ratio` (default 0.45) — alphabetic chars / total chars
- `wordlike_ratio >= min_wordlike_ratio` (default 0.60) — fraction of `[A-Za-z]{2,}` tokens of length 2–15
- `mean_word_len` within `[2.5, 9.0]`

`quality.pass = true` only if all hold. Tunable; defaults chosen so DataSet-12 passes (0.58 alpha, 1.0
wordlike, 4.8 mean) and PBB microfilm fails (sub-40 chars of edge noise). All thresholds echoed into
`generator.config`.

---

## 9. Routing decision rules (`needs_vision`) — the heart

Evaluate per page, first match wins:

1. `redaction.suspected` ⇒ `needs_vision=true`, reason `redaction_suspected`. (§7)
2. scrubbed `char_count < min_chars` ⇒ low-text page; run the **blank/slip-sheet gate (§9a)** before vision:
   - if `ink_coverage < blank_ink_max` (white) OR `dark_coverage > 1 − blank_ink_max` (solid black) ⇒
     `needs_vision=false`, reason `blank` (slip sheet / separator / blank scan — nothing to read). Bates stamp
     still captured + emitted as `text.content`.
   - else ⇒ `needs_vision=true`, reason `low_text`. (genuine image-only page with content)
3. `quality.pass == false` ⇒ `needs_vision=true`, reason `quality_fail`. (microfilm/garbage OCR)
4. **Image-dominant without sufficient co-located text** — `embedded_image_count >= 1` AND
   `is_full_page_image == false` AND there exist embedded sub-images not covered by text (figures/photos the
   text layer won't capture) ⇒ `needs_vision=true`, reason `has_figures`. *(Heuristic; conservative — see §13
   open question.)*
5. Otherwise ⇒ `needs_vision=false`, reason `text_sufficient`.

**Key refinement vs. today's pipeline:** the current `_classify` sends *any* text-plus-image page to vision
(`mixed_content`). Rule 5 deliberately keeps a **full-page scanned image + a passing text layer** as
text-sufficient (the page IS its OCR), which is exactly the DataSet-12 majority. `page_class` still records
`mixed_content` for fidelity, but `needs_vision` is the operative signal.

`extraction_summary.routing_recommendation`: `text_primary` if ≥ 90% of pages are text-sufficient,
`vision_primary` if ≤ 10% are, else `mixed`.

### 9a. Blank / slip-sheet gate (don't pay vision to read a blank page)

In Bates productions the dominant page type in some volumes is a **full-page scanned image with only a Bates
footer stamp** (DataSet-02: 601/699 pages = 86% `full_page_visual`, ~12 chars of text = the control number).
Most are genuine scans and correctly go to vision (rule 2 `low_text`). But a meaningful fraction are
**slip sheets / separators / privilege placeholders / scanner blanks** — image + footer, *no content*. Paying a
vision-LLM call to "read" those is pure waste; at 86% full-page-visual, even a small blank fraction is real money.

The current routing can't tell a blank slip sheet from a real scan (both are "image + footer, no text"). The
gate adds the missing signal **cheaply**: for low-text pages only (the vision-bound subset), render once at
`blank_render_dpi` (default 72 — coverage needs no detail) and measure coverage at **both extremes** from that
single render — `ink_coverage` = fraction of non-white pixels (`< ink_white_level`, default 245) and
`dark_coverage` = fraction of near-black pixels (`< dark_level`, default 90). A blank is a near-uniform page at
*either* end: a stamp-on-white page has near-zero `ink_coverage`; a solid-black scanner blank has near-one
`dark_coverage`.

- `ink_coverage < blank_ink_max` (default **0.0008**, deliberately conservative) ⇒ white/near-empty ⇒
  `needs_vision=false`, reason `blank`. Store the Bates stamp as `text.content`; flag `slip_sheet` (has image)
  or `blank_page` (none).
- `dark_coverage > 1 − blank_ink_max` ⇒ solid-black/all-dark ⇒ `needs_vision=false`, reason `blank`,
  flag `blank_black`.
- otherwise ⇒ `low_text` → vision.

**Fail-safe direction:** silent content loss (skipping a faint real page) is worse than a wasted vision call, so
each threshold catches only pages that are essentially uniform at that extreme — any real marks (light text on a
dark frame, or a few words on white) break the uniformity and route back to vision. `ink_coverage` and
`dark_coverage` are emitted on every low-text page (null elsewhere) so both can be **calibrated against a real
volume** before trusting them. The render is bounded to low-text pages, so text-rich pages stay render-free. All
thresholds in `generator.config`.

---

## 10. Images still matter (don't lose them)

Skipping **vision transcription** is not skipping images. Stage 2 still needs, per the `images[]` array:
- **Image-embed / multimodal:** each embedded image → `pipeline.image_embeddings` + `image_occurrences`
  (so visual search keeps working). Stage 1 records `xref`, `bbox`, dimensions, `coverage`, `is_full_page`.
- **Image content-safety (CSAM):** vision currently also runs the per-page image safety scan. If vision is
  skipped, Stage 2 **must** run a cheap image-safety pass on flagged images so detection is not lost. Stage 1
  sets a page `flags` entry `"image_safety_required"` whenever `embedded_image_count > 0`.

Stage 1 does **not** extract/transcode image bytes (keeps it cheap); it records references for Stage 2.

---

## 11. Stage 2 emulator — consumption contract (not built here)

A JSON is "ingestible" when `status=="ok"`. The emulator should, per document (idempotent on `source.sha256`):
- Upsert `pipeline.documents` (status lifecycle as today); set `page_count = source.page_count`.
- Per page, upsert `pipeline.document_pages`:
  - `page_class`, `embedded_image_count`, `needs_vision` ← JSON.
  - if `needs_vision == false`: write `ocr_text = text.content`, **emit no vision-page event**.
  - if `needs_vision == true`: render the page + emit a vision-page event (existing path). The scrubbed
    `text.content` may still be stored as a hint but is not authoritative for redaction-suspected pages.
- For every page with images: emit image-embed work + the image-safety pass (§10).
- Use **count-based completion** exactly like the deployed fix (`needs_vision=false OR vision_md set`); see
  [`HANDOFF-pbb-pipeline-and-multimodal.md`] and migration `0015_page_needs_vision.sql`.
- Continue the unchanged tail: chunk → embed → index.

**Override hook:** Stage 2 may force `needs_vision=true` for an entire dataset (defense-in-depth) regardless of
the recommendation — mirrors the existing `OCR_DI_DISABLED_DATASETS`-style per-dataset control.

---

## 12. Validation & acceptance criteria

1. **Correctness sample:** on 20 DataSet-12 files, ≥ 95% of pages `needs_vision=false`, and a manual read of 10
   random `text.content` outputs shows faithful, well-ordered text.
2. **Redaction safety:** on the known redacted set (e.g. `EFTA00003399`, the flight-log Contact Book),
   every page with under-dark invisible text is `redaction.suspected=true` and its scrubbed `text.content` contains
   **none** of the under-bar tokens (spot-check with the §7 detector as oracle).
3. **Garbage rejection:** on PBB `project-blue-book` PDFs, ≥ 95% of pages `needs_vision=true`
   (`quality_fail`/`low_text`) — the tool must not "cost-optimize" microfilm into the text lane.
4. **Determinism:** re-running on an unchanged file yields byte-identical JSON except `generated_at_utc`.
5. **Robustness:** a folder containing a corrupt PDF and an encrypted PDF completes the walk; both get
   `status:"error"` JSONs; the run manifest counts them.
6. **Cost projection:** the run manifest's `pages_text_sufficient / pages_total` × (vision $/page) is the
   documented saving for that batch.

---

## 13. Edge cases & open questions for the implementer

- **Rule 4 (`has_figures`) is the fuzziest.** Distinguishing "full-page scanned image + OCR" (skip vision) from
  "text page with an embedded chart/photo" (needs vision) needs a concrete heuristic. Proposed: full-page image
  (`max_image_coverage ≥ full_page_threshold`, default 0.80) ⇒ treat the single image as a backing scan (rule 5);
  multiple images or a mid-size image not under text ⇒ rule 4. **Validate against real figured documents before
  trusting.** When unsure, bias to vision.
- **Multi-column / table-heavy pages:** Markdown fidelity varies; acceptable for retrieval, but note in `flags`
  if block structure is ambiguous.
- **Rotated / skewed scans:** honor `page.rotation`; if text extraction yields high garbage despite a present
  layer, the quality gate catches it → vision.
- **Tagged-but-empty text layers / watermark-only text:** quality gate + `min_chars` handle these.
- **Load-file enrichment (`.DAT`/`.OPT`):** optional Stage-1.5 hook to populate `document.bates_*`/`title`
  from a sibling production load file (the DataSet volumes ship `DATA/VOLxxxxx.DAT`). Not required for the
  cost win; nice for citations.
- **PII in JSON:** these sidecars contain the document text (incl. names). They inherit the source data's
  sensitivity — store them alongside the PDFs under the same access controls; never copy them off-box casually.

---

## 14. Why this is safe to pursue

The cost win and the redaction-leak risk are **coupled and resolved together**: the same per-page analysis that
decides "text is sufficient, skip vision" is the analysis that detects redaction and forces vision when in
doubt. The quality gate keeps hostile corpora (PBB) in the vision lane automatically. The result is a hybrid
that takes the cheap path **only** where the text is real *and* unredacted — which the measurements say is the
majority of recent productions — and falls back safely everywhere else.

Related: [`HANDOFF-pbb-pipeline-and-multimodal.md`] (pipeline + the deployed chunked-OCR/idempotency fix),
and the redaction-leak findings probed via `~/mcp-test/epstein_probe.py` / `epstein_probe2.py`.
