# Handoff: Finish Stage 3 on DataSet-11 (the deferred ~48%)

**Status:** operational handoff — **not yet run.** DS-11 Stage 3 is **~52% complete** (173 of 332
`IMAGES\NNNN` subfolders). This doc is for the session that finishes the remaining **159 subfolders**.
**Created:** 2026-06-19. **Companion:** [`HANDOFF-stage3-text-summaries.md`](HANDOFF-stage3-text-summaries.md)
(what Stage 3 does) + [`HANDOFF-stage4-image-vision.md`](HANDOFF-stage4-image-vision.md) (the machine/env).

---

## 0. TL;DR

Loop `python C:\epstein-ingest\run_stage3.py <IMAGES\NNNN>` over every DS-11 subfolder that lacks a
`stage3-run-manifest.json`, writing all content-filter blocks to one shared quarantine log. It's
**gpt-4.1-nano text summaries — cheap (~$38 total, non-premium), resumable, ~10–20 hrs realtime.**

---

## 1. Why this is separate (and not urgent)

**Stage 3 (text lane) is independent of Stage 4 (vision lane).** Stage 4 reads `image_assets[]`; it does
**not** need the Stage-3 summaries. Stage 3 only feeds **Stage 5** (the final document summary). So DS-11's
Stage-4 vision run can happen with or without this finished. Finish Stage 3 whenever convenient before
Stage 5 — it is the cheap nano lane, not the premium vision spend.

---

## 2. Exact state (verified 2026-06-19)

- **Dataset:** `C:\Projects\Data\epstein\DataSet-11` — one volume `VOL00011`, nested as
  `VOL00011\IMAGES\NNNN\*.pdf` (332 subfolders, `0001`–`0332`, ~1,000 PDFs each, ~332k docs total).
- **Stage 1 ✅, Stage 2 ✅** (whole-dataset; manifests at the dataset root). DS-11 is now at the **uniform
  Stage-2 end-state** (escalate-vision + purge done 2026-06-19) — irrelevant to Stage 3 but noted.
- **Stage 3 ran per-subfolder** (one `stage3-run-manifest.json` per `IMAGES\NNNN`). **Done: 173** subfolders
  (`0001`–`0172`, `0175`). **Remaining: 159** — `0173`, `0174`, and `0176`–`0332`.
- **Per-subfolder economics** (from existing manifests): ~1,000 docs → **~$0.24 nano + ~3–8 min** realtime
  (e.g. `0100` = $0.258 / 1,497 pages; `0172` = $0.225 / 1,461 pages). A real fraction of pages get
  Azure-content-filter blocked (the existing `VOL00011\IMAGES\content-filter-failures.json` is ~50 KB).

**Total remaining ≈ 159 × ~$0.24 ≈ ~$38 nano, ≈ 10–20 hrs realtime.**

---

## 3. Environment (THIS machine)

- **Interpreter:** `C:\Users\carlo\anaconda3\python.exe` (native Windows, Python 3.12; has the deps).
- **Package:** the WSL repo `\\wsl.localhost\Ubuntu-22.04\home\cortega\repos\epstein-ingest` (`src.stage3`).
  Stage 3 is **realtime + threads** (no multiprocessing), so importing over the UNC path is fine.
- **Creds:** `~/.env.local` (`\\wsl.localhost\Ubuntu-22.04\home\cortega\.env.local`). Stage 3 needs the
  **nano** slot: `AZURE_OPENAI_ENDPOINT` (=`fd-spyvault-ai.cognitiveservices.azure.com`),
  `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_CHAT_NANO_DEPLOYMENT` (=`gpt-4.1-nano`),
  `AZURE_OPENAI_CHAT_API_VERSION` (=`2025-04-01-preview`).
- **Launcher (already created):** `C:\epstein-ingest\run_stage3.py` loads the `AZURE_*` vars from
  `~/.env.local` into the process (keys off the command line), puts the repo on `sys.path`, and calls
  `src.stage3.cli.main(argv)`. Use it instead of `python -m src.stage3` so creds are loaded.

> ⚠️ **Verify the nano deployment exists on THIS resource first** (§4 step 1). The prior 0001–0175 runs
> were on the OLD machine; confirm `gpt-4.1-nano` is deployed on `fd-spyvault-ai` here before looping.

---

## 4. Procedure

### Step 1 — Smoke-test one subfolder (confirms the nano deployment + real cost/time)

```bash
PY='C:\Users\carlo\anaconda3\python.exe'
IMG='C:\Projects\Data\epstein\DataSet-11\VOL00011\IMAGES'
"$PY" C:\epstein-ingest\run_stage3.py "$IMG\0173" -j 8 \
    --content-filter-log "$IMG\content-filter-failures.json"
```

