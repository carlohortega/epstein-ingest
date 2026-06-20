# Handoff: Stage 6 — Chunk for retrieval (chunk-only; embedding deferred to Stage 7)

**Status:** ✅ **BUILT + tested** (`src/stage6/`, `tests/run_stage6_tests.py`, launcher
`C:\epstein-ingest\run_stage6.py`). Validated on the **DS-12 canary** (152 docs → 1,727 parents /
3,824 children, ~1.3 s, $0); chunks verified **byte-identical** to an independent `chunk_pages(...)` and
each `content_sha` to the documented formula. **The two §1a.4/§11 fidelity calls were decided:**
`image_page_policy = "skip-no-text"` (skip pages with no text layer; do **not** inject captions — sv-kb
wouldn't) and `text_source = "stage1-scrubbed"` (keep Stage-1 `[REDACTED]` text; no re-extraction); both
are stamped into every `stage6` block. **DS-11 ≈ 331,655 docs is Stage-6-ready** (one local pass, minutes);
DS-09/DS-10 reach Stage 6 after their earlier LLM stages. Stages 1–5 are built and running on real data.
**Read first:** [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md) (§4 stage table row 6,
§6 sidecar evolution, the "Stage 6 — chunk with sv-kb's chunker" refinement, §8 deferred items), and the
**hard-requirement source you will reuse verbatim**:
`~/repos/sv-kb/pipeline/shared/svkb_pipeline/chunking.py`. The Stage 3/4/5 handoffs are the architectural
template; mirror their `src/stageN/` shape.

---

## 0. What Stage 6 does (30-second version)

Stages 1–5 produced, per document: verbatim page text (`pages[].text.content`), image
captions/descriptions (`image_assets[]`), and a **final** document summary (`document_summary_final`).
Stage 6 turns the document **body** into retrieval **chunks** by reusing sv-kb's production chunker
**verbatim** (`chunk_pages(...)`). The bar is **chunks indistinguishable from what sv-kb would produce** —
which means replicating sv-kb's input assembly exactly, not just calling the function (see §1a; two
divergences to decide: image-page transcription-vs-caption, and text-extraction parity).
It is **parent–child**: parent chunks (≈500/650 tok, *not* embedded — handed to the LLM at generation) and
child chunks (≈125/150 tok, embedded + retrieved), each child carrying a Contextual-Retrieval
`context_prefix` and a `content_sha`. Output is a **sibling `<name>.chunks.json`** (the exact artifact the
live `indexer-job` consumes) referenced from a new `stage6` block. Stage 6 **chunks only — it does NOT
embed** (embedding is deferred to Stage 7, §4). **No API calls at all** — chunking is deterministic + local,
so Stage 6 needs no Azure creds. Append-only, local-only, idempotent, resumable.

---

## 1. Core principles (requirements, not style)

- **Reuse the sv-kb chunker VERBATIM — hard requirement.** Call
  `svkb_pipeline.chunking.chunk_pages(...)` and serialize with `parents_to_dicts(...)`. Do **not** write a
  new chunker, do **not** fork/tune the token budgets, do **not** re-implement sentence packing. The whole
  point is that staged chunks are identical to live output so Stage 7 is a pure loader and search behaves
  the same. (The chunker is pure stdlib — `hashlib`/`re` — no extra dependency.)
- **Append-only / lanes immutable.** Stage 6 writes its own `stage6` block + a sibling `chunks.json` and
  **never** modifies Stage 1–5 fields.
- **Local only, no network.** Chunk-only Stage 6 makes **zero API calls** (no embedding — §4) and no Azure
  Storage / DB writes (that is Stage 7). Pure CPU + local file I/O. Idempotent + resumable.
- **Fidelity (Stage-1 ethos).** Chunk only the text that exists; `[REDACTED]` stays as-is (it's just text to
  the chunker); never fabricate. Child text is the clean display text; the `context_prefix` is metadata.
