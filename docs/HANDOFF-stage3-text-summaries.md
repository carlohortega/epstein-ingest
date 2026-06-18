# Handoff: Stage 3 — Text summaries (`gpt-4.1-nano`) + per-page safety triage

**Status:** ✅ **implemented 2026-06-17** (`src/stage3/`, package `src`, repo `~/repos/epstein-ingest`).
Validated live on DataSet-12; DataSet-11 (~331k docs) run in progress. The body below is the original
requirements; the amendments box records where the implementation deliberately diverged.

> ### Amendments — decisions made during implementation (2026-06-17)
> 1. **Safety enum is `ok | review` only — the `block` value below was dropped.** Per the sv-kb policy
>    (`pipeline/shared/svkb_pipeline/content_safety.py`: *"we LABEL, we do not block — unsafe material is
>    often the evidence"*), Stage 3 only triages whether a page needs the downstream Azure Content Safety
>    `text:analyze` scan. Wherever this doc says `ok | review | block`, read `ok | review`. The optional
>    `categories` hint is kept (aligned to `hate|sexual|violence|self_harm|csam_suspected`).
> 2. **Azure content-filter blocks are handled as a decisive `review` signal, not an error.** A prompt
>    rejected by the content-management policy (HTTP 400 `content_filter`) or an emptied completion
>    (`finish_reason=content_filter`) sets `text_safety={flag:"review", content_filtered:true}` + a
>    `content_filtered` page flag (no `summary`), is counted under `content_filtered`, and is logged to
>    **`content-filter-failures.json`** (file/page/timestamp/error; merges across runs). Flag/path:
>    `--content-filter-log`. Module: `src/stage3/faillog.py`. (On DataSet-12 the irreducible blocked set was
>    genuine `sexual:high`, ~84% with minor indicators → the CSAM-quarantine bucket; recovering adult
>    high-severity pages needs the Azure Limited Access *modified content filter* approval, minors never.)
> 3. **Model deployment is auto-detected.** The deployment name comes from `AZURE_OPENAI_CHAT_NANO_DEPLOYMENT`.
>    `gpt-4.1-nano` → chat request shape (`max_tokens`+`temperature=0`+`seed`, deterministic, preferred);
>    a `gpt-5`/o-series deployment → reasoning shape (`max_completion_tokens`+`reasoning_effort`). Force with
>    `param_style`. gpt-4.1-nano is ~6× cheaper than gpt-5-mini for this text-only workload.
> 4. **Repo/layout:** lives at `~/repos/epstein-ingest`, package `src` (not `text_first_extract`); Stage 1 is
>    in `src/stage1/`. Invoke `python -m src.stage3`. (Paths in the body still say `~/text-first-extract/` /
>    `text_first_extract/stage3/` — read as `~/repos/epstein-ingest/` / `src/stage3/`.)

**Goal:** for each PDF already processed by Stages 1–2, call **`gpt-4.1-nano` once per `text`/`mixed` page**
to produce a **page summary + structured `summary_json` (key_points/entities/topics) + a `text_safety`
triage flag** — all in a single bundled call — then roll those up into a **provisional document summary**.
Extend the sidecar JSON in place. The model runs on **Azure OpenAI** (this is the first stage that calls
an LLM and the first that costs money), but Stage 3 **writes only to the local sidecar** — no Azure Storage
uploads, no DB writes (those are Stage 7).

**Context:** companion to [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md) (§2 model
table, §4 stages, §6 JSON evolution, §7 safety) and the upstream handoffs
[`HANDOFF-text-first-pdf-extraction.md`](HANDOFF-text-first-pdf-extraction.md) (Stage 1) and
[`HANDOFF-stage2-render-classify-register.md`](HANDOFF-stage2-render-classify-register.md) (Stage 2).
Implement in the existing `~/text-first-extract/` codebase as a new `text_first_extract/stage3/` subpackage
(mirroring `stage2/`), reusing its walk / idempotency / run-manifest patterns.

---

## 1. Scope

**In scope:**
- For every **`page_kind == text` or `mixed`** page (Stage 2 wrote `page_kind`), one **bundled `gpt-4.1-nano`
  call** returning `summary` + `summary_json` + `text_safety`.
- A **provisional** per-document summary rolled up from the page summaries (text/mixed pages only).
- Extend the sidecar: per-page `summary` / `summary_json` / `text_safety`; top-level `document_summary`
  (provisional) + a `stage3` provenance/counts block.
- **Token + cost accounting** in the run manifest (this is a paid stage — measuring spend is a requirement).
- Idempotent, resumable, fault-isolated. Azure OpenAI calls only; results land on local disk.

**Out of scope (other stages):**
- **Image** captions / visual descriptions / image safety — `image` pages and photo artifacts go to **Stage 4**
  (`gpt-5-mini` vision + Azure AI Vision + Azure Content Safety).
- **Final** document summary that incorporates image captions — **Stage 5**.
- Chunking / embeddings — **Stage 6**. Azure Storage uploads / Postgres writes / events — **Stage 7**.
- The precise Azure **Content Safety** *text* scan itself is **not run here**; Stage 3 only emits the triage
  flag that decides whether a page should be escalated to it (see §6).

---

## 2. Inputs

- The PDF `<dir>/<name>.pdf` and its sidecar `<dir>/<name>.json` with **`status == "ok"` AND a complete
  `stage2` block** (Stage 3 needs `page_kind`). Skip and record any sidecar missing/!ok/without `stage2`.
- Per page Stage 3 reads **`pages[].text.content`** (verbatim, includes `[REDACTED]` markers),
  **`pages[].page_kind`**, and **`pages[].flags`** (notably `low_quality_text` from Stage 2, see §4).
- Config (§8), echoed into the sidecar's `stage3.config` like the earlier stages.
- Azure OpenAI connection: endpoint, **deployment name** for `gpt-4.1-nano`, api-version, and auth (API key
  or managed identity) — supplied by environment/config, **never written into the sidecar**.

---

## 3. The bundled nano call (the heart of this stage)

**One call per text/mixed page.** Use **structured outputs / JSON mode** (`response_format` json_schema) so
the result parses deterministically. Suggested response schema:

```jsonc
{
  "summary":  "string — 1–3 sentence faithful prose summary of THIS page's text",
  "key_points": ["string", "..."],          // 0–8 salient points
  "entities":  [{ "name": "string", "type": "person|org|place|date|other" }],
  "topics":    ["string", "..."],            // 0–6 short topic tags
  "text_safety": {
    "flag": "ok | review | block",           // triage; see §6
    "categories": ["string", "..."]          // e.g. ["sexual","violence","self-harm","csam-suspected"]
  }
}
```

Requirements for the call:
- **Faithful, extractive-leaning** summaries of the page's own text — **no outside knowledge, no speculation**,
  and **never reconstruct text under `[REDACTED]`** (treat redactions as unknown; do not guess names).
- **Deterministic-as-possible:** `temperature = 0` and a fixed `seed`; still not byte-reproducible (LLM), so
  Stage 3 **stamps provenance** (model, deployment, api-version, prompt-template version) instead of promising
  identical bytes. Same shared system prompt across all calls (enables **prompt caching**, §8).
- **Input truncation:** cap page text at `max_input_chars` (default ~24k chars ≈ ~6k tokens); if truncated,
  record `truncated: true` on that page's summary provenance. Pages are independent — no cross-page context.
- **Bundle, don't multiply:** summary, key_points, entities, topics, and the safety flag come back in this
  one call. No separate per-field or per-page safety call.

---

## 4. Page selection / routing

Per page, decided from the Stage-2 `page_kind` (no LLM needed to route):

| `page_kind` | Stage 3 action |
|---|---|
| `text` | summarize (bundled nano call) |
| `mixed` | summarize (the page text is canonical; the figure is handled as an image asset in Stage 4) |
| `image` | **skip** — no usable text; caption comes from Stage 4 vision |
| `blank` | **skip** — nothing to read |
| `redacted` | **skip** — only a footer of visible text; nothing to summarize |

- **`low_quality_text` pages** (Stage 2 flagged garbage-OCR text that still routed to `text`/`mixed`): default
  is to **still summarize** them but set `summary_json`/provenance `low_confidence: true` so Stage 6/7 can weight
  or quarantine them; `--skip-low-quality-text` flips this to skipping the call entirely (saves spend). Pick the
  default explicitly in §11.
- A page that already has a `summary` is **skipped** on resume (idempotency, §9).

---

## 5. Provisional document summary

- After a document's page summaries exist, make **one more nano call** to produce a **provisional**
  `document_summary` from the **concatenation of the page summaries** (text/mixed only), not the raw page text.
- **Large documents:** if the concatenated page summaries exceed `max_doc_summary_input_chars`, reduce
  **hierarchically** (summarize batches of page-summaries, then summarize those) — record the fan-in depth.
- Mark it **provisional** (`document_summary.provisional = true`): Stage 5 finalizes it once image-page captions
  exist. A document with **zero** text/mixed pages (all image/blank/redacted) gets `document_summary = null`
  (provisional) and is left for Stage 5.

---

## 6. Text safety triage + escalation

- The `text_safety.flag` is the **free-riding** safety signal for text pages (Strategy §7): `ok` (clean),
  `review` (potentially sensitive — escalate), `block` (model is confident it is disallowed content).
- Stage 3 **does not call Azure Content Safety**; it records the flag and a **doc-level rollup**
  (`document_summary.safety_rollup`) so an operator/Stage 4 can run the precise **Azure Content Safety *text***
  check on **only `review`/`block` pages** — a tiny fraction — instead of every page.
- This is the **text** half of the safety triad; the **image/CSAM** half (Azure Content Safety on photo
  artifacts + gpt-5-mini tags) is **Stage 4** and is **non-negotiable** in this domain — Stage 3 must not be
  read as the safety net for images.

---

## 7. Sidecar JSON extensions (Stage 3 writes — extends in place, never destructive)

Add to each **text/mixed** page object:
```jsonc
"summary": "Email from [REDACTED] to staff approving the Q3 operations budget…",
"summary_json": {
  "key_points": ["Q3 budget approved", "…"],
  "entities":   [{ "name": "Operations Committee", "type": "org" }],
  "topics":     ["finance", "operations"],
  "low_confidence": false,         // true when the page was low_quality_text or truncated
  "input_token_count": 612,        // REQUIRED: prompt tokens of THIS page's nano call (usage.prompt_tokens)
  "output_token_count": 138        // REQUIRED: completion tokens of THIS page's nano call (usage.completion_tokens)
},
"text_safety": { "flag": "ok", "categories": [] }
```
Add at the top level:
```jsonc
"document_summary": {
  "text": "Provisional summary of the document's textual pages…",
  "provisional": true,
  "covered_pages": 183,            // text/mixed pages folded in
  "safety_rollup": { "review": 2, "block": 0, "pages_flagged": ["12","88"] }
},
"stage3": {
  "tool_version": "…",
  "model": "gpt-4.1-nano", "deployment": "…", "api_version": "…", "prompt_version": "…",
  "generated_at_utc": "…",
  "config": { /* echo of thresholds/knobs */ },
  "counts": { "pages_summarized": 183, "pages_skipped": 12, "low_confidence": 7,
              "safety_review": 2, "safety_block": 0, "doc_summary_calls": 1 },
  "usage": { "prompt_tokens": 412305, "completion_tokens": 49870, "calls": 184,
             "estimated_cost_usd": 0.061, "batch": true }
}
```
- Field names align with the Stage-7 mapping (Strategy §6): `pages[].summary` → `document_pages.summary`,
  `pages[].summary_json` → `…summary_json`, `pages[].text_safety` → `…safety_tags`, `document_summary` →
  `documents`. `asset_context` records `model = gpt-4.1-nano`.
- `image`/`blank`/`redacted` pages are **not** given a `summary` (Stage 4/5 handle or skip them).

---

## 8. Model, cost & throughput requirements

- **Model:** `gpt-4.1-nano` via **Azure OpenAI** (deployment name from config). Text only; input-dominated.
- **Per-page token counts are required:** record each page's own call usage **under `summary_json`** as
  **`input_token_count`** (= `usage.prompt_tokens`) and **`output_token_count`** (= `usage.completion_tokens`).
  These per-page figures are the source of truth that the document/run totals in `stage3.usage` are summed from,
  and they let Stage 6/7 attribute cost per page. (For the provisional document-summary call, record its usage
  under `document_summary` similarly.)
- **Cost accounting is required:** capture `usage.prompt_tokens`/`completion_tokens` from every response, sum
  per document and per run, and compute `estimated_cost_usd` from **configurable rates** (default
  `gpt-4.1-nano`: **$0.10 / 1M input, $0.40 / 1M output**; cached-input rate lower). Surface totals in the run
  manifest — this stage's whole justification is that it is cheap, so the bill must be **measured, not assumed**.
- **Two cost levers, both first-class config:**
  - **Batch mode** (`--batch`): submit via the Azure OpenAI **Batch API** (~**50%** cheaper, async ≤24h window)
    for bulk back-catalog runs. Real-time mode for small/incremental runs.
  - **Prompt caching:** keep the system/instruction prompt **byte-identical** across all calls so cached-input
    pricing applies at scale (100k+ calls). Page text is the only varying part.
- **Throughput & limits:** bounded concurrency sized to the deployment's **TPM/RPM** quota; exponential backoff
  with jitter on `429`/`5xx`; never exceed quota. (Unlike Stages 1–2 this is **I/O/network-bound**, so the
  concurrency model is async/thread-pool with rate limiting, **not** a CPU process pool — but reuse the same
  bounded-window streaming discipline so a 300k-page corpus never buffers all tasks/results in memory.)
- **Truncation/skip controls** (`max_input_chars`, `--skip-low-quality-text`) bound per-call spend.

---

## 9. Automation requirements

- **Idempotent + resumable:** skip a page that already has a `summary`; skip a document whose `stage3` block is
  present and complete (unless `--force`). A crash loses at most in-flight calls; never re-summarizes done pages.
- **Fault-isolated:** a single failed/again-rate-limited call (after retries) marks that page
  `summary_error` (with reason) and continues — it never aborts the document or the walk. A document with no
  eligible pages is recorded and skipped.
- **Secrets:** Azure credentials come from env/config and are **never** written to the sidecar or manifest.
- **Run manifest:** `stage3-run-manifest.json` at the root — documents/pages seen / summarized / skipped /
  errored; `safety_review`/`safety_block` totals; **token totals + estimated cost** (the operator's spend
  report); model/deployment/prompt versions; batch vs realtime; wall-clock.
- **Provenance over determinism:** outputs are LLM-generated, so byte-determinism is **not** required; instead
  every summary/doc-summary is stamped with model + deployment + api-version + prompt-version (+ `seed`,
  `truncated`, `low_confidence`).

---

## 10. Acceptance criteria

1. **Only text/mixed pages are summarized;** image/blank/redacted pages get **no** `summary` (spot-check that
   `page_kind` gates the call). Vision-bound pages are untouched.
2. **One bundled call per page** yields a valid `summary` + `summary_json` + `text_safety` that conform to the
   schema (§3); no separate per-field or per-page-safety calls.
3. **Redaction respected:** no summary reconstructs text under `[REDACTED]`; redacted names never appear.
4. **Provisional document summary** present for any doc with ≥1 text/mixed page, marked `provisional:true`, and
   omitted/`null` for all-image/blank/redacted docs (left to Stage 5).
5. **Safety triage works:** `review`/`block` pages are flagged and rolled up at the document level; the run
   manifest counts them. (No Content Safety call is made here.)
6. **Idempotency:** re-running skips completed pages/docs; `--force` redoes them. **Fault isolation:** an
   injected API failure marks one page `summary_error` and the run completes.
7. **Cost is measured:** **every summarized page records `summary_json.input_token_count` and
   `output_token_count`** (per-page), and the manifest reports the prompt/completion token totals (summed from
   those) plus an estimated USD cost; batch mode reduces it ~50%. No Azure Storage/DB writes occur (only the
   local sidecar + manifest change).

---

## 11. Open questions / notes for the implementer

- **`low_quality_text` pages (§4):** default = summarize-but-mark-`low_confidence`; alternative = skip. Decide
  before bulk runs — on DataSet-12 this was ~16% of pages (251/1525); skipping them is a real spend cut but
  drops thin/garbled pages from retrieval. Recommended: **summarize + low_confidence** (keep recall; let Stage 6/7
  down-weight), with `--skip-low-quality-text` available.
- **`text_safety` enum:** `ok|review|block` proposed here; confirm against the sv-kb `safety_tags` /
  `content_safety_findings` schema so Stage 7 maps cleanly. Define category vocabulary explicitly.
- **Doc-summary reduction (§5):** the hierarchical fan-in for very large documents (e.g. the 195-page scans in
  DataSet-11) needs a concrete batch size + depth cap; bias toward a single reduce where the page-summary
  concatenation fits the context.
- **Batch vs realtime default:** recommend **batch** for the back-catalog (DataSet-10/11/12) and realtime for the
  growing-dataset incremental passes (matches the Stage-1 `--skip-existing` loop cadence).
- **Cost preview:** before a bulk run, the eligible-page count × avg page tokens gives a spend estimate from the
  existing sidecars (no API calls) — provide a `--dry-run` that prints projected tokens/cost from `char_count`.
