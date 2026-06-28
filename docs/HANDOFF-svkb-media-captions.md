# Handoff → sv-kb agent: media captions + descriptions (audio / video / standalone image)

**To:** the sv-kb maintainer agent (`~/repos/sv-kb`).
**From:** epstein-ingest (media track), 2026-06-23.
**Why:** epstein-ingest just built a media track that gives **every document a caption + description** —
including audio, video, and standalone images. sv-kb does **not** do this today. This handoff specifies
the matching sv-kb changes so the two stay convergent (epstein-ingest deliberately mirrors sv-kb's
chunker/embeddings/schema; this is the one place sv-kb is currently *behind* the agreed model).

The epstein-ingest implementation is the reference design: `src/media/{caption.py,caption_stage.py,
frames_stage.py,chunking.py}` and `src/media_stage4` (caption) / `src/media_stage5` (chunk).

---

## 0. The gap (what sv-kb does today)

Confirmed by reading `~/repos/sv-kb`:
- **No captioning anywhere.** The only image→text path is `svkb_pipeline/vision.py` = PDF **page**
  transcription (gpt-5-mini reads a rendered page → Markdown + visual description). There is no per-image
  or per-document caption/description for media.
- **`chunk_kind` reserves `'image_caption'` and `'summary'`** (`sql/migrations/0003_chunks_embeddings.sql`,
  `svkb_pipeline/chunking.py`) but **nothing populates them.**
- **`documents` has no caption/description column** — by design, captions/summaries belong as **chunks**.
- **Keyframes** are already embedded as `image_embeddings` + `image_occurrences`
  (`artifact_kind='keyframe'`, `0013_image_occurrences.sql`) by `transcribe-job` → `image-embed-job`.
  Good — keep this. They are *occurrences*, not documents.
- **`media_metadata`** (`0002_documents.sql`) is populated for A/V by `transcribe-job`.

So: keyframe **visual** search exists; **textual** "what is this video/audio/photo" does not.

---

## 1. Target model (match epstein-ingest)

Every document gets a caption + a description, produced and **chunked** (so it's embedded + searchable):

| source_kind | caption + description from | chunk_kind for the caption text |
|---|---|---|
| video | transcript + a small **sample** of safety-cleared keyframes (gpt-5-mini, multimodal) | `summary` |
| audio | transcript (gpt-5-mini, text) | `summary` |
| image (standalone) | the image (gpt-5-mini, Stage-4 image prompt) | `image_caption` |
| pdf | already has `document_summary` + per-embedded-image captions (Stage 4 / vision-page) | (unchanged) |

Keyframes stay **occurrences** (visual search); a sample of them is *input* to the video caption — they
are not separate documents and do not each get their own caption.

---

## 2. Concrete sv-kb changes

### 2a. A/V caption + description (the main addition)
- **Where:** `pipeline/transcribe-job/main.py` (after transcription + keyframe extraction), or a small new
  step before `chunker-job`/`indexer-job`.
- **What:** one gpt-5-mini call per document:
  - **video** → system "passive description tool" prompt + user = transcript excerpt (cap ~12k chars) +
    `image_url` blocks for ~6–12 sampled keyframes (`detail:"low"`);
  - **audio** → same prompt, transcript only.
  Structured output `{caption, description}` (strict json_schema). See
  `epstein-ingest/src/media/caption.py` (`build_av_messages`, `AV_CAPTION_SCHEMA`, `_AV_SYSTEM`).
- **Deployment:** the existing gpt-5-mini vision deployment (`AZURE_OPENAI_VISION_DEPLOYMENT`).
- **Chunk it:** run the caption+description text through `chunking.chunk_markdown(...)` and **re-tag**
  `chunk_kind='summary'` on the parents + children (exactly how `chunk_transcript` re-tags `'transcript'`).
  Index continuously alongside the transcript chunks. Reference: `src/media/chunking.text_parents` +
  `serialize`.

### 2b. Standalone-image caption + description
- sv-kb currently routes images → `img2pdf` → PDF pipeline (no caption). To give images a caption,
  either (recommended) add a native image caption step using the **Stage-4 image prompt + schema**
  (`vision.py`'s page prompt is page-shaped; epstein uses `src/stage4/prompts.py` `ASSET_SCHEMA` /
  `build_asset_messages`, returning caption/description/content_class/has_visual_content/safety), and chunk
  the caption as `chunk_kind='image_caption'`; the visual vector continues via `image-embed-job`.
- Net: a standalone photo ends up with a 1024-d visual vector **and** an `image_caption` text chunk.

### 2c. Keyframe sampling — adopt CONTENT-AWARE (scene-change) sampling + content-scaled safety
- sv-kb's `transcribe.py` uses a **fixed** `DEFAULT_KEYFRAME_INTERVAL_SECONDS = 10`. This corpus is
  dominated by **long, static surveillance footage** (hours of an unchanging cell exterior); a fixed
  interval wastes ~120 near-identical embeds + a dense safety scan per such video.
- **Sample on visual change, not wall-clock:** `ffmpeg -vf select='gt(scene,T)'` (epstein default
  `keyframe_mode=scene`, `scene_threshold≈0.4`, detect on a downscaled decode), with a per-video **CAP**
  (`MAX_FRAMES≈120`), a **FLOOR** (`min_frames≈2`), and a fallback to a tiny representative set when there
  are no changes. Static footage → ~1–2 keyframes; busy footage → capped. Reference:
  `src/media/ffmpeg.detect_scene_changes` + `frames_stage._embed_timestamps`. (Keep `interval` mode as a
  fallback = the old behavior with the cap.)
- **Make safety cost scale with content, not length:** scan the scene-change frames (a new face/object IS
  a scene change) **plus a COARSE floor grid** (`safety_floor_seconds≈120`) as a backstop — instead of a
  dense fixed grid. A 10h static video drops from ~1800 Content-Safety scans to a handful. Reference:
  `src/media/frames_stage._safety_floor_scan`.

### 2f. Audio-aware transcription gating — the premium-line cost saver
- Transcription is billed per audio minute and is otherwise content-blind, so hours of silent surveillance
  audio cost full price for empty transcripts. Add: **skip when there's no audio stream** (probe already
  has this), and run **`ffmpeg silencedetect`** to skip fully-silent files and silent windows (no API call
  for those). Bill on transcribed minutes, not total. Reference: `src/media/ffmpeg.detect_silence` +
  `transcribe_stage` (`skip_no_audio`/`skip_silent`/`silence_noise_db`/`silence_min_seconds`).