Expect the manifest line to show `pdfs_summarized` > 0 (not all errored) and a cost ≈ $0.2. If it errors
with a deployment/auth problem, the nano deployment isn't set up on this resource — fix that before looping
(deploy `gpt-4.1-nano` on `fd-spyvault-ai`, or point `--deployment` / `--endpoint` at one that has it).

### Step 2 — Loop the remaining subfolders (PowerShell)

Run sequentially (one subfolder at a time — `-j 8` gives in-subfolder concurrency; sequential keeps the
shared content-filter log race-free). The loop **skips any subfolder that already has a manifest**, so it
naturally targets the 159 remaining and is safe to re-run after an interruption.

```powershell
$img='C:\Projects\Data\epstein\DataSet-11\VOL00011\IMAGES'
$py='C:\Users\carlo\anaconda3\python.exe'
$launcher='C:\epstein-ingest\run_stage3.py'
$cflog=Join-Path $img 'content-filter-failures.json'
$subs = Get-ChildItem -LiteralPath $img -Directory | Sort-Object Name
foreach ($s in $subs) {
  if (Test-Path -LiteralPath (Join-Path $s.FullName 'stage3-run-manifest.json')) { continue }
  Write-Host "=== Stage 3: $($s.Name)  ($(Get-Date -Format HH:mm:ss)) ==="
  & $py $launcher $s.FullName -j 8 --content-filter-log $cflog --progress-every 0
}
Write-Host "DONE."
```

Run it under the long-unattended pattern (keep the machine awake — `powercfg /change standby-timeout-ac 0`
— scheduled task / background, single worker process at a time). It's resumable: a crash mid-loop just
re-runs from the first subfolder still missing a manifest.

---

## 5. Idempotency / resume

- **Doc level:** Stage 3 skips any PDF whose sidecar already has a `stage3` block (`skipped_already_done`),
  so a re-run of a partially-done subfolder finishes only the undone docs.
- **Subfolder level:** the loop's `Test-Path stage3-run-manifest.json` gate skips fully-done subfolders
  cheaply (a subfolder that only *skips* still costs ~3 min to walk — that's why we gate on the manifest
  rather than re-running all 332).
- A subfolder whose manifest exists but is suspected partial: delete its `stage3-run-manifest.json` and
  re-run that subfolder; doc-level resume fills only the gaps.

---

## 6. Content-filter quarantine

Azure blocks a real fraction of genuinely-sensitive text pages at the nano call (`content_filter`). Stage 3
flags those `text_safety={flag:"review", content_filtered:true}` (no summary) and appends them to the
`--content-filter-log`. **Point every subfolder at the one shared log** `VOL00011\IMAGES\content-filter-
failures.json` (above) so all blocks merge into a single quarantine list (dedup on file+page). That list is
the CSAM/minors quarantine bucket → human review / mandatory-reporting process; do **not** auto-recover
minors-related content. (Same governance as Stage 4's image content-filter log.)

---

## 7. Verify completion

```powershell
$img='C:\Projects\Data\epstein\DataSet-11\VOL00011\IMAGES'
$subs = Get-ChildItem -LiteralPath $img -Directory
$missing = $subs | Where-Object { -not (Test-Path -LiteralPath (Join-Path $_.FullName 'stage3-run-manifest.json')) }
"subfolders=$($subs.Count)  done=$($subs.Count - $missing.Count)  missing=$($missing.Count)"
# total nano spend across all subfolders:
($subs | ForEach-Object {
   (Get-Content (Join-Path $_.FullName 'stage3-run-manifest.json') -Raw -EA SilentlyContinue |
     ConvertFrom-Json).usage.estimated_cost_usd
 } | Measure-Object -Sum).Sum
```

Done when `missing=0` (all 332 have a manifest). Spot-check a few sidecars in a newly-run subfolder for a
`stage3` block + `document_summary` + per-page `summary`/`text_safety`.

---

## 8. Gotchas

- **Don't run Stage 3 concurrently with anything that writes DS-11 sidecars** (e.g. a Stage-4 run on the
  same dataset). They both rewrite sidecars → corruption risk. DS-11's escalate/purge are already done, so
  the only conflict to avoid is an overlapping Stage-4 run.
- **`--skip-low-quality-text` is ON by default** (correct): those pages are handled by Stage 4 vision, not
  nano. Leave it.
- **Batch mode is not usable here** (Azure caps a batch at ~100k requests and multi-job chunking isn't
  implemented). Per-subfolder realtime is the proven path; don't pass `--batch`.
- **Cost:** budget ~$38 nano; each subfolder's manifest reports its own `usage.estimated_cost_usd`.
- This is the **non-premium** lane — it does not touch gpt-5-mini/embeddings/Content-Safety.

---

**Bottom line:** create nothing new (launcher exists), smoke-test `0173`, then run the §4 loop unattended.
~$38 / ~10–20 hrs of resumable nano summaries finishes DS-11 Stage 3 and unblocks its Stage 5.
