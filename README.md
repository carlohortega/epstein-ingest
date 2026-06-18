# epstein-ingest

A staged PDF ingestion pipeline (sv-kb). Each stage is a self-contained subpackage of the
`src` package with its own CLI — **stage1** (text-first pre-extraction, below),
**stage2** (render / classify `page_kind` / register image assets), **stage3** (nano summaries +
safety triage). The sections below cover each stage in order.

## Stage 1 — text-first pre-extraction

A **deterministic, offline** tool that
walks a folder of PDFs and writes a sidecar `<name>.json` next to each `<name>.pdf`, recommending — per
page — whether the page can be ingested from its **embedded text layer** (cheap, ~$0) or must still go
to **vision/OCR** (redacted, garbage, or image-dominant pages).

At multi-million-page scale, taking the text lane where it is safe saves five-to-six figures of vision
cost. The cost win and the **redaction-leak risk are resolved together**: the same per-page analysis
that says "text is sufficient" is the one that detects redaction and forces vision when in doubt.

Spec: `docs/HANDOFF-text-first-pdf-extraction.md`. This implements **Stage 1** (the extractor).
Stage 2 (the emulator that loads JSON → pipeline tables) is a contract only — see §11 of the spec.

## Install / run

The only required dependency is **PyMuPDF** (`fitz`), already present in the sv-kb venv
(`~/repos/sv-kb/pipeline/shared/.venv`, PyMuPDF 1.27.2). `pymupdf4llm` is optional (Markdown for
born-digital pages; the tool runs fully without it).

```bash
# via the convenience wrapper (uses the sv-kb venv automatically)
./run.sh ~/data/epstein/DataSet-12/VOL00012 -j 6

# or directly
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python -m src.stage1 <ROOT> [flags]
```

Key flags: `-j/--workers N` (default CPU-2), `--force` (re-extract), `--generated-at ISO8601`
(fixed timestamp for byte-reproducible runs). Every threshold (§6/§7/§8/§9) is a `--flag` and is
echoed into each JSON's `generator.config`. `--help` lists them.

Output per PDF: a sibling `<name>.json`. At the root: `extract-run-manifest.json` (the operator's
go/no-go artifact: files seen/extracted/skipped/errored, aggregate page counts, versions, wall-clock).

## What it does, per page (exact order — Spec §4)

1. **Render-free analysis** — `get_text`, `get_texttrace`, `get_images`, `get_image_info`,
   `get_drawings`, page rect/rotation.
2. **Redaction scrub (§7)** — find opaque occluder rects (by fill luminance) and the invisible OCR
   layer (render mode 3 / opacity 0); one low-DPI grayscale render decides which glyphs sit *under
   dark* regions. Render is skipped entirely when there are no occluders and no invisible glyphs.
3. **Text reconstruction (§6)** — **scrub-before-assembly** at the span level, so occluded/leaked
   words never reach output. Adaptive on the detected `text_layer_type`:
   - `ocr_layer` / `mixed` → `structured_text`: reading-order text with only geometry-supported
     structure (paragraphs, columns, lists). **No fabricated headings/emphasis** — the OCR layer's
     typography (16–19 noisy font sizes/page) carries none.
   - `born_digital` → `markdown` (via `pymupdf4llm` when installed; otherwise the same structured path).
   - `none` → empty content.
4. **Metrics** on the scrubbed text. 5. **Quality gate (§8)**. 6. **Routing (§9)** → `needs_vision`.

## Routing (`needs_vision`, first match wins — §9)

1. redaction suspected → `redaction_suspected`
2. too few chars → **blank/slip-sheet gate**: render once at low DPI, measure coverage at both extremes;
   a near-uniform page — stamp-on-white (`ink_coverage`≈0) **or** solid-black scanner blank (`dark_coverage`≈1)
   → `blank` (skip vision — nothing to read); otherwise → `low_text`
3. quality gate fails (garbage OCR / microfilm) → `quality_fail`
4. mid-size embedded figure with little co-located text → `has_figures`
5. otherwise → `text_sufficient` (the page IS its OCR; skip vision)

