# Shared placeholder PAGE assets

Two canonical placeholders used by **both** `epstein-ingest` (Stage 8 §4a) and `sv-kb` to keep a **PDF
document intact** when an individual page can't be published as-is. Byte-identical in both repos
(`epstein-ingest/assets/redaction/` ≡ `sv-kb/assets/redaction/`).

| File | Title (color) | Replaces |
|---|---|---|
| `content-redacted.png` / `.pdf` | **CONTENT REDACTED** (dark red) | a **flagged** (high-risk: sexual/violent) PDF page or PDF-embedded image |
| `page-blank.png` / `.pdf` | **BLANK PAGE** (gray) | a **blank / effectively-blank** PDF page (per Stage-1/2 metadata) |

Both are US-Letter @ 200 DPI (**1700×2200**), matching the Stage-2 page-render DPI.

## The three-case redaction policy (see HANDOFF-stage8-ingest.md §4a)

1. **Standalone media (image / video / audio) that is quarantined → NOT uploaded at all** (no placeholder —
   there is no document to keep intact). sv-kb rejects it server-side; epstein-ingest leaves it quarantined
   locally and skips it.
2. **Flagged PDF page / embedded image → `content-redacted` placeholder** (document still ingests; the page is
   redacted to preserve continuity).
3. **Blank / effectively-blank PDF page → `page-blank` placeholder** (a clean empty-page marker instead of an
   ugly blank scan; preserves page numbering).

The placeholder is **never embedded**; substituted occurrences reference the placeholder's `content_hash`, so
all substitutions of a variant dedup to one and never point at withheld/empty originals.

## Canonical content hashes (PNG only — the stable dedup key)

- `content-redacted.png` = `408082bbf0381a0248ba955b31606c652c4c489682f9f10bc3590380ebb72f22`
- `page-blank.png`       = `4c23fd4413fad971cd9fc7abf5fa8403111ae9e57e0e15faa4f03942f0546cef`

**PDF bytes are NOT deterministic** — Pillow embeds a creation timestamp, so each `.pdf` regeneration differs.
The PNG is the canonical asset and the dedup key (sv-kb stores/substitutes page *PNGs*); the `.pdf` is a
convenience artifact for the rare single-page-PDF substitution. If a stable PDF is ever needed, strip its
`/CreationDate`.

## Regenerate (deterministic PNGs)

```
C:\Users\carlo\anaconda3\python.exe assets\redaction\generate_redaction_asset.py
```
(Needs Pillow — use the anaconda env that renders Stage 1/2; system/WSL python3 lack PIL.)

## 5-second "content redacted" video — pending

For a flagged **video upload** that sv-kb chooses to answer with a clip (vs. simply rejecting it), a 5 s
1280×720 MP4 can be made from `content-redacted.png` — **ffmpeg is not installed** on this machine yet:
```
ffmpeg -y -loop 1 -i content-redacted.png -t 5 -r 24 \
  -vf "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=white,format=yuv420p" \
  content-redacted-5s.mp4
```
Note: per the policy above, a quarantined standalone video is simply **not uploaded**, so this clip is only a
UX "upload blocked" response, not stored content — confirm whether it's still wanted.

Keep `sv-kb/assets/redaction/` byte-identical to this directory.
