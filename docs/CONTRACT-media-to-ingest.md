# Contract: media track → Stage 8 ingest (the shared load tail)

**Purpose.** The media track (`src/media*`, `media-stage-*`) and the PDF track both converge on the same two
shared tails — `embed` (text vectors) and `ingest` (live load). This doc states **exactly what Stage 8 ingest
(`src/ingest`) will consume**, so media output is ingestible *without* rework. It is written from the consumer
side: every shape below is something ingest already reads (PDF track) or requires; conform to it and media
folds in by re-running the same tails.

**Status (2026-06-28):** PDF track ingests today (S8 prepare built + tested; S9 apply built, creds-gated).
Media fold-in on the ingest side is **deferred until media output shapes freeze** — see "Ingest obligations".
Owner split: **media agent** produces the shapes below; **ingest (this owner)** builds the discovery walker +
the `source_kind`-keyed occurrence branch.

---

## 1. Document discovery (the one real gap — needs agreement)

Ingest currently finds documents by walking each dataset's `IMAGES/` dir for `*.pdf` with sibling
`<stem>.json` + `<stem>.chunks.json` (`discover.find_pdfs_for_dataset`). **Media files are not PDFs and not
under `IMAGES`, so ingest will not find them yet.** To fold media in, agree on a **discoverable per-dataset
media location** with sibling sidecars, e.g.:

```
DataSet-NN/<MEDIA_DIR>/<stem>.{mp4|mp3|jpg|…}      # the media file (or a pointer)
DataSet-NN/<MEDIA_DIR>/<stem>.json                  # the merged media sidecar (§5 safety, source_kind)
DataSet-NN/<MEDIA_DIR>/<stem>.chunks.json           # transcript + caption chunks (§2)
DataSet-NN/<MEDIA_DIR>/<stem>/keyframes/*.png       # keyframe PNGs (§3)
DataSet-NN/<MEDIA_DIR>/<stem>/keyframes/*.emb.json  # keyframe image embeddings (§3)
```

The exact `<MEDIA_DIR>` is **TBD between the two tracks**. Ingest will add a `find_media_for_dataset` walker
to match it (mirrors `find_pdfs_for_dataset`). Identifiers stay the same: `case_key='epstein'`,
`dataset_key='DataSet-NN'`, `document_name`=stem.

## 2. `chunks.json` — transcript + caption text (already source-agnostic on ingest)

Must be the **shared sv-kb chunker** output (`svkb_pipeline.chunking`), serialized by `parents_to_dicts`:
- `chunk_transcript(...)` → `chunk_kind='transcript'`, with `timestamp_start_ms`/`timestamp_end_ms`/
  `speaker_label` per parent+child.
- `chunk_caption(...)` → `chunk_kind='summary'` (A/V caption) or `'image_caption'` (standalone image).
- Each child carries `content_sha`, `context_prefix`, `text`, `chunk_index`. **`content_sha` MUST be
  `sha256(f"{embedding_model}\n{context_prefix}\n{text}")`** — identical recipe to PDF/embed, so caption +
  transcript text dedups corpus-wide and the `embed` store covers it. An empty `chunks.json` is fine (silent
  video → 0 text chunks; the doc still registers).

Ingest reads these via `record._ordered_child_shas` (prepare) and `parents_from_dicts` → `index_document`
verbatim (apply). **No ingest work needed here** beyond discovery.

## 3. Keyframe / standalone-image embeddings — `*.emb.json` + `content_hash`

For every embedded image (video keyframe or standalone image), write a sibling `*.emb.json`:
```json
{"embedding_model": "azure-ai-vision-multimodal", "model_version": "<ver>", "dim": 1024, "vector": [ ... ]}
```
The image **`content_hash` is NOT stored** — ingest computes it as `sha256(<PNG bytes>)` (the
`pipeline.image_embeddings` dedup key, `vector(1024)`). So the keyframe **PNG must be present** at apply time
(same mount azcopy reads). Mismatched `dim`/model → ingest skips the vector.

## 4. `image_occurrences` mapping (ingest adds a `source_kind` branch)

Schema `0013` supports both shapes; ingest will route by `source_kind`:
| field | PDF image | **video keyframe** | standalone image |
|---|---|---|---|
| `artifact_kind` | `pdf_image` | **`keyframe`** | `pdf_image` (or an agreed value) |
| `page_number` | page | `-1` | `-1` |
| `timestamp_ms` | `-1` | **keyframe ms** | `-1` |
| `frame_index` | `-1` | **sampled frame index** | `-1` |

So the media sidecar's per-keyframe record needs to expose `timestamp_ms` + `frame_index` (the scene-change
sampler already has the real sampled moment — surface it).

## 5. Safety — media doc-level rollup (§4a)

Standalone/media safety is **whole-document** (a flagged media file is not ingested at all; CSAM-suspected →
not ingested + preserve/report). Ingest's `safety.media_doc_verdict` reads a rollup at
`document_summary_final.safety` **or** `media.safety`:
```json
{"flag": "ok|review", "categories": [...], "minor_indicator": bool, "explicit": bool,
 "content_filtered": bool}
```
plus an optional top-level `content_safety` block (Azure severities). High-confidence withhold only — bare
`flag='review'` does NOT trigger (same rule as the PDF gate). Keep this shape and whole-media withholding works
for free.

## 6. `documents.source_kind`

Set `'audio' | 'video' | 'image'` on the media sidecar's `document.source_kind` (ingest reads it into the
`documents` row; the schema CHECK allows all of `pdf/office/image/audio/video/web/text`). Optional but nice:
populate `media_metadata` fields (duration, codecs, transcript stats) for the `pipeline.media_metadata` row —
ingest can fill it when present.

---

## Ingest obligations (this owner, deferred until media shapes freeze)

1. `discover.find_media_for_dataset` — the media-aware walker for the agreed `<MEDIA_DIR>` (§1).
2. A `source_kind`-keyed branch in `record.py`/`load.py` for keyframe `image_occurrences` (`keyframe` +
   `timestamp_ms`/`frame_index`, §4).
3. Optional `pipeline.media_metadata` registration in `register.py` (§6).

Everything else (chunks, text/image vectors, safety gate, tenancy, blobs via azcopy) is already source-agnostic
and needs no change. **Do not build #1–#3 until the media output shapes are frozen** — building against moving
shapes invites rework.

**Refs:** ingest = `src/ingest/{discover,record,safety,load,register}.py`; schema =
`sv-kb/sql/migrations/{0002,0003,0011,0013_image_occurrences,0013_safety_review,0014_source_withheld}.sql`;
chunker = `svkb_pipeline.chunking`; `HANDOFF-stage8-ingest.md` §2a/§4/§4a.
