# Handoff: Media Track — non-PDF asset processing (audio / video / standalone images)

**Status:** **IMPLEMENTED 2026-06-23** (was: build handoff). Built as `src/media` (shared engine,
stdlib-only core) + `src/media_stage1..4` (runnable stages, CLI `python -m src.media_stageN`), mirroring
the `src/stage1..3` idioms. Tests: `tests/run_media_tests.py` (no network / no ffmpeg — injected fake
clients + monkeypatched ffmpeg; 34 checks). Detailed against sv-kb's real media pipeline. The PDF track
(S1–S6) + `embed` (S7) + `ingest` (S8) are built; the media track is a **parallel set of producer
stages** that emit the **same downstream contract** so the shared `embed` + `ingest` tail consumes both.

**What was built (vs. this spec):**
- `media_stage1` (probe): `ffprobe` → sidecar `<name>.media.json` (typed `media_metadata` + raw ffprobe
  JSONB bag); `--dry-run` inventories; missing-ffprobe → error sidecar, re-probed on resume. Reports
  total audio+video minutes (the cost driver) per §0a.
- `media_stage2` (transcribe): ffmpeg segment (`-vn`, mono/16k/64k) → `gpt-4o-mini-transcribe` per window
  → `transcript.md`/`.vtt`/`transcribe.meta.json` + a `media_transcribe` block carrying the windows.
  `--dry-run` projects cost from probed minutes. Mirrors Stage 3 (injected client, limiter, retry).
  **Audio-aware gating (cost control for long/silent surveillance footage):** no audio stream → skip
  transcription entirely; `silencedetect` → skip fully-silent files and silent windows (no API call);
  **billed minutes = transcribed, not total** (`usage.transcribed_minutes`). Tunable
  (`--skip-no-audio`/`--skip-silent`/`--silence-noise-db`/`--silence-min-seconds`).
- `media_stage3` (frames): **CONTENT-AWARE keyframes by default** (`keyframe_mode=scene`): sample on
  visual change (`ffmpeg select='gt(scene,T)'`) with a per-video CAP + FLOOR + fallback — static footage
  yields ~1–2 frames, not 120 (the surveillance-cost fix); `interval` mode keeps the legacy time grid.
  **Safety scales with content, not length:** scene-change frames are scanned (a new face/object *is* a
  change) plus a COARSE floor grid backstop (`safety_floor_seconds`) — a 10h static video no longer costs
  ~1800 Content-Safety scans. 1024-d AI Vision vectors as `.emb.json` in the **exact Stage-4 staged
  format** (`embedding_model`/`model_version`/`dim`/`vector`); `image_occurrences` rows
  (`artifact_kind`,`frame_index`,`timestamp_ms`=real sampled time,`page_number=-1`). Keyframes are
  **occurrences, not documents** (visual search + a sample feeds the §4 caption). **Redaction enforced at
  this stage**: a frame/image scored `block` is never embedded and its blob is deleted (defense-in-depth
  ahead of the S8 §4a gate).
- `media_stage4` (caption — added 2026-06-23): doc-level **caption + description** via gpt-5-mini (reuses
  Stage 3's **token-metered** `NanoClient` + Stage 4's image prompt/schema). **video** ← transcript +
  sampled keyframes; **audio** ← transcript; **image** ← Stage-4-identical fields
  (caption/description/content_class/has_visual_content/safety). Captures prompt/completion/cached tokens
  + USD (the media token-cost line). High-risk (`block`) images are not sent to the LLM. Appends a
  `media_caption` block.
- `media_stage5` (chunk): reuses sv-kb's `chunk_transcript` + `chunk_markdown` + `parents_to_dicts`
  **verbatim** (NOT forked) → bare-list `<name>.chunks.json` byte-identical to `src/embed`'s expected
  input (`parents_from_dicts`). Combines `chunk_kind` **transcript** (A/V windows) + **summary** (A/V
  caption) + **image_caption** (standalone-image caption), so S7 dedups all media text vs PDF text by
  `content_sha`.

**The bar:** media must be **indistinguishable from a native ~/repos/sv-kb ingest** for what we stage (same
DB rows, same chunk/embedding shapes, same blobs), bar the documented divergences (§9).

**Read first:** sv-kb's `svkb_pipeline/{transcribe.py,chunking.py,vision_embed.py,blob_paths.py,models.py}`,
`pipeline/{transcribe-job,image-embed-job,normalize-job}/main.py`, and
`sql/migrations/{0002_documents.sql (media_metadata),0003_chunks_embeddings.sql,0013_image_occurrences.sql}`.
Companions: `HANDOFF-stage7-embed.md`, `HANDOFF-stage8-ingest.md`, `HANDOFF-stage4-image-vision.md` (the PDF
image lane this mirrors).

