# Handoff: RUN Stage 3 across DataSet-09/10/11/12 (this machine, 2026-06-19)

**You are the session that will get Stages 2→3 to completion across all four datasets.** Stage 3 is built and
validated; this is the **current-state + orchestration** handoff. It **supersedes the machine/paths and
"current state" sections of [`HANDOFF-stage3-execution.md`](HANDOFF-stage3-execution.md)** (that doc was
written for the OLD box — `D:\Carlo\…`, `home/carlo`, `doc-scanner` conda). Read the old doc for the
*detailed run mechanics that haven't changed* (content-filter handling, concurrency math, batch caveat) and
[`HANDOFF-stage3-text-summaries.md`](HANDOFF-stage3-text-summaries.md) for *design*. Read **this** for *where
we stand, on what machine, and in what order*.

---

## 0. What Stage 3 does (30 seconds)

For every Stage-2 `text`/`mixed` page → **one bundled gpt-4.1-nano call** → page `summary` + `summary_json`
(key_points/entities/topics) + `text_safety` triage flag; then a **provisional** per-document summary.
**Text only — no vision** (vision is Stage 4). Skips `low_quality_text` pages by default
(`--skip-low-quality-text`). Extends the sidecar JSON in place; writes `stage3-run-manifest.json` +
`content-filter-failures.json`. **No Azure Storage / no DB** (Stage 7). Idempotent, resumable, fault-isolated.

**Stage 3 needs only Stage 1 + Stage 2** (`status:"ok"` + a `stage2` block with `page_kind`). It does **not**
need escalate/purge — those are Stage-4 prep and are independent (see §5).

---

## 1. THE STATE OF THE FOUR DATASETS (the headline — and a correction)

Verified 2026-06-19 from manifests + sampled sidecars across early/mid/late folders.

| Dataset | Stage 1 | Stage 2 | Stage 3 | Stage-2 flow | Layout | On C: yet? |
|---|---|---|---|---|---|---|
| **DS‑09** | ✅ | ✅ `--no-stage-pages` **+ escalate + purge** (uniform) | ❌ **not started — READY now** | NEW flow | **flat** `IMAGES\*.pdf` (528,995) | ✅ yes |
| **DS‑10** | ✅ | ❌ **NOT started** | ❌ blocked on Stage 2 | — | nested `IMAGES\NNNN` (504 folders, 503,154) | copying C: now |
| **DS‑11** | ✅ | ✅ (ORIGINAL full-staging) | ⏳ **partial** (≈0001–0167 done, tail to 0332 not) | original | nested (332 folders, 331,655) | copying C: now |
| **DS‑12** | ✅ | ✅ (ORIGINAL full-staging) | ✅ **done** (152 PDFs, $0.17) | original | nested (1 folder, 152) | copying C: now |

**Correction to a common belief:** **DS‑10 has NOT undergone Stage 2.** It is Stage‑1-only — sampled sidecars
have no `stage2` block, `page_kind=None`, no output dirs, and there is no Stage‑2 manifest. So DS‑10 is the
*most* work (a full Stage‑2 run + escalate, *then* Stage 3), not "awaiting Stage 3."

**Important platform note:** the datasets were accidentally copied to `D:` (slow SMR HDD) and are being
re-copied to **`C:` (SSD)**. **Run everything on `C:`** once each lands; the `D:` copies are redundant and
slow. DS‑09 already lives on `C:` and is ready right now.

---

## 2. THIS MACHINE (the old execution doc is wrong about all of this)

