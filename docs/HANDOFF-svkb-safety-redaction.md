# Handoff: sv-kb safety redaction — **OBSOLETE / RETRACTED**

**This document is retracted (2026‑06‑25).** It proposed a **scan-before-upload, never-store-flagged-bytes**
change to sv-kb. That approach was **rejected**: in the native pipeline, content review happens *after* upload,
so refusing uploads / byte-substituting at ingest isn't achievable. sv-kb **gates at serving** instead.

**Canonical reference:** `sv-kb/docs/SAFETY-staged-vs-svkb.md`. The settled model:
- **Native sv-kb:** ingest everything; gate at serving (CSAM → whole-doc 403; sexual/violence/hate/self-harm →
  per-unit withhold; serve the shared placeholder via a `redacted` capability; vectors kept, label-not-exclude).
- **Staged ingest (epstein `src/ingest`):** the corpus is curated, so it **removes** violations pre-ingest
  (CSAM-suspected → whole document not ingested + preserve/report; withhold → asset removed, placeholder page,
  vector absent, `review_status='withheld'`, `source_withheld=true`). See `HANDOFF-stage8-ingest.md` §4a.

Pending sv-kb work is tracked in the canonical doc §7 (e.g. `documents.source_withheld` + gateway gate;
widen `review_status` to allow `'withheld'`; delete-on-confirm, CSAM exempt).
