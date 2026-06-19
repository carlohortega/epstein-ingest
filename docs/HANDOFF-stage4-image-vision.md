# Handoff: Stage 4 — Image-asset vision (caption + description + safety + image embeddings)

**Status:** **BUILT 2026-06-19** (`src/stage4/`; offline tests `tests/run_stage4_tests.py` all pass; live
gate validated on the DS-09 calibration samples — see the update box directly below). The original
requirements text is retained for context.
**Read first:** [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md) — especially the
**"Update — DataSet-09 Stage 2 + vision-routing decisions (2026-06-18)"** box, which is the single most
important context for this stage. Then [`HANDOFF-stage2-render-classify-register.md`](HANDOFF-stage2-render-classify-register.md)
(what produces `image_assets[]`) and [`HANDOFF-stage3-text-summaries.md`](HANDOFF-stage3-text-summaries.md) +
[`HANDOFF-stage3-execution.md`](HANDOFF-stage3-execution.md) (the Azure/LLM plumbing you will **reuse**).

---

## Update — Stage 4 built + first-validated (2026-06-19)

`src/stage4/` is implemented, mirroring `src/stage3/` and reusing its `connection`/`client`/`ratelimit`
plumbing. What landed and what was learned:

- **Modules:** `pregate.py` (Tier-1 local blank filter), `prompts.py` (vision system prompt +
  `json_schema`), `client.py` (gpt-5-mini vision; reuses Stage-3's reasoning-shape auto-detect + content
  filter handling), `assets.py` (per-asset chain: pre-gate → vision → embed → content-safety), `embed.py`
  (Azure AI Vision multimodal embeddings), `safety.py` (Azure Content Safety image), `process.py` /
  `walk.py` (per-asset scheduler, resume, manifest, dry-run), `faillog.py`, `cli.py`. Iteration unit is the
  **image asset**; resume skips any asset already carrying `has_visual_content`.
- **Azure stack on THIS machine (`~/.env.local`; sv-kb is NOT checked out here) — all THREE services live
  + verified 2026-06-19:**
  - **gpt-5-mini vision** `AZURE_OPENAI_VISION_DEPLOYMENT=gpt-5-mini` on `fd-spyvault-ai.cognitiveservices.azure.com` ✅
  - **AI Vision multimodal embeddings** `AZURE_AI_VISION_*` on `ais-spyvault-ai.cognitiveservices.azure.com` ✅ (probe: 200, 1024-dim)
  - **Content Safety (CSAM net)** `AZURE_CONTENT_SAFETY_*` on a **dedicated** `cs-spyvault-ai.cognitiveservices.azure.com`
    (kind ContentSafety, **S0**, api-version `2024-09-01`) ✅ (probe: 200).
  Content Safety remains **gracefully optional** in code (if the env vars are absent it warns + skips, no
  per-asset errors), but it is now configured and ON. **Gotcha learned:** the *classic* multi-service
  `ais-spyvault-ai` (kind `CognitiveServices`) does **not** serve `/contentsafety/` — the same key returns
  200 for AI Vision but **401** for Content Safety ("feature not on this resource", not a bad key), which is
  why a dedicated `cs-spyvault-ai` resource was created. Probe: `C:\epstein-ingest\probe_content_safety.py`.
- **Live gate validation** (`C:\epstein-ingest\validate_stage4_samples.py`, pre-gate OFF so the real model
  adjudicates all 19 oracle PNGs): **19/19 correct** against the pixels. gpt-5-mini cleanly separates
  blanks/`broken_placeholder`/`text_only`/"Exhibit C" slip-sheets (`has_visual_content=false`) from real
  photos and document scans (`true`). Cost **$0.018 / 19 images** (~105 s) → whole-corpus realtime ≈ the
  §6 ~$130 estimate. **The §10.2 oracle mislabels `A3` as blank** — it is a genuine photograph (feathers/
  fur around a redaction box); the model correctly returns `photo`/`true`. Exactly the "minority are real
  photos under a redaction box" case §2 predicted; treat `A3` as expected-`true`.
- **Prompt hardened (`PROMPT_VERSION` `stage4-2026-06-19.2`):** the `A3` validation caption read
  *"Photograph of a person with the face redacted"* — a mild inference of the subject hidden under the bar.
  Rule 2 of the vision system prompt now forbids inferring the **kind** of subject a redaction conceals
  (while still keeping a redacted photo `has_visual_content=true` on its visible imagery). Offline tests
  still pass; a 1-image `A3` recheck against the live model was offered but not run (no-spend).