---

## 0. Scope & how sv-kb does it natively (the target)

In scope: assets that **never pass through the PDF track** — **audio**, **video**, and **standalone images**
(not extracted from a PDF). Embedded PDF images stay owned by Stage 4 (§7). `models.SOURCE_KINDS =
("pdf","office","image","audio","video","web","text")`.

Native sv-kb routing (`normalize-job/main.py`):
- **audio / video** → `transcribe-job` → (keyframes →) `image-embed-job` → `indexer-job`.
- **image** → converted to a **single-page PDF** (`img2pdf`) → the **PDF/OCR pipeline** (so sv-kb has *no*
  dedicated image stage — an image is just a 1-page PDF).
- transcription is **Azure OpenAI `gpt-4o-mini-transcribe`** (NOT Azure Speech), windowed.

---

## 0a. Reality (what media actually exists) + prerequisites

**Measured inventory (2026-06-23, files outside `VOL/IMAGES`):** ~**3,000 video** files (DS-09 ≈ 2,161 incl.
**1,730 `.avi`**; DS-10 ≈ 828; DS-11 = 4; DS-12 = 0), ~**150 audio** (m4a/wav/mp3), ~**330 standalone images**
(DS-09). **Video dominates** → the cost/effort center is **video transcription + keyframes**. Media lives in
the non-IMAGES sibling dirs (NATIVES etc.), so the media walk targets media file extensions and **excludes the
`IMAGES` PDF tree**.

**HARD PREREQUISITE — ffmpeg/ffprobe are NOT installed** (confirmed absent on Windows AND WSL). The ENTIRE
media track needs them (probe, audio segmentation, keyframes). **Install ffmpeg+ffprobe before any media work.**

**Two leading principles (apply the project's proven playbook):**
- **Reuse sv-kb's `svkb_pipeline/transcribe.py` VERBATIM** (`probe_media`, `segment_audio`, `build_windows`,
  `extract_keyframes`, `AzureOpenAITranscribe`) + `chunking.chunk_transcript` — the fidelity core, exactly as
  Stage 6 reused `chunk_pages`. Do not reimplement transcription/windowing/chunking.
- **Probe/size FIRST, spend SECOND.** Run the cheap local probe pass (media-stage-1) over all media to compute
  **total audio-minutes** (the cost driver) and project transcription $ BEFORE running media-stage-2. 3,000
  videos of unknown duration could be cheap or very expensive — don't commit blind. Long transcription runs
  use the **Windows Scheduled Task** durability pattern (Claude background tasks get reaped ~25 min).

---

## 1. The shared contract (what every media stage MUST emit)

Identical to the PDF track's downstream contract, so S7/S8 need **no changes**:
1. a **sidecar JSON** beside the asset (status, source sha/bytes, probe metadata, per-segment data, safety);
2. a sibling **`<name>.chunks.json`** from sv-kb's chunker — **`chunk_transcript()`** for A/V (timestamps +
   speaker), `chunk_pages`/`chunk_markdown` for image text — same `parents_to_dicts` shape, same `content_sha`
   = `sha256(model\ncontext_prefix\ntext)` (so S7 dedups media text against PDF text for free);
3. **image vectors on disk** for image/keyframe assets — same staged format as Stage 4
   (`azure-ai-vision-multimodal`, **1024-d**, `.emb.json`, keyed by image **`content_hash`**);
4. **blobs** for S8 to upload (the media file, transcript .md/.vtt, poster, keyframes);
5. **`media_metadata`** typed fields + a JSONB bag (ffprobe output).

---

## 2. AUDIO (mirror sv-kb `transcribe-job` + `transcribe.py`)

**Pipeline:** `ffprobe` probe → `ffmpeg` segment audio (mono, 16 kHz, 64 kbps mp3, `-vn`, `-segment_time =
window_seconds`) → transcribe each window via **gpt-4o-mini-transcribe** → `build_windows()` →
`chunk_transcript()`.

**Transcription (the premium line for media):**
- Env: `AZURE_OPENAI_ENDPOINT`/`_API_KEY`, `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` (default
  `gpt-4o-mini-transcribe`), `AZURE_OPENAI_CHAT_API_VERSION` (default `2025-04-01-preview`).