### 2d. Redaction at the media stage (defense-in-depth)
- A keyframe/standalone image that Content Safety scores high-risk (`block`) should **not** be embedded,
  its bytes should not be retained, and it must **not** be sent to the caption LLM. (epstein deletes the
  blob + omits the vector in `frames_stage`, and `caption_stage` skips block-scored images.) This is
  stricter than relying solely on the upload-time redaction gate.

### 2e. Cost / token capture
- The new caption calls are gpt-5-mini chat → they return `usage` (prompt/completion/cached tokens).
  Record them via `svkb_pipeline/inference_ledger.py` (`usage_from_openai`, `insert_inference_usage`,
  `build_idempotency_key`) like the other LLM jobs, so media caption spend is metered.
  (Transcription itself is billed per audio-minute and returns no token usage — track minutes, as today.)

---

## 3. Schema — no migration needed

Everything required already exists:
- `pipeline.chunks.chunk_kind` already allows `'summary'` and `'image_caption'` (`0003`). Just populate them.
- `pipeline.embeddings` embeds child chunks regardless of `chunk_kind` (the indexer is kind-blind), so
  caption/summary children embed for free once chunked.
- `pipeline.media_metadata`, `pipeline.image_embeddings`, `pipeline.image_occurrences` unchanged.
- No new `documents` column — caption/description live as chunks (sv-kb's existing convention).

---

## 4. content_sha / dedup invariant (don't fork the chunker)

epstein-ingest computes `content_sha = sha256(f"{embedding_model}\n{context_prefix}\n{text}")` by calling
sv-kb's `chunk_transcript` / `chunk_markdown` + `parents_to_dicts` **verbatim**. Keep producing captions
through the **same** `chunk_markdown` path (re-tagging `chunk_kind`) so the prefix/`content_sha` recipe is
identical and caption text dedups across the corpus exactly like transcript/PDF text. Do **not** hand-roll
caption chunks with a different prefix.

---

## 5. Acceptance (parity with epstein-ingest media track)

1. Audio + video documents have a `summary`-kind caption/description chunk (from transcript [+ keyframes]).
2. Standalone images have an `image_caption`-kind caption/description chunk + a 1024-d visual vector.
3. Keyframes remain `image_occurrences` (visual search), sampled with a per-video cap; Content Safety
   scanned densely; high-risk frames/images redacted (not embedded, not captioned).
4. Caption LLM token usage recorded in the inference ledger.
5. `content_sha` for caption chunks is produced by sv-kb's own `chunk_markdown` (no fork).

**One-liner:** sv-kb already gives videos *visual* search via keyframe vectors; add the *textual* layer —
a gpt-5-mini caption+description for audio/video (from transcript+keyframes) and for standalone images —
chunked into the already-reserved `summary` / `image_caption` kinds, metered in the inference ledger, with
adaptive-capped keyframe sampling so the corpus doesn't explode.