- **Cost with the CSAM net on:** whole-corpus ≈ **~$130 vision + ~$50–75 Content Safety** (artifacts-only,
  ~$1–1.5/1k images) + negligible embeddings. The Tier-1 pre-gate still removes ~30% of image-page vision
  volume. CS scans `artifact` assets by default; flip `--no-content-safety-artifacts-only` for full pages too.
- **Not done (deliberate, per build scope):** no full DS-09 run yet (validation only); **batch mode is not
  implemented** (`--batch` errors out — realtime only; multi-job chunking is still the open work); DS-10/11/12
  are not yet at DS-09's uniform Stage-2 end-state (§3 preconditions still stand).

---

## 0. What Stage 4 does (30-second version)

For every **image asset** registered by Stage 2 (`image_assets[]` in each sidecar — image pages + photo
artifacts + escalated `low_quality_text` pages), Stage 4 makes **one bundled gpt‑5‑mini vision call** →
`caption` + `description` + a **content verdict** (`content_class` + `has_visual_content`) + image
**safety tags**; runs **Azure Content Safety** on photo artifacts (the CSAM net); and produces an **image
embedding**. It **extends the sidecar JSON in place** (per-asset fields + a `stage4` block), writes a run
manifest, and appends blocked assets to a content-filter failures log. **Vision only — text is Stage 3.**
**Local staging only — no Azure Storage, no DB** (that's Stage 7). Idempotent, resumable, fault-isolated.

**This is the only premium-cost stage.** Everything about it is shaped by two things: (1) minimizing and
gating vision calls, and (2) the fact that this corpus is *evidence* with a real CSAM/explicit-image surface.

---

## 1. The corpus reality you MUST internalize (this is not generic vision)

Do not build Stage 4 as "caption every image." This corpus is peculiar in ways that dictate the design:

- **It is ~entirely scanned-image PDFs with an OCR text layer.** `is_full_page_image` is true on ~93% of
  pages and `max_image_coverage` is a near-constant **0.9778**. So "image coverage" tells you almost nothing —
  page routing was done by **text sufficiency**, and the vision set is small and noisy.
- **The vision set is OVER-INCLUSIVE by deliberate decision (Option A).** `image_assets[]` =
  **`image` pages ∪ photo artifacts ∪ escalated `low_quality_text` pages** (handwriting / poor-OCR scans,
  tagged `route_reason:"low_quality_text"`). ≈ **8–10% of pages.** This was chosen to avoid false negatives
  (real images hiding under a text overlay) and to recover handwriting — at the cost of sending some junk.
- **~63% of `image` pages are NOT useful pictures.** The dominant `full_page_image_low_text` class
  (`coverage == 0.9778`) is mostly **blank scans, broken-image placeholders** (email/web→PDF with missing
  images), and **exhibit slip-sheets** ("Exhibit C"). A *minority* are real photos (some under a redaction
  box). Genuine photographs (e.g. a Venice canal, a hillside excavator) live mostly in the low-coverage and
  non-0.9778 ≥0.5 classes. **Rendered examples to calibrate your eye live in `C:\epstein-ingest\samples\`**
  (the `A*`/`B*`/`TI*`/`sample*` PNGs from the DS‑09 investigation).
- **Scale:** DS‑09 `vision_workload` (image pages + kept artifacts) ≈ **111k** for the 85% covered by its
  manifest → whole-set ≈ **~130k vision units**. DS‑10 is larger (~3.3M pages). DS‑11/DS‑12 are smaller.

**Consequence:** the headline feature of Stage 4 is **not** the caption — it's the **`has_visual_content`
gate** that keeps blanks/broken/slip-sheets from ever becoming assets downstream. Build that first.

---

## 2. The `has_visual_content` / `content_class` contract (build this — it's the point)

The operator's explicit requirement: **we must never accept a non-image (blank / all-black / broken /
false-positive) as an asset.** Today nothing enforces that. Stage 4 introduces a **two-tier verdict**:

**Tier 1 — cheap LOCAL pre-gate (no API call), at Stage 2/escalate or at Stage-4 startup.**
- The slam-dunk blanks are detectable from pixels for free. For `image` pages we **already have
  `dark_coverage`** in the sidecar (Stage 1 computed it for image candidates); within the 0.9778 class,
  `dark_coverage < ~0.005` is a confident "blank". Set `has_visual_content=false`, `content_class="blank"`,
  and **skip the vision call** for these. This removes **~30% of the image-page vision volume at ~zero risk**
  (a provably-blank page cannot be a real image).
- Keep the pre-gate **conservative** (only ultra-confident blanks) so vision still adjudicates anything
  ambiguous. `dark_coverage` is **null on `text`/escalated pages** — for `low_quality_text` assets, measure
  the rendered PNG's tonal stats at gate time (the PNG already exists in `images/`).
- Record a `prefilter_hint` so the decision is auditable.

**Tier 2 — authoritative VISION verdict (the gpt‑5‑mini call), for everything not pre-gated.**
The structured response includes the verdict, not just prose:

```jsonc
// per entry in image_assets[], added by Stage 4:
"caption":            "one-line factual caption",
"description":        "longer visual description (a.k.a. visual_description)",
"content_class":      "photo | document_scan | diagram_chart | handwriting | blank | black_or_redacted | broken_placeholder | text_only | indeterminate",
"has_visual_content": false,           // THE GATE — true only for a real, meaningful image
"vision_confidence":  0.0,
"safety":             { "flag": "ok|review", "categories": [], "minor_indicator": false, "explicit": false },
"embedding_ref":      "…",             // image embedding (vector or sidecar/blob ref); only when has_visual_content
"prefilter_hint":     "likely_blank|null"
```

**Downstream exclusion rule (single source of truth = `has_visual_content`):**
- **Stage 5** (finalize doc summary): fold in only captions where `has_visual_content=true`.
- **Stage 6** (embed): create image embeddings only where `has_visual_content=true`.
- **Stage 7** (ingest): create a standalone image **asset/record** only where `has_visual_content=true`.
  Blanks/black/broken are **recorded but excluded as assets**; for a full-page `image` page that vision says
  is blank, also **demote that page's `page_kind`→`blank`** so the sidecar is internally consistent.
- **Content Safety still runs regardless of the gate** — a "blank" is the model's word, not proof.

---

## 3. Preconditions — Stage 4 needs the UNIFORM Stage-2 end-state on every dataset

Stage 4 reads the **staged PNGs** (`<name>/images/…` and `<name>/artifacts/…`) listed in `image_assets[]`.
It does **not** render (no PyMuPDF needed for the call itself). So every target dataset must first be at
**DS‑09's end-state**: `images/` (image pages **+** escalated `low_quality_text`) + `artifacts/` +
`thumbnail.png`, **no `pages/`**, `image_assets[]` complete. Current states (verified 2026-06-19):

| Dataset | Stage 1 | Stage 2 | Escalate (low_quality_text→images/) | Purge `pages/` | Stage-4 ready? |
|---|---|---|---|---|---|
| **DS‑09** | ✅ | ✅ (`--no-stage-pages`) | ✅ done | ✅ done | **YES** (uniform) |
| **DS‑10** | ✅ | ❌ **not started** | ❌ | n/a (none created) | No — run Stage 2 `--no-stage-pages` + `--escalate-vision` first |
| **DS‑11** | ✅ | ✅ (original full-staging) | ❌ needed | ❌ recommended (reclaims disk) | No — run `--escalate-vision` (+ `--purge-staged-pages`) |
| **DS‑12** | ✅ | ✅ (original full-staging) | ❌ needed | ❌ recommended | No — run `--escalate-vision` (+ `--purge-staged-pages`) |

The escalate/purge tooling exists: `python -m src.stage2 ROOT --escalate-vision` and
`--purge-staged-pages [--confirm]` (see `src/stage2/escalate.py` and the Stage‑2 spec's top amendment).
**Bring 10/11/12 to parity before running Stage 4 so it sees a uniform corpus.**

---

## 4. The vision call (gpt‑5‑mini)

- **Model:** **gpt‑5‑mini** (vision). Stage 3 used gpt‑4.1‑nano (text); Stage 4 needs a **separate
  gpt‑5‑mini deployment**. Add an env slot (e.g. `AZURE_OPENAI_VISION_DEPLOYMENT`). The Stage‑3 `client.py`
  **auto-detects chat vs reasoning shape** by model family — reuse it; gpt‑5‑mini will take the reasoning
  shape. **API version `2025-04-01-preview`** (supports `json_schema` structured outputs).
- **One bundled call per asset** returns the full structured object in §2 (caption + description +
  content_class + has_visual_content + safety + confidence). **Do not** split into separate caption/safety
  calls. Use **`json_schema` structured output** (mirror `src/stage3/prompts.py`).
- **Determinism:** `temperature=0` + `seed` where the family allows (reasoning models may ignore temp; stamp
  model+version+`generated_at` on the `stage4` block regardless).
- **Image input:** base64 the staged PNG from the asset `path`. Renders are **200 DPI** (Stage 2 default).
- **Prompt must explicitly ask the model to flag blank/no-content / broken-placeholder / text-only** so
  `content_class`/`has_visual_content` are reliable — this is the corpus's dominant case, not an edge case.

---

## 5. Image embeddings & Content Safety (the safety architecture)

- **Image embeddings:** Azure **AI Vision multimodal embeddings** (separate endpoint/key from OpenAI), only
  for `has_visual_content=true` assets. Store a vector or an `embedding_ref` (decide: inline vs sibling file,
  consistent with how Stage 6 stores text vectors). Cheap relative to the vision call.
- **Azure Content Safety (image):** run on extracted **photo artifacts** (and image pages) — the **CSAM net**
  for photos embedded in otherwise-textual productions. **Non-negotiable in this domain.** Separate
  endpoint/key.
- **The content-filter blocking reality — this is the #1 Stage‑4 risk.** Stage 3 already saw Azure **block a
  real fraction of pages at the model call** (DS‑12 ≈ 4% of summarized pages, all `sexual:high`, ~84% with a
  minor indicator). **Stage 4 sends actual imagery to the model, so expect this to bite much harder.** Handle
  a block as a first-class outcome, exactly like Stage 3 (`src/stage3/faillog.py`):
  - On HTTP 400 `content_filter` or a 200 with `finish_reason=content_filter`: set
    `safety={flag:"review", content_filtered:true}` + a `content_filtered` flag, write **no caption**, count
    it, and append `{file, asset path, timestamp, error}` to **`content-filter-failures.json`** (merges across
    runs). It is **not** an error and must not abort the run.
- **Governance (carry over from Stage 3 §7):** blocked imagery is the **CSAM/minors quarantine bucket** →
  human review + any mandatory-reporting process. **Minors-related content is hard-blocked regardless** and
  must stay quarantined — **do not** pursue an exemption to auto-caption it. Adult high-severity recovery
  needs the Azure **Limited Access "modified content filter"**. Set the deployment's filter to **"Lowest
  blocking"** and **Prompt Shields Off** (they false-positive on evidence) — the most permissive setup short
  of Limited Access. `safety.flag` is **`ok | review` only** (no `block`): sv‑kb policy is *label, don't block*.

---

## 6. Cost & DPI (gpt‑5‑mini — verify pricing at build time)

- **Image tokens:** a full US-Letter page bills **~2,490 tokens** (image patches cap at **1,536 × 1.62
  multiplier**). **Downsampling 200→150 DPI saves nothing** — both exceed the 1,536-patch cap and rescale to
  it; you must go **below ~130 DPI** to cut tokens (legibility risk on dense/handwritten pages). Artifacts are
  smaller (fewer patches).
- **Pricing (original gpt‑5‑mini):** ~$0.25 /1M input, ~$0.025 /1M cached, ~$2.00 /1M output. As of mid-2026
  OpenAI lists **versioned successors** (e.g. `gpt‑5.4‑mini` ≈ $0.75/$4.50) — **re-check the live price** for
  whatever deployment you use; it can be ~3× the original.
- **Estimates (Option A, whole corpus, input-dominated; output adds on top):** DS‑09-scale ≈ **~$130 realtime
  / ~$65 batched**; the **Tier-1 pre-gate removes ~30%** of image-page volume; DS‑10 is several× larger.
- **Batch API = 50% off** and is the right tool for an offline 130k+-asset job — **but** Azure caps a batch at
  ~100k requests and **multi-job request-chunking is NOT implemented** (see `src/stage3/batch.py` and the
  strategy's "Batch mode … needs request-chunking" gap). Either implement chunking for Stage 4 or run realtime.

---

## 7. Architecture — reuse Stage 3, don't reinvent

Mirror `src/stage3/` as `src/stage4/`. Concrete reuse:

| Need | Reuse from Stage 3 | Stage-4 change |
|---|---|---|
| Azure creds/connection | `connection.py` | add vision + AI-Vision + Content-Safety endpoints/keys |
| LLM client (chat/reasoning auto-detect, backoff) | `client.py`, `ratelimit.py` | send image content blocks; reasoning shape for gpt‑5‑mini |
| Structured-output prompt | `prompts.py` | new vision prompt + `json_schema` (caption/description/content_class/has_visual_content/safety) |
| Walk / resume / manifest / per-subfolder | `walk.py` | **iterate `image_assets[]`, not pages**; resume = skip assets that already have a `caption`/`stage4` |
| Content-filter failures log | `faillog.py` | same format, asset-level |
| Batch | `batch.py` | implement multi-job chunking if you want the 50% discount at scale |
| CLI / config | `cli.py`, `config.py` | flags: `--dry-run`, `-j`, `--deployment`, `--content-filter-log`, `--skip-pregated`, `--no-embeddings`, `--no-content-safety` |

**Iteration unit differs from Stage 3:** Stage 3 iterates *pages*; **Stage 4 iterates image *assets***
(image pages + artifacts + escalated pages) and reads the PNG at each asset's `path`. Resume granularity is
per-asset (skip if it already has a Stage‑4 verdict), so a crash loses at most the in-flight assets.

---

## 8. Execution environment & ops (THIS machine — the Stage‑3 docs show the OLD one)

⚠️ **The Stage‑3 execution doc's paths are the OLD machine** (`D:\Carlo\…`, `home/carlo`, `doc-scanner`
conda). On **this** machine:

- **Data lives on `C:` (the SSD) — never process on `D:`** (`D:` is a slow SMR HDD and only holds the
  accidental copies). Datasets: `C:\Projects\Data\epstein\DataSet-NN\VOL000NN\IMAGES\…`. DS‑09 is **flat**
  (`IMAGES\*.pdf`); DS‑10/11/12 are **nested** (`IMAGES\NNNN\*.pdf`).
- **Interpreter:** native Windows `C:\Users\carlo\anaconda3\python.exe` (PyMuPDF **1.27.2** pinned — though
  Stage 4 needs `requests`/`openai`-style HTTP + base64, not necessarily fitz). Windows-local package copy at
  `C:\epstein-ingest\src` (run with `PYTHONPATH=C:\epstein-ingest`), or import the repo over the WSL UNC path.
- **Creds:** `sv-kb/.env.local` (extract just the needed vars with the `get()` snippet from the Stage‑3 doc §2;
  **never print/commit the key**). **Verify the `.env.local` location on this machine** (`home/cortega`, not
  `home/carlo`) and **add the gpt‑5‑mini / AI-Vision / Content-Safety entries** if absent.
- **Large datasets → per-subfolder loop** over `IMAGES\NNNN` (DS‑10/11) to avoid the multi-minute `find_pdfs`
  walk and to get cheap resumes — same pattern as Stage‑3 §5c. DS‑09 is flat; chunk by Bates ranges if needed.
- **Idempotent + resumable**, keep the machine awake (`powercfg /change standby-timeout-ac 0`), scheduled-task
  + backstop for long unattended runs (reuse the DS‑09 Stage‑2 pattern in memory `dataset09-stage2-run`).
  Confirm **exactly one** worker before relaunching; back-off handles 429s.
- **Dry-run first** (`--dry-run`): it should report the eligible-asset count (after Tier-1 pre-gate) and a
  projected token/$ cost — verify before spending.

---

## 9. Sidecar schema additions (exact)

Per `image_assets[]` entry: the §2 fields (`caption`, `description`, `content_class`, `has_visual_content`,
`vision_confidence`, `safety`, `embedding_ref`, `prefilter_hint`, optional `content_filtered`). Plus a
top-level block:

```jsonc
"stage4": {
  "tool_version": "…", "generated_at_utc": "…",
  "vision_model": "gpt-5-mini", "vision_deployment": "…", "api_version": "2025-04-01-preview",
  "embedding_model": "…", "content_safety": "azure-content-safety",
  "config": { /* echoed flags incl. pregate thresholds */ },
  "counts": { "assets": N, "captioned": …, "pregated_blank": …, "has_visual_content_true": …,
              "content_filtered": …, "safety_review": …, "errored": … },
  "usage": { "input_tokens": …, "output_tokens": …, "calls": …, "estimated_cost_usd": …, "batch": false }
}
```
Run manifest `stage4-run-manifest.json` at the run root (clean-finish marker, like other stages) + the merged
`content-filter-failures.json`.

---

## 10. Acceptance criteria

1. **One bundled vision call per non-pregated asset**; resume skips assets already carrying a Stage‑4 verdict.
2. **`has_visual_content` is correct on a spot-check** — verify it is `false` on the known DS‑09 blanks
   (`C:\epstein-ingest\samples\A*`/`sample1,4,5,6,7`) and `true` on the real photos (`B*`, `sample2/3`). Use
   the rendered PNG as the oracle, as in prior stages.
3. **Content-filter blocks are handled gracefully** (flagged `review` + `content_filtered`, logged, no crash);
   minors-indicated content stays quarantined.
4. **Content Safety runs on artifacts**; image embeddings produced only for `has_visual_content=true`.
5. **Local only** (no Azure Storage / DB writes); **idempotent** (re-run is a no-op on done assets);
   fault-isolated (a bad PNG/API error skips that asset with a manifest entry, never aborts).
6. **Cost within the §6 estimate**; dry-run cost matches actuals within reason.

---

## 11. Open questions / decisions for the builder

- **Pre-gate placement:** compute Tier-1 in Stage 2/escalate (persist `prefilter_hint` + provisional
  `has_visual_content=false`) vs. at Stage-4 startup. Either works; doing it in Stage 2/escalate means the
  cost preview is right and Stage 4 just honors it. (Recommend: Stage-4 startup for now, so Stage 2 isn't
  re-run; revisit.)
- **Embedding storage:** inline vector vs sibling file vs deferred to Stage 6. Align with how Stage 6 stores
  text vectors so Stage 7 ingest is uniform.
- **`mixed` pages:** their figure is already an artifact in `image_assets` (vision-ready). Decide whether to
  also caption the full mixed *page* (we chose **low_quality_text only** for escalation; mixed full-page is
  optional).
- **Batch vs realtime:** implement multi-job batch chunking (50% off, big at DS‑10 scale) or accept realtime.
- **Limited Access application** for the adult `sexual:high` bucket (carried over from Stage 3) — product/legal
  decision, not a code one.

---

## 12. References

- **Strategy:** `STRATEGY-pdf-ingestion-stages.md` → "Update — DataSet-09 Stage 2 + vision-routing decisions
  (2026-06-18)" (over-inclusive routing, 0.9778 blanks, `has_visual_content`, caption+description, cost/DPI).
- **Stage 2:** `HANDOFF-stage2-render-classify-register.md` (produces `image_assets[]`; `--escalate-vision` /
  `--purge-staged-pages`); code `src/stage2/escalate.py`, `process.py`, `classify.py`.
- **Stage 3:** `HANDOFF-stage3-text-summaries.md` (build) + `HANDOFF-stage3-execution.md` (Azure/content-filter
  ops); code `src/stage3/{connection,client,prompts,walk,faillog,ratelimit,batch}.py` — the templates to mirror.
- **Memory:** `dataset09-stage2-run` (the full DS‑09 flow + escalate/purge + vision-workload findings),
  `stage1-execution-env`, `dataset09-stage1-run`.
- **DS‑09 investigation scripts & rendered evidence** (calibration): `C:\epstein-ingest\{sample_pagekind,
  sample_imagepages,tally_imagereasons,sample_classes,probe_blankmetric,probe_falseneg,probe_text_pixels,
  tally_optionA}.py` and the PNGs in `C:\epstein-ingest\samples\`.

**Bottom line for the builder:** the captioner is the easy part. The value of Stage 4 is the **`has_visual_content`
gate** (so junk never becomes an asset) and **robust content-filter/CSAM handling** (so the run survives the
evidence). Build those two first, reuse the Stage‑3 Azure plumbing, gate cheaply before you pay for vision,
and bring DS‑10/11/12 to DS‑09's uniform Stage‑2 end-state before you start.
```