| Thing | OLD doc said | **This machine** |
|---|---|---|
| Data drive | `D:\Carlo\Projects\…` | **`C:\Projects\Data\epstein\DataSet-NN\VOL000NN\IMAGES\…`** (SSD) |
| Interpreter | `…envs\doc-scanner\python.exe` (PyMuPDF 1.27.1) | **`C:\Users\carlo\anaconda3\python.exe`** (PyMuPDF 1.27.2; `requests` present; no `openai` SDK needed — Stage 3 uses raw HTTP) |
| Package | repo over UNC | **`C:\epstein-ingest\src`** (`PYTHONPATH=C:\epstein-ingest`; has `stage3` — but **verify it's current**, re-sync from `\\wsl$\Ubuntu-22.04\home\cortega\repos\epstein-ingest\src` if unsure) |
| Azure creds | `~/repos/sv-kb/.env.local` | **`\\wsl$\Ubuntu-22.04\home\cortega\.env.local`** (the `sv-kb` repo is NOT on this machine). Has `AZURE_OPENAI_ENDPOINT/API_KEY/CHAT_API_VERSION/CHAT_NANO_DEPLOYMENT` (+ vision/embed/transcribe slots for later stages). |
| WSL home / distro | `home/carlo`, `ubuntu` | `home/cortega`, **`Ubuntu-22.04`** |
| Layout | nested `IMAGES\NNNN` | DS‑10/11/12 **nested**; **DS‑09 is FLAT** (`IMAGES\*.pdf`) — changes how you drive it (§6) |

You can run cleanly from **PowerShell** (no `D:`/MSYS path-mangling needed since data is on `C:`). The Git‑Bash
per-subfolder pattern from the old doc still works too; just swap the paths/interpreter/creds above.

---

## 3. The plan & order

**Stage-3-ready order (text track):**
1. **DS‑09 — run Stage 3 NOW.** It's ready (`stage2=true` everywhere, `stage3=false`). Caveat: it's **flat**
   and now full of Stage‑2 output dirs → the naive `find_pdfs` walk is slow (§6/§9). Mitigate before launching.
2. **DS‑11 — resume Stage 3** (per-subfolder loop; done folders skip). Then it's complete.
3. **DS‑12 — done.** Verify the manifest only.
4. **DS‑10 — run Stage 2 first** (`--no-stage-pages`, the DS‑09 playbook), **then** Stage 3. This is a
   multi-hour Stage‑2 run (503k PDFs) before any Stage 3 is possible.

**Disk/alignment track (independent — see §5):** purge DS‑11/DS‑12 `pages/` early (reclaim disk on C:);
escalate DS‑10/11/12 before Stage 4 (not before Stage 3).

---

## 4. Run mechanics (PowerShell, this machine)

**Load creds (names only printed; never echo the key):**
```powershell
$envfile = "\\wsl$\Ubuntu-22.04\home\cortega\.env.local"
Get-Content $envfile | Where-Object { $_ -match '^\s*AZURE_[A-Z0-9_]+\s*=' } | ForEach-Object {
  $k,$v = $_ -split '=',2; Set-Item "env:$($k.Trim())" (($v -replace '\s*#.*$','').Trim())
}
$env:PYTHONPATH = "C:\epstein-ingest"
$py = "C:\Users\carlo\anaconda3\python.exe"
```

**Dry-run first (free — projects eligible pages + cost, no API calls):**
```powershell
& $py -m src.stage3 "C:\Projects\Data\epstein\DataSet-11\VOL00011\IMAGES\0001" --dry-run
```
Check the first output lines say `deployment=gpt-4.1-nano`, `reasoning_shape=False`.

**Nested dataset (DS‑10/11/12) — per-subfolder loop (the canonical pattern; cheap resumes, instant start):**
```powershell
$img   = "C:\Projects\Data\epstein\DataSet-11\VOL00011\IMAGES"
$cflog = Join-Path $img "content-filter-failures.json"
$start = "0001"   # bump to the frontier to skip done folders
Get-ChildItem $img -Directory | Sort-Object Name | Where-Object { $_.Name -ge $start } | ForEach-Object {
  Write-Host ">>> $($_.Name) $(Get-Date -Format HH:mm:ss)"
  & $py -m src.stage3 $_.FullName -j 32 --progress-every 500 --content-filter-log $cflog
}
```
- **No `--force`** (done docs skip on their `stage3` block). `--content-filter-log` at the dataset IMAGES root
  so all subfolders merge into one failures log. Each subfolder also writes its own `stage3-run-manifest.json`.
- **Find the DS‑11 resume frontier** (first folder whose first sidecar lacks `stage3`) instead of re-scanning
  done folders:
```powershell
Get-ChildItem $img -Directory | Sort-Object Name | ForEach-Object {
  $s = Get-ChildItem $_.FullName -Filter *.json | Select-Object -First 1
  if ($s -and -not ((Get-Content $s.FullName -Raw | ConvertFrom-Json).stage3)) { $_.Name; break }
}
```

**Flat dataset (DS‑09):** no `NNNN` folders to loop. Point at the IMAGES root, but **mitigate the slow walk
first** (§9): either (a) add a pruned walk to `src/stage3/walk.py` mirroring
`src/stage2/escalate.py::_find_pdfs_pruned` (skips the per-PDF output dirs — recommended), or (b) accept a
multi-minute startup walk and run once (resume re-walks). Then:
```powershell
& $py -m src.stage3 "C:\Projects\Data\epstein\DataSet-09\VOL00009\IMAGES" -j 32 --progress-every 2000 `
  --content-filter-log "C:\Projects\Data\epstein\DataSet-09\VOL00009\IMAGES\content-filter-failures.json"
```

**Concurrency/resume/monitor:** `-j 32` ≈ 7.8 docs/s (threads, not agents; ~1.5 s/nano call; 0 throttling seen,
push `-j48` if clean). Resume skips any doc with a `stage3` block. Monitor the streamed `... n/N pdfs` lines +
per-subfolder summary (pages summarized/skipped/errored/safety-review/content-filtered/spend). Keep the
machine awake (`powercfg /change standby-timeout-ac 0`); ensure **exactly one** python worker before relaunch.

---

## 5. Escalate & Purge — bring DS‑10/11/12 to DS‑09's Stage‑2 end-state (Stage‑4 prep, independent of Stage 3)

These are **Stage‑2 maintenance passes** (`src/stage2/escalate.py`, already built; see the Stage‑2 spec's top
amendment). They do **not** affect Stage 3 (Stage 3 reads text + `page_kind`, never `images/`, `pages/`, or
`image_assets`), so run them on whatever track is convenient. **DS‑09 already has both done.**

- **`--escalate-vision`** — renders `low_quality_text` pages into `<name>/images/` and registers them in
  `image_assets[]` (`route_reason:"low_quality_text"`), forward-filling the over-inclusive vision set
  (`image ∪ mixed ∪ low_quality_text`). Idempotent. Needed on **DS‑10/11/12** before Stage 4.
- **`--purge-staged-pages [--confirm]`** — deletes the deferred `<name>/pages/` display renders (regenerated
  at Stage 7 on ingest); keeps `images/`/`artifacts/`/`thumbnail.png`; nulls dangling non-image `render_path`.
  Dry-run unless `--confirm`. **DS‑11/DS‑12 used full-staging so they have big `pages/` trees — purge reclaims
  the disk** (DS‑09 reclaimed ~68.6 GB this way).

```powershell
# DS-11 example (do per dataset; DS-12 likewise):
& $py -m src.stage2 "C:\Projects\Data\epstein\DataSet-11\VOL00011" --escalate-vision -j 14
& $py -m src.stage2 "C:\Projects\Data\epstein\DataSet-11\VOL00011" --purge-staged-pages         # dry-run report
& $py -m src.stage2 "C:\Projects\Data\epstein\DataSet-11\VOL00011" --purge-staged-pages --confirm # then delete
```

**Per-dataset matrix:**

| Dataset | Escalate | Purge | When |
|---|---|---|---|
| DS‑09 | ✅ done | ✅ done | — |
| DS‑10 | run Stage 2 `--no-stage-pages` then `--escalate-vision` (no `pages/` ever created → nothing to purge) | n/a | with its Stage 2 |
| DS‑11 | needed | **recommended early** (disk) | purge anytime; escalate before Stage 4 |
| DS‑12 | needed | recommended (small) | anytime before Stage 4 |

**Recommended timing:** purge DS‑11/DS‑12 **early** (reclaims C: disk for DS‑10's Stage 2); escalate them
**just before Stage 4**.

---

## 6. Content-filter & safety governance (unchanged — carry from old doc §7)

- An Azure block (HTTP 400 `content_filter`, or 200 `finish_reason=content_filter`) is **not** an error:
  Stage 3 sets `text_safety={flag:"review", content_filtered:true}` + a `content_filtered` flag, writes **no
  summary**, counts it, and appends `{file,page,timestamp,error}` to `content-filter-failures.json`.
- **Blocked pages are the CSAM/minors quarantine bucket.** On DS‑12 the whole blocked set was `sexual:high`,
  ~84% with a minor indicator. **Minors-related content stays quarantined → human review + mandatory
  reporting; do NOT pursue an exemption to auto-summarize it.** Adult high-severity recovery needs the Azure
  **Limited Access "modified content filter."** Filter set to **"Lowest blocking"**, **Prompt Shields Off**.
- `text_safety` is **`ok | review` only** (no `block`) — sv‑kb policy: *label, don't block*. `review` rate is
  corpus-dependent (DS‑12 57%, DS‑11 ~9%) — not always tiny.
- DS‑11 already has a `content-filter-failures.json` (104 entries up to ~EFTA0249xxxx); resuming merges into it.

---

## 7. Deep context (why things are the way they are)

- **The corpus is ~entirely scanned-image PDFs + an OCR text layer** (`is_full_page_image` ~93%,
  `max_image_coverage` ≈ 0.9778). Stage 3 lives off the OCR text; `page_kind` (Stage 2) decides eligibility.
- **`low_quality_text`** = pages whose OCR Stage 1 judged garbage (handwriting/poor scans) that still routed to
  `text`. Stage 3 **skips them by default** (their text isn't trustworthy); they are instead **escalated to
  vision** at Stage 2/Stage 4 (that's what `--escalate-vision` is for). So Stage 3 skipping them is correct,
  not a loss.
- **Over-inclusive vision routing** (`image ∪ mixed ∪ low_quality_text`) and the **`has_visual_content` gate**
  are Stage‑4 concepts — see [`HANDOFF-stage4-image-vision.md`](HANDOFF-stage4-image-vision.md). They don't
  touch Stage 3 but explain why escalate exists.
- **Stage 5 depends on Stage 3 + Stage 4:** only docs with ≥1 image page that gained a Stage‑4 caption get
  re-summarized; text-only docs just promote their provisional Stage‑3 summary to final. So finishing Stage 3
  is necessary but the *final* doc summary waits on Stage 4 for image-bearing docs.
- **Out of current scope:** audio/video natives (DS‑10 has ~874 `.mp4/.mov/.m4a/…` in `NATIVES`) and the
  `DATA\*.OPT/*.DAT` eDiscovery load files. The pipeline is **PDF-only**. (Note `~/.env.local` has an
  `AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT` — A/V transcription is a *future* pipeline, not built.)
- **Batch mode** (`--batch`, ~50% cheaper) is **not usable at full-dataset scale** — Azure caps a batch at
  ~100k requests; multi-job chunking is unimplemented (`src/stage3/batch.py`). Use realtime per-subfolder.

---

## 8. Gotchas / failure modes (current machine)

| Symptom | Cause | Fix |
|---|---|---|
| DS‑09 run "alive" but 0 docs for many minutes at start | flat `IMAGES` + ~528k Stage‑2 output dirs → `find_pdfs` `os.walk` descends into them (Handoff §4.3 slow-walk) | add a pruned walk to `src/stage3/walk.py` (copy `escalate.py::_find_pdfs_pruned`), or accept the one-time walk |
| Nested dataset stalls at start | same walk over the whole `IMAGES` tree | drive it **per-subfolder** (§4), never the whole root |
| `connection`/auth error | creds not loaded, or pointed at `sv-kb/.env.local` | use **`~/.env.local`** (§2); confirm `AZURE_OPENAI_*` are in `env:` |
| `ImportError: src` | `C:\epstein-ingest\src` stale or `PYTHONPATH` unset | re-sync `src` from the repo; set `PYTHONPATH=C:\epstein-ingest` |
| Every call 400 content_filter / floods | Prompt Shields on, or filter at "Highest blocking" | fix the deployment filter (Shields Off, "Lowest blocking") |
| Two runs racing the same sidecars | old loop still alive | confirm exactly one `python.exe` (`tasklist`), kill strays, then relaunch |
| Long run dies overnight | machine slept / task reaped | keep awake (`powercfg`), use a scheduled-task + backstop for unattended runs (see memory `dataset09-stage2-run`) |

---

## 9. Healthy-run checklist

- [ ] Target on **C:**; `status:"ok"` + `stage2` block present on a sampled sidecar.
- [ ] Creds loaded from `~/.env.local`; `PYTHONPATH=C:\epstein-ingest`; `src/stage3` importable.
- [ ] `--dry-run` shows sane eligible-page count + cost; `deployment=gpt-4.1-nano`, `reasoning_shape=False`.
- [ ] Nested → per-subfolder loop; flat (DS‑09) → pruned walk applied.
- [ ] First subfolder starts within seconds (no multi-minute stall).
- [ ] Per-subfolder summaries show `0 errored`; content-filtered counts plausible (genuine `sexual:high`, no jailbreak floods).
- [ ] Exactly one python worker; machine kept awake.

---

## 10. Bottom line

- **DS‑12: done.** **DS‑11: resume Stage 3** (per-subfolder, from the frontier) → done. **DS‑09: run Stage 3
  now** (apply the pruned-walk fix for its flat layout first). **DS‑10: run Stage 2 (`--no-stage-pages`) +
  escalate, *then* Stage 3** — it was never Stage‑2'd.
- **Escalate/purge are Stage‑4 prep, independent of Stage 3.** Purge DS‑11/DS‑12 early for C: disk; escalate
  10/11/12 before Stage 4. DS‑09 is already uniform.
- Run on **C:**, native python, creds from **`~/.env.local`**; the old execution doc's mechanics are valid but
  its paths/state are stale.

## 11. References
- `HANDOFF-stage3-execution.md` (detailed run mechanics — content-filter, concurrency, batch; OLD-machine paths),
  `HANDOFF-stage3-text-summaries.md` (design).
- `HANDOFF-stage2-render-classify-register.md` (Stage 2 + escalate/purge), `src/stage2/escalate.py`.
- `HANDOFF-stage4-image-vision.md` (Stage 4 — **built + gate-validated 2026-06-19**; the `has_visual_content`
  gate; runs after Stage 3 / after DS-10/11/12 reach DS-09's uniform Stage-2 end-state).
- `STRATEGY-pdf-ingestion-stages.md` → "Update — DataSet-09 Stage 2 …" (over-inclusive routing, the corpus reality).
- Memory: `dataset09-stage2-run` (DS‑09 flow, escalate/purge, scheduled-task pattern, dataset states),
  `stage1-execution-env`, `dataset09-stage1-run`.