The blank gate is **conservative and symmetric** (silent content loss is worse than a wasted vision call): only
pages essentially uniform at one extreme are caught — any real marks (a few words on white, or light text on a
dark frame) break the uniformity and route back to vision. White blanks flag `blank_page`/`slip_sheet`, all-dark
blanks flag `blank_black`. `metrics.ink_coverage` and `metrics.dark_coverage` are emitted on every low-text page
so the threshold (`--blank-ink-max`) can be calibrated against a real volume. `metrics.bates_stamp` captures the
page's Bates control number where present (useful for citation even on blanks).

## Safety properties

- **Fail-safe redaction (positive bar detection)**: redaction bars — solid dark rectangles, vector or
  rasterized — are detected directly by scanning the render, then verified on their **interior** (pure black
  vs. a dark photo's gray) so photos aren't false-flagged. Each bar's text is dropped from `text.content` and
  replaced with a `[REDACTED]` marker at its position; the page is flagged `redaction.suspected=true` → vision.
  Detection is independent of under-bar text, so a redaction with nothing OCR'd under it is still caught/marked.
- **Deterministic**: identical input ⇒ byte-identical JSON except the caller-supplied `generated_at_utc`.
- **Idempotent/resumable**: skips a PDF whose sidecar exists and whose recorded `source.sha256` matches.
- **Fault-isolated**: corrupt/encrypted PDFs get a `status:"error"` sidecar; the walk continues.
- **Offline**: reads PDFs, writes JSON. No network, Azure, or DB. Dependency surface = PyMuPDF + stdlib.
- **Sensitive output**: sidecars contain document text (incl. names). Store them alongside the PDFs
  under the same access controls; never copy off-box casually (Spec §13).

## Tests

```bash
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python tests/run_tests.py
```

Synthetic fixtures (`tests/make_fixtures.py`) exercise every path — born-digital, OCR layer,
**redaction leak** (invisible tokens under a black bar; asserts the secret token is scrubbed),
microfilm garbage, figure pages, plus encrypted and corrupt PDFs — and verify determinism, idempotency,
and the manifest. `tests/_demo.sh` runs the CLI end-to-end and dumps a few sidecars.

## Stage 2 — render pages, classify `page_kind`, register image assets

The staged-ingestion Stage 2 (`src.stage2`, spec:
`docs/HANDOFF-stage2-render-classify-register.md`) consumes the Stage-1 sidecars and, **fully
offline**, renders each page to PNG, deterministically classifies it, extracts embedded photo artifacts,
makes one thumbnail per PDF, and **extends the same sidecar** with an image-asset manifest. No Azure, no
DB, no LLM — it just stages the local inputs Stage 4 (vision) and Stage 7 (ingestion) consume.

```bash
# run AFTER Stage 1 (needs the <name>.json sidecars in place)
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python -m src.stage2 <ROOT> [flags]
```

Per page, **deterministic `page_kind`** (first match wins — Handoff §4): `blank` → `redacted` (bars cover
≥ `--fully-redacted-bar-frac` of the page **and** no readable text remains) → `image` (full-page image,
low text) → `mixed` (text page + mid-size figure) → `text`. **Only `image` pages go to vision** — a
redacted page that still has a usable text layer is `text`/`mixed`, not `image`, which is the corpus-wide
cost win. (`page_kind` recomputes the visible, marker-excluded char count itself, because Stage-1's
`text.char_count` includes the inserted `[REDACTED]` markers.)

`page_kind` keys on char-count/coverage/redaction only — it does **not** re-run Stage 1's §8 quality
gate. A page Stage 1 rejected as garbage OCR (`quality_fail`) but with ≥ `min_chars` therefore stays in
the cheap text lane; it is kept there (no vision) but **flagged `low_quality_text`** (and tallied as
`text_quality_suspect`) so Stage 6/7 ingestion can decide whether to trust or escalate it.

Output per PDF, beside `<name>.pdf` / `<name>.json`:
```
<name>/images/page-<NNNN>.png       page_kind=image renders (the image assets)
<name>/pages/page-<NNNN>.png        text/mixed/blank/redacted display renders (defer with --no-stage-pages)
<name>/artifacts/page-<NNNN>-img-<MM>.png   embedded photos, deduped by content hash
<name>/thumbnail.png                one per PDF
```
Sidecar gains per-page `page_kind` + `render_path` + `render_dpi`, and top-level `thumbnail_path`,
`image_assets[]` (image pages + kept artifacts; the manifest Stage 4 iterates), and a `stage2` block
(versions, echoed config, counts). Renders are **200 DPI** (`--render-dpi`); a DPI clamp only lowers that
for abnormally large-format pages to stay under `--max-render-pixels` (OOM guard). At the root:
`stage2-run-manifest.json`, including the **vision workload** = image pages + kept artifacts (the Stage-4
cost preview). Idempotent/resumable (skips a PDF whose sidecar already carries a complete `stage2` block;
`--force` redoes); byte-deterministic renders + JSON.

```bash
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python tests/run_stage2_tests.py
```

## Stage 3 — text summaries + safety triage (Azure OpenAI)

Stage 3 (`src.stage3`, spec: `docs/HANDOFF-stage3-text-summaries.md`) is the **first
stage that calls an LLM and the first that costs money**. For every `text`/`mixed` page (Stage 2's
`page_kind`) it makes **one bundled call** that returns a faithful `summary` + structured `summary_json`
(key_points / entities / topics) + a `text_safety` triage flag, then rolls the page summaries up into a
**provisional** document summary (Stage 5 finalizes once image captions exist). It still writes **only the
local sidecar + run manifest** — no Azure Storage, no DB (those are Stage 7).

```bash
# realtime (after Stages 1–2); connection comes from the environment (.env.local)
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python -m src.stage3 <ROOT> [flags]

# project tokens/cost from existing sidecars with NO API calls:
python -m src.stage3 <ROOT> --dry-run
# bulk back-catalog via the Azure Batch API (~50% cheaper, async ≤24h):
python -m src.stage3 <ROOT> --batch
```

Connection (env; the api **key is never written to disk**): `AZURE_OPENAI_ENDPOINT`,
`AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_CHAT_API_VERSION`, `AZURE_OPENAI_CHAT_NANO_DEPLOYMENT`. Point the last
at a **`gpt-4.1-nano`** deployment (cheapest, deterministic with `temperature=0`+`seed`). The client
**auto-detects the model family** (`param_style=auto`): `gpt-4.1-nano` → chat shape (`max_tokens`/
`temperature`/`seed`); a `gpt-5`/o-series deployment → reasoning shape (`max_completion_tokens` +
`reasoning_effort`). So Stage 3 runs against whatever the deployment actually is — but gpt-4.1-nano is ~6×
cheaper than gpt-5-mini and reproducible, so prefer it (override per-run with `--deployment`).

**Safety is a triage gate, not a verdict.** `text_safety.flag` is `ok` (no downstream check) or `review`
(escalate to Azure Content Safety `text:analyze` on that page only). Per sv-kb policy we **label, we do not
block** — there is no `block` value. The optional `categories` hint aligns to the Content Safety taxonomy
(`hate|sexual|violence|self_harm|csam_suspected`) but is advisory; downstream does the real 0–7 scoring.
Redactions are respected: the model is instructed never to reconstruct text under `[REDACTED]`.

**Content-filter blocks are themselves a safety signal.** Azure's content-management policy can reject a
prompt (HTTP 400 `content_filter`) or empty a completion (`finish_reason=content_filter`) on disallowed
material. Stage 3 treats that not as a generic error but as a decisive escalation: the page gets
`text_safety={flag:"review", content_filtered:true}` + a `content_filtered` flag (no summary — the model
produced none), is counted under `content_filtered`, and is appended to **`content-filter-failures.json`**
(file / page / timestamp / error; merges across runs, dedup on file+page). Path overridable with
`--content-filter-log` (default `<ROOT>/content-filter-failures.json`). Operational notes on the Azure
filter (per-deployment severity sliders — "Lowest blocking" = block high only; the separate **Prompt
Shields / jailbreak** toggle, a frequent false-positive on evidence OCR; and that genuine high-severity
sexual content needs the Limited Access *modified content filter* approval, with minors-related content
hard-blocked regardless) live in `docs/HANDOFF-stage3-text-summaries.md`.

**Cost is measured, not assumed.** Every summarized page records `summary_json.input_token_count` /
`output_token_count`; the per-doc `stage3.usage` and the root `stage3-run-manifest.json` sum tokens + an
`estimated_cost_usd` from configurable rates (default `$0.10`/1M in, `$0.40`/1M out; batch ×0.5). The stage
is I/O-bound, so it uses a bounded **thread** pool + a shared rate limiter with 429/5xx backoff (not the
Stage 1–2 process pool), streaming a document window so a 300k-page corpus never buffers in memory.
Idempotent/resumable (skips pages/docs already done; `--force` redoes) and fault-isolated (a genuinely
failed call marks that page `summary_error` and the run continues; a content-filter block becomes `review`
as above). Default `--skip-low-quality-text` drops Stage 2's `low_quality_text` pages to cut spend (flip
with `--no-skip-low-quality-text`).