- **The output IS the live contract.** `parents_to_dicts(chunk_pages(...))` is exactly what the live
  `indexer-job` downloads and feeds to `parents_from_dicts(...) → index_document(...)`. Match that JSON shape
  exactly (it already does if you use `parents_to_dicts`).

---

## 1a. Fidelity to sv-kb — the exact replication recipe (the WHOLE point)

The requirement is **chunks indistinguishable from what sv-kb would have produced** for the same PDFs. The
chunker is deterministic, so identical output requires **identical inputs**. The litmus test is the child
**`content_sha = sha256(f"{embedding_model}\n{context_prefix}\n{text}")`**: if `embedding_model`,
`context_prefix`, or `text` differs from what sv-kb computes, the chunk is distinguishable **and** it misses
the shared embedding cache (re-embeds; won't dedup against live rows). So replicate
`svkb_pipeline/consolidate.py` **exactly** — not "in spirit":

1. **Per-page `md` assembly (copy `consolidate.py`):**
   - **text-layer page** → `md = f"## Transcription\n\n{text}"` — the **exact wrapper**, including the
     `## Transcription` heading (it is load-bearing: it sets `heading_path=["", "Transcription"]` and
     `Section: Transcription` in the prefix). Feeding bare text would change every child's heading_path *and*
     content_sha. Replicate the `{ocr_text or ''}` empty-string handling too.
   - **scanned/image page** → sv-kb feeds the page's **full vision-to-markdown transcription**
     (`vision_md_blob_path`). ⚠️ See gap (4).
2. **Call `chunk_pages` with sv-kb's exact kwargs.** `consolidate.py` calls
   `chunk_pages(pages, document_name=…, case_key=…, dataset_key=…)` — i.e. **`brief_description=""`** (default)
   and **`embedding_model="text-embedding-3-small"`** (default). **Do NOT pass the Stage-5 summary as
   `brief_description`** — sv-kb doesn't, so it would change every `context_prefix`/`content_sha` and make
   100% of our chunks distinguishable + break dedup. (Summary-in-prefix is the **deferred** §8
   LLM-`context_prefix`; sv-kb doesn't do it yet, so neither do we.)
3. **Identifiers feed `content_sha` → not cosmetic.** `context_prefix =
   "[Context: This excerpt is from {document_name}, {case_key}/{dataset_key}. Section: {section}.]"`. Confirm
   `document_name`/`case_key`/`dataset_key` against how the live `IngestionEvent`/`models.py` sets them for
   these datasets (likely `document_name=<pdf stem>`, `dataset_key=DataSet-NN`, `case_key=VOLxxxxx` —
   **verify; a wrong key silently makes every chunk distinguishable**).
4. **Two unavoidable divergence risks — decide explicitly (§11):**
   - **Image/scanned pages — the big one.** sv-kb chunks a **full vision markdown transcription**; our
     cost-optimized Stage 4 is **caption-only**. Feeding captions is *not* what sv-kb would do → image-page
     chunks are distinguishable. **Either (A) add a vision-to-markdown transcription** for image pages (a
     Stage-4 scope+cost change: one gpt-5-mini transcription per scanned page) to be truly indistinguishable,
     **or (B) accept image-page divergence** and document it. This is the single biggest Stage-6 decision.
   - **Text-extraction parity.** sv-kb's `ocr_text` is its `pdf.py` `page.get_text("text")`; our
     `pages[].text.content` is PyMuPDF text **with redaction scrubbing → `[REDACTED]`** (+ optional
     reconstruction). These can differ byte-wise. Keeping our safer text is defensible (never leak under-bar
     content) but is a deviation — **decide: re-extract to match sv-kb, or keep Stage-1 text + document it.**

---

## 2. Inputs — what Stage 6 reads and how it assembles page text

The chunker takes **`pages: list[tuple[int, str]]`** — `(page_number, md)` pairs — plus identifiers. Build
the page list from the frozen sidecar **in page order**, replicating `consolidate.py` (§1a), including only
pages with retrievable content:

| Page | `md` fed to the chunker — to MATCH sv-kb |
|---|---|
| `text` / `mixed` (text-layer) | **`f"## Transcription\n\n{pages[].text.content}"`** — sv-kb's exact wrapper (heading load-bearing); not bare text |
| `image` (scanned, `has_visual_content`) | sv-kb feeds the **full vision-to-markdown transcription**; our Stage 4 has only **caption+description** → **divergence, decide §1a(4)/§11** |
| `blank` / `redacted` / no content | **skip** (nothing to chunk) |

> **Chunk body = the page text (wrapped), not the page summary** — retrieval must hit the actual document
> language. The summary does **not** feed the chunk and (for sv-kb parity) does **not** feed
> `brief_description` either (§1a.2).

**Identifiers** (feed `content_sha` — match sv-kb, see §1a.3):
- `document_name` = PDF stem (e.g. `EFTA02730265`); `dataset_key` = e.g. `DataSet-11`; `case_key` = e.g.
  `VOL00011`. **Verify against sv-kb's `models.py`/`IngestionEvent`.**
- **`brief_description=""`** and **`embedding_model="text-embedding-3-small"`** — the defaults
  `consolidate.py` uses. Do not change them.

---

**Per-page, NOT stitched — and the markdown caveat (verified in sv-kb + our sidecars).** The live sv-kb PDF
path (`svkb_pipeline/consolidate.py`) builds **per-page** `(page_number, md)` and calls `chunk_pages`, which
chunks **each page independently** and tags `page_number`. There is **no cross-page stitching** — that's the
deliberate choice that preserves page-level citations (the whole-document `chunk_markdown` path is only for
non-paginated DOCX/HTML in `chunker-job`). We match that. **Separately:** the chunker's heading-aware
behavior (`heading_path`, heading-boundary parent splits) only activates on markdown `#` headings. In sv-kb
those come from the **vision job transcribing scanned pages *to markdown***; clean text-layer pages get none
(just `## Transcription\n\n{raw text}`). **Our input has no markdown:** Stage 1 stores
`text.format:"structured_text"` (plain text, no `#`), and Stage 4 is **caption-only** (not a markdown
transcription). So beyond the constant `## Transcription` wrapper we apply for parity (§1a — it yields a
fixed `Section: Transcription`, not real document structure), there are no document headings and the chunker
degrades to **token-budget sentence packing, per page** — exactly sv-kb's text-layer behavior. This is
acceptable: the real wins
(parent/child split, `context_prefix`, `content_sha` dedup, `page_number` citations) are **independent of
markdown**. Do **not** add stitching and do **not** fork the chunker for headings; the lever to regain
structure is the **input** (§11), and the bigger retrieval lever for this corpus is the Contextual-Retrieval
`context_prefix`, not heading-aware chunking.

## 3. The chunk call (reuse, don't rebuild)

```python
from svkb_pipeline.chunking import chunk_pages, parents_to_dicts

parents = chunk_pages(
    pages,                                   # [(page_number, "## Transcription\n\n<text>"), ...] — §1a/§2
    document_name=doc_name,                  # = pdf stem; case_key/dataset_key per sv-kb (§1a.3, VERIFY)
    case_key=case_key,
    dataset_key=dataset_key,
    # brief_description="" and embedding_model="text-embedding-3-small" are the DEFAULTS sv-kb uses.
    # Pass nothing else — matching consolidate.py exactly is the point (§1a.2).
)
chunks_json = parents_to_dicts(parents)      # write THIS to <name>.chunks.json
```

What the chunker guarantees (from `chunking.py`, do not re-derive):
- Budgets: parent target/max = 500/650 tok; child target/max = 125/150 tok; tokens estimated as
  `len(text)//4` (no tokenizer dependency — budgets are soft).
- Each `ParentChunk`: `text, heading_path, chunk_index, token_count, page_number, chunk_kind="text",
  children[]` (timestamp/speaker fields stay `None` for PDFs).
- Each `ChildChunk`: `text, context_prefix, content_sha, chunk_index, heading_path, token_count`. The child
  `content_sha = sha256(f"{embedding_model}\n{context_prefix}\n{text}")` — this is the **embedding
  cache/dedup key**; identical child text across the corpus shares one embedding.
- `chunk_index` is made continuous across pages within a document.
- Headings: the chunker splits markdown by `#` headings to populate `heading_path`. With the `## Transcription`
  wrapper (§1a), text-layer pages get `heading_path=["", "Transcription"]` (a constant section — matching
  sv-kb), not real document structure.

---

## 4. Embedding scope — DECIDED: chunk-only (no embedding in Stage 6)

**Stage 6 chunks only. It does NOT embed.** Embedding is deferred to **Stage 7** (decide the exact mechanism
then). Consequences — Stage 6 is **pure-local, no Azure calls, no creds required**: it produces
`chunks.json` (+ the `stage6` block) and stops.

Rationale: the live system already puts embedding in the **`indexer-job`**, which is the **single writer** of
`chunks`/`embeddings` and already embeds child chunks deduped by `content_sha`
(`svkb_pipeline/indexing.index_document` → `embed_in_batches`). So a `chunks.json` artifact is the natural,
sufficient hand-off; embedding at Stage 6 would duplicate the indexer and require re-implementing its
content_sha dedup locally. The strategy table's "Chunk + embed (staging)" wording is superseded by this
decision — Stage 6 is the **chunk** half; the **embed** half is Stage 7's call.

> **For whoever builds Stage 7** (reference, not a Stage-6 task): embedding env is
> **`AZURE_OPENAI_EMBED_DEPLOYMENT`** (default `text-embedding-3-small`), **`EMBED_DIMENSIONS`** (default
> 1536; large → 3072), model inferred (`"large" in deployment`), REST
> `/openai/deployments/{dep}/embeddings?api-version=2024-10-21`, `EMBED_BATCH_SIZE` (default 32),
> `dimensions` sent only when ≠ 1536. The `content_sha` on each child is the embedding cache/dedup key —
> which is exactly why §1a fidelity matters. Reuse `svkb_pipeline/embeddings.py` verbatim then; never write a
> new embedding client.

---

## 5. Sidecar schema additions (append-only — exact)

Add a **new** top-level `stage6` block and reference the sibling artifact (don't inline thousands of child
chunks into the sidecar — keep it lean, mirror the live `chunks_blob_path` artifact):

```jsonc
"stage6": {
  "tool": "text-first-extract-stage6", "tool_version": "1.0.0",
  "chunker_source": "svkb_pipeline.chunking.chunk_pages",
  "chunker_params": { "parent_target": 500, "parent_max": 650, "child_target": 125, "child_max": 150 },
  "embedding_model": "text-embedding-3-small",
  "generated_at_utc": "…", "config": { /* echoed flags */ },
  "chunks_ref": "<name>.chunks.json",         // sibling artifact (relative); the §6 staged file
  "counts": { "pages_chunked": N, "parents": P, "children": C, "skipped_pages": S }
}
```

The sibling **`<name>.chunks.json`** holds `parents_to_dicts(parents)` verbatim — the exact bytes the live
`indexer-job` consumes. A run manifest `stage6-run-manifest.json` at the run root (counts + usage + any
errors), mirroring Stage 3/4/5.

---

## 6. Preconditions / per-doc gate

Since `brief_description=""` (§1a.2), **Stage 5 is NOT a data dependency** of Stage 6 — the chunk inputs are
Stage 1 text (always) and, for image pages, the Stage 4 vision output (caption today, or a transcription if
§1a(4)-A is chosen). Gate on those; running after Stage 5 is only an ordering convention, not required:

```
status != ok                         -> skip (skipped_status)
no stage1 page text & no stage4      -> skip (nothing to chunk)
image_assets present and no stage4   -> skip (no_stage4)                # vision output missing for a vision doc
stage6 block present and not --force -> skip (skipped_done)             # idempotency
else                                 -> chunk
```

A doc whose pages are all blank/redacted (no chunkable content) still finalizes: write `stage6` with
`counts.parents=0` and an empty `chunks.json` (or `chunks_ref:null`) so it's marked done, not retried.

---

## 7. Architecture — mirror Stage 3/4/5

Mirror `src/stage5/` as `src/stage6/`. There is **no prompt module** (chunking is deterministic, no LLM):

| Need | Reuse / pattern | Stage-6 specifics |
|---|---|---|
| sv-kb chunker | `svkb_pipeline.chunking.chunk_pages` / `parents_to_dicts` | imported verbatim; put `~/repos/sv-kb/pipeline/shared` on `sys.path` |
| Walk / scheduler / manifest / dry-run | `src/stage5/walk.py` | per-**document** pool (chunking is CPU-cheap — a process pool may beat threads since there's no I/O wait), pruned PDF walk |
| Per-doc gate + finalize | `src/stage5/process.py` | the §6 gate + §5 append-only writes + sibling `chunks.json` |
| CLI / config | `src/stage5/{cli,config}.py` | flags: `--dry-run`, `-j`, `--force`. **No connection/`--endpoint`/`--deployment` flags — Stage 6 makes no API calls.** |

There is **no `connection.py` / client / ratelimit** in Stage 6 — it calls no Azure service. Launcher
`C:\epstein-ingest\run_stage6.py` mirrors `run_stage5.py` (puts the WSL repo on `sys.path`, calls
`src.stage6.cli.main`) **plus** adds `~/repos/sv-kb/pipeline/shared` to `sys.path` so
`import svkb_pipeline.chunking` resolves. It does **not** need to load `AZURE_*` creds (chunk-only).

---

## 8. Environment & operations (lessons learned — read before running)

**Machine reality (this is a different box than the original runbooks assume):**
- Repo: `\\wsl$\Ubuntu-22.04\home\cortega\repos\epstein-ingest` (WSL Ubuntu-22.04, user `cortega`).
- **sv-kb is now copied to `~/repos/sv-kb`** (chunker lives at `pipeline/shared/svkb_pipeline/chunking.py`).
  The `run.sh` reference to `~/repos/sv-kb/pipeline/shared/.venv/bin/python` **does not exist** here.
- Data on **C:** at `C:\Projects\Data\epstein\DataSet-NN\VOLxxxxx\IMAGES\NNNN\*.pdf` (+ sidecars). C: is
  NVMe; do not process on D: (slow SMR).
- **The chunker is pure stdlib** (`hashlib`/`re`) — Stage 6 (chunk-only) needs **no third-party deps and no
  Azure creds**. Either Python works: the WSL repo `.venv` (has PyMuPDF; used for the offline tests) or the
  native **`C:\Users\carlo\anaconda3\python.exe`** (3.12, what the launchers use for real runs).
- **Launcher pattern (use it, not bare `-m`):** `C:\epstein-ingest\run_stageN.py` puts the live WSL repo on
  `sys.path` (always current code); for Stage 6 it must **also** add `~/repos/sv-kb/pipeline/shared`. Unlike
  Stages 3–5 it does **not** need to load `AZURE_*` (chunk-only, no API). Invoke as
  `C:\Users\carlo\anaconda3\python.exe C:\epstein-ingest\run_stage6.py <ROOT> [flags]`.

**Run posture (from the Stage 5 runs):** Stage 6 chunking is **free + fast** — run a **single pass over the
whole dataset IMAGES tree** (one `stage6-run-manifest.json`), resumable (skip docs with a `stage6` block).
For a big set (DS-11 ≈ 331k) launch **detached** writing to a log (`Start-Process …
-RedirectStandardOutput`), then monitor the manifest/log — same as the DS-11 Stage 5 run. DS-12 (152 docs)
runs foreground in seconds. Dry-run first to get counts.

**Tooling gotchas (these cost real time — heed them):**
- The **Bash tool is Git Bash on Windows**, not WSL. Run Linux things via `wsl.exe -d Ubuntu-22.04 bash -lc
  '...'`, but that layer **mangles shell variable expansion** (`$var`, `$(...)`, `$$`, `for`-loop vars come
  through empty). Use **literal paths** and **one command per call**; don't rely on shell vars/loops across it.
- **Inside WSL, `C:` is `/mnt/c`, not `/c`** (a bundle write failed on exactly this). In Git Bash, `C:` is `/c`.
- Use the **PowerShell tool** for Windows-native python runs and filesystem checks; it's the cleanest for C:.
- **git push needs `gh`** (installed 2026-06-19) — `gh auth login` (browser device flow). HTTPS password
  auth is dead; GitHub Credential Manager creds on the Windows side were stale.

**Dataset state:** **DS-12** (152 docs, 1 folder) is **through Stage 6** (the canary run). **DS-11**
(331,655 docs, 332 folders) is fully through Stage 5 → **Stage-6-ready now** (one local pass, minutes; run
detached + `--progress-every`, dry-run first for counts). **DS-09** (~529k, flat) and **DS-10** (~503k, 504
folders) are at earlier stages (DS-10 through Stage 3; DS-09 through Stage 2) — they reach Stage 6 after
their Stage 3/4/5 complete. Manifest locations can vary per dataset (e.g., DS-12's
`stage4-run-manifest.json` sits at the `DataSet-12` root, not under `IMAGES`) — gate on per-sidecar
**blocks**, never on manifest presence.

---

## 9. Cost & scale

- **Chunking: $0** — Stage 6 makes no API calls. CPU-cheap (regex + sha256); the work is reading page text +
  writing one `chunks.json` per doc, like the Stage 5 sidecar rewrite (~560 docs/s observed there). DS-11
  (~331k docs) should run in minutes.
- **Embedding cost is a Stage-7 concern** (deferred, §4). For sizing later: text-embedding-3-small ≈
  `$0.02`/1M tok, child chunks ≈125 tok each, and `content_sha` dedup cuts repeated boilerplate materially.

---

## 10. Acceptance criteria

1. **Reuse:** chunks come from `svkb_pipeline.chunking.chunk_pages` + `parents_to_dicts` unchanged;
   `<name>.chunks.json` round-trips through `parents_from_dicts` (i.e., the live `indexer-job` can consume it
   as-is).
2. **sv-kb fidelity (§1a):** text-layer pages wrapped `f"## Transcription\n\n{text}"`; `brief_description=""`
   and `embedding_model="text-embedding-3-small"`; identifiers match sv-kb's conventions; page order preserved;
   `parent.page_number` set. Image-page handling matches the §1a(4) decision (transcription, or documented
   divergence). A spot-checked chunk's `content_sha` equals what sv-kb's `consolidate.py` would compute.
3. **Append-only:** Stages 1–5 fields byte-unchanged; new `stage6` block + sibling `chunks.json` written.
4. **content_sha integrity:** child `content_sha == sha256(f"{embedding_model}\n{context_prefix}\n{text}")`
   with `embedding_model="text-embedding-3-small"` and `brief_description=""` (NOT the summary).
5. **Gate:** skips non-ok / no-content / (vision doc) missing-stage4 / already-done; empty docs finalize with
   `parents=0` (not retried).
6. **Idempotent/resumable/local:** re-run is a no-op on done docs (`--force` redoes); a bad doc is isolated
   (skip + manifest entry, never aborts); **no API calls, no DB/Storage writes** (chunk-only).
7. **No embedding:** Stage 6 produces no vectors and contacts no Azure service (embedding is Stage 7, §4).
8. **`--dry-run`** reports pages/parents/children to be produced, with no API calls.

---

## 11. Open questions / decisions

- **Image/scanned-page fidelity (§1a.4) — the biggest decision.** sv-kb chunks a **full vision-to-markdown
  transcription**; our Stage 4 is **caption-only**. (A) add a transcription step for image pages to be truly
  indistinguishable, or (B) accept + document the divergence. **Decide before building.**
- **Embedding scope (§4) — DECIDED: chunk-only.** Stage 6 does **not** embed; embedding is deferred to Stage
  7 (mechanism TBD there). Stage 6 makes no Azure calls.
- **`case_key`/`dataset_key`/`document_name` derivation (§1a.3 — NOT cosmetic):** these feed `content_sha`,
  so a wrong key makes every chunk distinguishable + breaks embedding dedup. Likely `dataset_key=DataSet-NN`,
  `case_key=VOLxxxxx`, `document_name=<pdf stem>` — **verify against `svkb_pipeline` models/`IngestionEvent`.**
- **`brief_description=""` (NOT the summary) + `## Transcription` wrapper — decided (§1a):** required for
  sv-kb parity; passing the summary or bare text would make 100% of chunks distinguishable.
- **Text-extraction parity (§1a.4):** keep our redaction-scrubbed Stage-1 text (safer) vs re-extract to match
  sv-kb's raw `get_text` — **decide + document.**
- **Chunk body = page text, not the summary** — **decided (§2).**
- **Input structure / markdown (no stitching — decided).** Our text is plain `structured_text`, so the
  chunker's heading-aware logic is inert and degrades to token-budget packing (matches sv-kb's text-layer
  pages; §2). Keep `chunk_pages` per-page; **do not stitch, do not fork the chunker.** Optional cheap win:
  feed **pymupdf4llm markdown for born-digital pages** (Stage 1's optional reconstruction, currently off) to
  restore real `#` headings for that subset. Real structure on **scanned** pages would need a
  **vision-to-markdown transcription** (a Stage-4 scope+cost change — sv-kb does this, our caption-only Stage
  4 does not) — **deferred.** The larger retrieval lever is the `context_prefix` (and the §8 LLM-generated
  prefix), not headings.
- **Sibling `chunks.json` vs inline `chunks[]`:** **decided — sibling file** (keeps the sidecar lean; mirrors
  the live `chunks_blob_path` artifact; §5/§10.1).
- **Deferred (Phase 2, strategy §8):** LLM-generated `context_prefix` (Anthropic Contextual Retrieval — the
  bigger search-quality lever), heading/semantic-aware chunking, RAPTOR/GraphRAG. Not now; the template
  prefix ships first.

---

## 12. References

- **Strategy:** `STRATEGY-pdf-ingestion-stages.md` → §4 (stage table row 6), §6 (sidecar evolution + Stage-7
  table mapping: `chunks`, `embeddings`, `image_embeddings`), §8 (deferred), and the "Stage 6 — chunk with
  sv-kb's chunker" refinement.
- **sv-kb (reuse verbatim):** `~/repos/sv-kb/pipeline/shared/svkb_pipeline/chunking.py`
  (`chunk_pages`/`parents_to_dicts`/`ParentChunk`/`ChildChunk`), `…/embeddings.py`
  (`AzureOpenAIEmbeddings`/`embed_in_batches`), `…/indexing.py` (`index_document` — the Stage-7 consumer),
  `pipeline/indexer-job/main.py` (env wiring: `AZURE_OPENAI_EMBED_DEPLOYMENT`, `EMBED_DIMENSIONS`,
  `EMBED_BATCH_SIZE`).
- **Templates to mirror:** `src/stage5/{__init__,config,process,walk,cli,__main__}.py` and the Stage 3/4/5
  handoffs.

**Bottom line:** Stage 6 is a thin, deterministic, local wrapper around sv-kb's `chunk_pages`, and the bar is
**chunks indistinguishable from sv-kb's** — so replicate `consolidate.py` exactly: page-ordered
`(page_number, md)` where each text-layer page's `md = f"## Transcription\n\n{text}"`, with
`brief_description=""` and `embedding_model="text-embedding-3-small"` and identifiers that match sv-kb (these
three feed `content_sha`). Write the live-shaped `chunks.json` sibling + a `stage6` block — **chunk-only; no embedding** (that's Stage
7, so Stage 6 makes no Azure calls). The two open fidelity calls: **image pages** (sv-kb transcribes; we
caption — §1a.4) and **text-extraction parity** (our `[REDACTED]` scrubbing vs raw `get_text`). Reuse the
chunker **verbatim**; do not stitch, do not fork; append only.