- `TRANSCRIBE_WINDOW_SECONDS` default **120**. Each 120 s window → one `TranscriptWindow(index, start_s,
  end_s, text)`; the model returns **plain text only** (no word timestamps), so timestamps are **window-level**
  (deterministic). `speaker_label` is **null** (diarization not done) — carried as a field for the future.
- `tools`: `ffmpeg`/`ffprobe` (env `FFMPEG_BIN`/`FFPROBE_BIN`, default on PATH).

**Chunks:** `chunk_transcript(windows, document_name, case_key, dataset_key, speaker_label=None,
embedding_model="text-embedding-3-small")` → parents/children with `chunk_kind="transcript"`,
`timestamp_start_ms`/`timestamp_end_ms` (window bounds ×1000), `speaker_label`. Children carry
`context_prefix` + `content_sha` exactly like PDF children → embedded by **S7** unchanged.

**Artifacts (blobs):** `transcript.md` (`## [hh:mm:ss]` headings + text — the markdown `chunk_transcript`
consumes), `transcript.vtt` (WebVTT), `transcribe.meta.json` (probe + transcription stats), `poster.webp`
(480×120 waveform).

**Rows S8 writes:** `documents` (`source_kind='audio'`, `md_blob_path` → transcript.md), **`media_metadata`**
(1 row: `duration_seconds`, `audio_channels`, `sample_rate`, `transcript_blob_path/_model/_word_count/
_language/_confidence`, `speaker_count`, `poster_blob_path`), `chunks` (transcript parents/children),
`embeddings` (child text vectors, via S7 pre-load + guard provider). **No `document_pages`** for A/V.

---

## 3. VIDEO (audio path of §2 + visual keyframes)

Everything in §2 (the `-vn` segmentation transcribes the audio track identically), **plus**:
- **Keyframes:** `ffmpeg -vf fps=1/interval`, each frame ≤480 px **WebP**, `artifacts/frames/frame_<NNNN>.webp`.
  **Use a DURATION-ADAPTIVE interval with a per-video CAP** — `interval = max(BASE≈10s, ceil(duration/
  MAX_FRAMES))` with `MAX_FRAMES ≈ 60–120` — NOT sv-kb's fixed 1/10s. At this corpus's scale (~3k videos, many
  hours each) a fixed interval yields **millions** of keyframes (each = an AI-Vision embed + CS scan + blob +
  occurrence). The cap bounds cost/storage per video; long videos just sample coarser (fine — the **transcript
  is the primary searchable content**; keyframes are supplementary). Configurable (extends sv-kb's
  `VIDEO_KEYFRAME_INTERVAL_SECONDS` knob).
  - **Safety/retrieval decoupling (decide):** keyframes also feed the visual safety scan, so a coarse cap could
    let an objectionable frame fall *between* embedded samples. Content-Safety is cheap; embedding+storage is
    the cost — so **scan densely for safety, keep sparse embedded keyframes** (the cap). Optional v2:
    ffmpeg scene-change detection (`select='gt(scene,T)'`) to capture on visual change, paired with the cap.
- **Poster:** WebP frame at 50 % duration → `poster.webp`.
- **Keyframe embeddings:** each frame → **Azure AI Vision** 1024-d vector (`.emb.json`, keyed by
  `content_hash = sha256(webp_bytes)`), same as Stage 4 images. S8 loads them into `image_embeddings` (dedup
  by `content_hash`) **and** writes an **`image_occurrences`** row per frame:
  `artifact_kind='keyframe'`, `frame_index`, `timestamp_ms` (= `frame_index × interval × 1000`),
  `page_number=-1`.
- **`media_metadata`** additionally carries `video_width`, `video_height`, `frame_rate`, `keyframe_count`,
  `keyframe_interval_seconds`.

(Safety: run **Azure Content Safety** on keyframes — the CSAM net — exactly as Stage 4 does on PDF photo
artifacts; this domain makes it non-negotiable. Keyframes + standalone images carry the same per-asset safety
verdict, and at ingest they pass the **Stage 8 §4a safety-redaction gate** — high-risk keyframes/images are
replaced with a placeholder and never uploaded, and their image vector is omitted, exactly like PDF images.)

---

## 4. STANDALONE IMAGE — decision required (the one real fork)

Native sv-kb converts an image to a **single-page PDF** (`img2pdf`) and runs the **PDF/OCR pipeline**; there
is **no native image-caption path**. Two options for epstein:

- **(A — recommended for fidelity + reuse) `img2pdf` → run the existing PDF track (S1–S6).** The image becomes
  a 1-page PDF, lands in `VOL/IMAGES`, and flows through S1–S6 → Stage 4 captions + safety + **1024-d image
  embedding** → shared S7/S8. This is the most sv-kb-faithful routing and reuses everything. **Caveat:** an
  image page has no text layer, so (per the PDF track's `skip-no-text` policy) it yields a **visual vector but
  no text chunks** — the same documented scanned-page divergence (§9). `source_kind` on the row is `image`.
- **(B) a dedicated `media-image` stage** mirroring Stage 4 (gpt-5-mini caption+description + 1024-d embed +
  safety) **and chunking the caption** into `chunks.json` so the image is text-searchable. Richer text recall,
  but diverges from sv-kb (which doesn't caption-chunk) and is more new code.

**Recommendation: (A)** unless caption-based text search of standalone photos is a stated requirement — then
(B). Either way the **1024-d image embedding + `image_occurrences` (`artifact_kind='pdf_image'` for A)** are
produced; S8 unifies by `content_hash`.

---

## 5. The media stages (producer stages; mirror `src/stageN`)

- **`media_stage1` — probe / register:** detect `source_kind` (ffprobe + extension), capture container/codec
  metadata (duration, channels, sample_rate, width/height, frame_rate, bitrate) → sidecar + the
  `media_metadata` field set + JSONB bag; emit thumbnail/poster. Deterministic, local, no API.
- **`media_stage2` — transcribe (A/V):** ffmpeg segment → gpt-4o-mini-transcribe per window →
  `transcript.md`/`.vtt` + `transcribe.meta.json`. The premium line — needs a stage3/4-style limiter + retry.
- **`media_stage3` — frames / image vision:** video keyframes (+ poster); 1024-d image embeddings for
  keyframes and (option B) standalone images; Content-Safety scan. Mirrors Stage 4's embed/safety.
- **`media_stage4` — chunk:** `chunk_transcript` (A/V) / `chunk_*` (image text) → `<name>.chunks.json`.
- → shared **`embed` (S7)** then **`ingest` (S8)**. (Standalone images via option A skip M-stages and use the
  PDF track instead.)

Each stage: append-only sidecar block, idempotent/resumable, fault-isolated, per-stage run manifest — same
conventions as `src/stage1..6`.

---

## 6. Schema targets (so S8 writes the same rows as native sv-kb)

- **`pipeline.documents`** — `source_kind ∈ {audio,video,image}`; `md_blob_path` → transcript.md (A/V).
- **`pipeline.media_metadata`** (`0002`) — one row per A/V doc (the typed fields in §2/§3).
- **`pipeline.chunks`** — transcript chunks: `chunk_kind='transcript'`, `timestamp_start_ms`/`_end_ms`,
  `speaker_label`. (`index_document` writes these unchanged — it's `chunk_kind`-blind.)
- **`pipeline.embeddings`** — child transcript text vectors (S7, `content_sha`).
- **`pipeline.image_embeddings`** + **`pipeline.image_occurrences`** (`0013`) — keyframe/image vectors
  (`content_hash`) + occurrence rows (`artifact_kind`, `frame_index`, `timestamp_ms`).

S8 is already source-agnostic (`index_document` is `source_kind`-blind); the only S8 additions media needs are
the **`media_metadata`** insert and the **`image_occurrences`** rows — fold these into S8's `register.py`/
`load.py` (the S8 handoff already lists `image_embeddings`; add `image_occurrences` + `media_metadata`).

---

## 7. Asset-embedding ownership (decided — across both tracks)

- **PDF images** (artifacts + image-pages): embedded by **Stage 4** (staged). Not re-embedded here.
- **Non-PDF images / video keyframes:** embedded by the **media stages** (same model/format/1024-d).
- **Unification at S8:** both load into one `pipeline.image_embeddings`, deduped by `content_hash`; each
  physical occurrence recorded in `image_occurrences`.
- **Transcript/text chunks:** embedded by the shared **S7**, deduped by `content_sha` across PDF + media.
- PDF image-vector coverage gaps are fixed by **re-running Stage 4**, never by a media stage.

---

## 8. Azure resources & cost posture

| Work | Service | Env | Cost |
|---|---|---|---|
| A/V transcription | Azure OpenAI `gpt-4o-mini-transcribe` | `AZURE_OPENAI_*`, `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` | **the media premium line** (∝ audio minutes) |
| Image/keyframe embed | Azure AI Vision (vectorizeImage) | `AZURE_AI_VISION_*` | cheap (1024-d) |
| Image caption (option B / image-as-PDF Stage 4) | Azure OpenAI gpt-5-mini | `AZURE_OPENAI_VISION_DEPLOYMENT` | premium (only if captioning) |
| Content Safety (frames/images) | Azure Content Safety | `AZURE_CONTENT_SAFETY_*` | cheap (CSAM net) |
| ffmpeg / ffprobe (segment, keyframes, probe) | local | `FFMPEG_BIN`/`FFPROBE_BIN` | free |

Transcription cost scales with **audio duration**, not file count — project it from total A/V minutes before a
bulk run (mirror the PDF `--dry-run` cost-projection discipline).

---

## 9. Divergences from native sv-kb (be explicit)

- **Standalone images (option A):** visual vector only, no text chunks (caption not chunked / no
  vision-to-markdown) — the same scanned-page gap as the PDF track. Option B closes the text gap but diverges
  the other way (sv-kb doesn't caption-chunk).
- **Speaker diarization:** not done (`speaker_label=null`) — matches sv-kb today (it also defers diarization).
- Everything else (transcript chunking, timestamps, media_metadata, image embeddings/occurrences) is built to
  match sv-kb's own jobs.

---

## 10. Sequencing — media vs. S8 (recommendation)

**Do NOT block S8 on the media track.** S8 is source-agnostic and **idempotent per document**, so:
1. **Build S8 now, fully source-aware** (it must *know* the media contract — `media_metadata`,
   `image_occurrences`, transcript chunks — so it isn't retrofitted later; this handoff + the agent findings
   give the full schema, so designing it in now is free).
2. **Run S8 on the PDF corpus immediately** (DS-9/10/11/12 are embedded + ready; ingest is ~$0 and proves the
   loader end-to-end against live sv-kb).
3. **Build the media stages next**, then **re-run S8** (idempotent) to fold media in — no rework, incremental
   value, and the expensive transcription spend is decoupled from getting the PDF corpus live.

Build S8 media-aware; ingest PDFs first; add media + re-ingest. (Only do media-first if you specifically want
a single big-bang ingest — not worth blocking the ready PDF corpus.)

---

## 11. Acceptance criteria

1. A/V produce `chunks.json` via `chunk_transcript` (transcript kind + ms timestamps), consumable by S7/S8
   unchanged; `content_sha` dedups against PDF text.
2. `media_metadata` populated from ffprobe + transcription stats; `image_occurrences` written for keyframes.
3. Image/keyframe vectors are `azure-ai-vision-multimodal` 1024-d, `content_hash`-keyed, unified at S8.
4. Standalone-image decision (A vs B) implemented per §4; image vectors produced either way.
5. Idempotent/resumable per asset; per-stage manifests; safety (Content Safety) on frames/images.
6. Indistinguishable from a native sv-kb media ingest except the §9 divergences.

---

## 12. Open decisions

- **§4 standalone-image — RESOLVED (option B, native): caption + description + visual vector.** Updated
  2026-06-23: `media_stage3` produces the 1024-d visual vector + safety + occurrence, and `media_stage4`
  now produces a Stage-4-identical **caption + description** that `media_stage5` chunks as
  `chunk_kind="image_caption"` — so standalone photos are text-searchable, matching the principle that
  **every document has a caption + description** (PDF-embedded images already do, via Stage 4). No
  img2pdf/PyMuPDF round-trip.
- **Doc-level caption/description for video + audio (added 2026-06-23):** the media analog of the PDF
  `document_summary`. Video caption is generated from the transcript + a small sample of safety-cleared
  keyframes (cost lever `--max-keyframes`, default 8); audio from the transcript. Chunked as
  `chunk_kind="summary"`. Keyframes remain `image_occurrences` (visual search), NOT separate documents.
- Transcription cost ceiling / batching for large A/V volumes (the premium line) — `media_stage2
  --dry-run` projects cost from probed minutes; a Batch transport (like Stage 3's) is not yet built.
- Whether to OCR text *burned into* video keyframes (extra recall vs cost) — default no.
- Dataset inventory: how much audio/video/image actually exists per dataset (drives cost) — run a probe pass
  (`media_stage1`, which reports total audio+video minutes) first to size it. Still owed: DS-01..08.

**Bottom line:** media gets its own probe/transcribe/frame/chunk stages, but emits the **same sidecar +
`chunks.json` + 1024-d image vectors + blobs + media_metadata** contract as PDFs — so the already-built
`embed` and `ingest` stages load both tracks uniformly and indistinguishably from native sv-kb (bar the
documented image-text gap). Build S8 media-aware now, ingest the ready PDF corpus first, then add media and
re-ingest.
