# Handoff: Media Track — non-PDF asset processing (audio / video / standalone images)

**Status:** skeleton / anchor — **NOT yet designed in detail.** Establishes the contract so the media stages
slot into the shared tail (`src/embed` = Stage 7, `src/ingest` = Stage 8) exactly like the PDF track.

**Scope:** assets that **never pass through the PDF track (S1–S6)** — standalone images (not extracted from a
PDF), audio, and video. Embedded PDF images are **out of scope** here (Stage 4 already owns them; §"ownership"
below).

**Read first:** `HANDOFF-stage7-embed.md`, `HANDOFF-stage8-ingest.md`, and sv-kb's
`svkb_pipeline/{transcribe.py,vision.py,vision_embed.py,chunking.py,models.py}` + `sql/migrations/0002` (the
`media_metadata` table) and `0009` (media metadata bag).

---

## 0. Why a separate track

sv-kb is multi-source by design: `models.SOURCE_KINDS = ("pdf","office","image","audio","video","web",
"text")`, with `media_metadata`, `chunk_transcript()` (timestamped/speaker chunks), `transcribe.py`,
`vision.py`, and a 1024-d image embedder (`vision_embed.py`). Media needs its own extraction/analysis stages
(probe, transcribe, frame/vision), but it produces the **same downstream contract** as PDFs, so the shared
**embed** and **ingest** stages consume both. Numbered `media-stage-01`, `-02`, … (the `M`-track).

---

## 1. The shared contract (what every media stage must emit)

To be consumable by Stage 7 (embed) and Stage 8 (ingest) unchanged, a processed media asset must produce:
- a **sidecar JSON** beside the asset (status, source, per-segment metadata, safety);
- a **`<name>.chunks.json`** from sv-kb's chunker — `chunk_transcript()` for A/V (timestamps + speaker),
  `chunk_pages`/`chunk_markdown` for image-text/OCR — same `parents_to_dicts` shape, same `content_sha`
  scheme (so Stage 7 dedups media text against PDF text for free);
- **image vectors on disk** for image/frame assets, same staged format as Stage 4
  (`azure-ai-vision-multimodal`, 1024-d, `.emb.json`, keyed by image `content_hash`);
- **blobs** (the media file, thumbnails, extracted frames) for Stage 8 to upload;
- **metadata** for the `media_metadata` typed columns + its JSONB bag (ffprobe/MediaInfo: container, codecs,
  bitrate, language tracks, chapters).

If a media stage emits this contract, **no change to Stage 7/8 is needed.**

---

## 2. Rough stage shape (to be detailed)

- **media-stage-01 — probe/register:** identify `source_kind`, extract container/codec metadata (→ typed +
  bag), thumbnail; deterministic, local.
- **media-stage-02 — transcribe (A/V) / OCR-or-vision (images):** Whisper-class transcription with time
  windows (`transcribe.build_windows`); for images, caption/description + safety (mirrors PDF Stage 4).
- **media-stage-03 — image/frame vision embed:** `vision_embed` 1024-d vectors for image assets + sampled
  video frames, staged like Stage 4.
- **media-stage-04 — chunk:** `chunk_transcript` (A/V) / `chunk_*` (image text) → `<name>.chunks.json`.
- → then shared **Stage 7 (embed)** and **Stage 8 (ingest)**.

---

## 3. Asset-embedding ownership (decided — applies across both tracks)

- **PDF images** (artifacts + image-pages): embedded by **Stage 4** (already staged). Not re-embedded here.
- **Non-PDF images / video frames**: embedded by the **media-image stage** (same model/format).
- **Unification at Stage 8**: both load into one `pipeline.image_embeddings`, deduped by `content_hash`.
- **Text/transcript chunks**: embedded by the shared **Stage 7**, deduped by `content_sha` across PDF + media.
- Coverage gaps in PDF image vectors are fixed by **re-running Stage 4**, never by a media stage.

---

## 4. Open (to design later)

- Exact transcription model/provider + cost posture (A/V is the premium line, like gpt-5-mini for PDF vision).
- Video frame-sampling policy (which frames become image assets/embeddings).
- Standalone-image dedup vs PDF-extracted images (shared `content_hash` space — already unified at S8).
- Per-track manifests + how the dataset-summary module rolls up media alongside PDF.

**Bottom line:** media gets its own extraction/analysis stages, but emits the **same sidecar + `chunks.json` +
image-vectors + blobs + metadata** contract as PDFs, so the shared **embed** and **ingest** stages ingest both
tracks uniformly. Detail the M-stages later; the contract above is the commitment.