```bash
PYTHONPATH=. ~/repos/sv-kb/pipeline/shared/.venv/bin/python tests/run_stage3_tests.py   # offline (fake client)
```

## Layout

```
src/
  __init__.py      pipeline overview (package marker)
  stage1/          Stage 1 — deterministic, offline text-first pre-extraction
    config.py        all tunable thresholds (echoed into generator.config)
    classify.py      page_class — verbatim mirror of pipeline pdf.py _classify
    redaction.py     §7 occluder + invisible/under-dark detection, should_scrub predicate
    reconstruct.py   §6 layer-type detection + adaptive text reconstruction
    quality.py       §8 quality gate
    images.py        §5/§10 embedded-image inventory (references only, no byte extraction)
    page.py          §4 per-page orchestration → page record
    document.py      per-PDF → sidecar object; sha256; error isolation
    walk.py          folder walk, idempotency, parallelism, run manifest
    cli.py           argparse entry point (python -m src.stage1)
  stage2/          Stage 2 — render / classify page_kind / register image assets
    config.py        Stage2Config thresholds (render DPI, thumbnail, artifact min, redacted-bar frac)
    classify.py      deterministic page_kind decision (Handoff §4)
    render.py        DPI clamp, deterministic page/thumbnail PNG renders, PNG-header dim reader
    artifacts.py     embedded-photo extraction + content-hash dedup
    process.py       per-PDF: classify + render + extract + extend sidecar
    walk.py          folder walk, idempotency, parallelism, stage2 run manifest
    cli.py           argparse entry point (python -m src.stage2)
  stage3/          Stage 3 — gpt-4.1-nano page summaries + safety triage (Azure OpenAI)
    config.py        Stage3Config knobs (model/determinism, truncation, rates, concurrency)
    prompts.py       byte-identical system prompts + strict JSON schemas (caching) + message builders
    connection.py    Azure connection from env (key is env-only, never written)
    client.py        realtime nano client (requests + retry/backoff), usage/cost helpers, dual model shape
    ratelimit.py     thread-safe sliding-window RPM/TPM limiter
    summarize.py     routing, truncation, normalization, doc-summary reduce, safety rollup, cost accounting
    process.py       per-doc gate, DocState, finalize (content-filter → review), extends sidecar
    walk.py          realtime concurrency scheduler, dry-run preview, stage3 run manifest
    batch.py         Azure Batch API path (two rounds: pages then doc summaries) + resume state
    faillog.py       content-filter-failures.json writer (merge/dedup across runs)
    cli.py           argparse entry point (python -m src.stage3)
```
