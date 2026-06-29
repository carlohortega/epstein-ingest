# Media safety — known gaps (accepted, deferred for later remediation)

**Status:** Accepted 2026-06-29. The current media safety handling is sufficient for the present run; the two
gaps below are documented for a future remediation pass. Neither blocks ingest. Canonical safety model:
`HANDOFF-stage8-ingest.md` §4a + `sv-kb/docs/SAFETY-staged-vs-svkb.md`.

## What already works (context — NOT gaps)

So the gaps are clear by contrast, here is what the pipeline *does* enforce today:
- **PDF page/image fails safety** → page render replaced with the `content-redacted` placeholder, image vector
  omitted, `review_status='withheld'`, `documents.source_withheld=true`. (`src/ingest/safety.py`,
  `src/ingest/record.py`, `register.py`.)
- **PDF or standalone-image CSAM-suspected** (vision-LLM `minor_indicator`) → **whole document NOT ingested**
  (no rows/blobs/vectors), preserved + reported. (`safety.image_asset_verdict` csam tier → `skip_csam`;
  `src/ingest/media.py:_media_safety` for `kind=="image"`.)
- **Video keyframe fails safety** (Azure Content Safety severity ≥ threshold = `block`) → the keyframe blob is
  **deleted at `media_stage3`** and never embedded; at ingest it yields no vector, no occurrence, no blob; the
  video document is still ingested (transcript) with `source_withheld=true` + a `content_safety_findings` row.
  (`src/media/frames_stage.py:_scan_and_embed`/`_embed_keyframes`; `src/ingest/media.py:_media_safety` video branch.)
- **Audit trail:** every flagged unit writes a `pipeline.content_safety_findings` row (the data model already
  has `modality IN (...,'transcript','keyframe',...)`), so the schema is ready for both gaps below.

---

## Gap 1 — Video has no WHOLE-DOCUMENT CSAM hold (only per-frame redaction)

**Current behavior.** Video keyframes are scanned by **Azure Content Safety**, which scores severity per
category (hate/sexual/violence/self-harm, 0–7) but has **no CSAM / minor verdict**
(`src/media/vision.py:SafetyVerdict` = `flag`/`max_severity`/`categories` only). High-severity frames are
redacted individually, but there is **no doc-level `minor_indicator`** for a video, so — unlike a PDF or a
standalone image — **a video is never held out wholesale for suspected CSAM.** The `minor_indicator` that
drives the PDF/standalone-image whole-doc hold comes from the **vision LLM (gpt-5-mini)** safety field, which
is only populated for standalone images (`media_caption.safety`); the A/V caption path returns
caption/description with **no safety field** (`src/media/caption_stage.py:caption_av`).

**Impact.** A video whose frames contain CSAM has its *individual high-severity frames* redacted, but the
**document (transcript + metadata + raw video file) is still ingested** (with `source_withheld=true`). There is
no whole-video quarantine + preserve/report equivalent to the PDF CSAM path.

**Remediation sketch.**
1. Produce a **doc-level minor/CSAM signal for video** — e.g. run the vision LLM (the gpt-5-mini path that
   already returns `minor_indicator` for images) over the sampled safety-cleared keyframes during
   `media_stage4`, and surface a `safety.minor_indicator` on the A/V `media_caption` block; **or** route any
   `block` keyframe in a video to a human CSAM adjudication.
2. In `src/ingest/media.py:_media_safety`, extend the **video** branch so a positive doc-level minor signal
   returns `csam=True` → `skip_csam` (whole video NOT ingested, preserve + report), matching the image branch.
3. Schema needs nothing new (`documents.csam_suspected`/`safety_status='quarantined'` already exist).

**Code:** `src/media/{vision.py,caption_stage.py,frames_stage.py}`, `src/ingest/media.py:_media_safety`.

---

## Gap 2 — Transcript TEXT (audio + video) is not safety-scanned

**Current behavior.** A/V transcripts (the spoken-word text) are chunked, embedded, and ingested with **no
text-safety scan**. The media track only safety-scans **visual** units (keyframes/images); **audio has no
visual content, so it has no safety gate at all**, and a video's *spoken* content is likewise ungated.
(`src/media/transcribe_stage.py` produces `media_transcribe.windows[].text` → `chunk_transcript` →
`chunks.json`, none safety-scanned. Contrast the PDF track, which carries page `text_safety` from Stage 3.)

**Impact.** Harmful **spoken** content (threats, hate speech, exploitation described in audio) is transcribed,
embedded, and made retrievable with no flag or withhold. Audio assets in particular pass through with zero
safety review.

**Remediation sketch.**
1. Add a **text-safety scan** over transcript windows — Azure **Content Safety `text:analyze`** (or the LLM)
   per `TranscriptWindow`, in `media_stage2` (append severities to each window) or a small new stage.
2. Map flagged windows to `pipeline.content_safety_findings` with **`modality='transcript'`**,
   `locator='t:<ms>'` (the data model already supports this), and apply the §4a policy: high-severity →
   withhold the transcript segment / set `source_withheld`; CSAM-in-text → whole-doc hold.
3. Wire the findings through `src/ingest/media.py:_findings` + `register.py` (which already writes findings).

**Code:** `src/media/transcribe_stage.py`, `src/ingest/media.py:_findings`, `src/ingest/register.py`.

---

## Priority note

If/when remediated, **Gap 1 (video CSAM) is the higher-stakes item** for this corpus (it's the legal/ethical
floor), and **Gap 2 (transcript text) is the broader-coverage item**. Both reuse existing infrastructure (the
`content_safety_findings` model, the §4a withhold/skip mechanics in `src/ingest`); the missing piece is the
**producing-side scan**, not the data model or the ingest gate.
