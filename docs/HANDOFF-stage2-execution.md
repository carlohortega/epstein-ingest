# Handoff: EXECUTING Stage 2 (render + classify `page_kind` + register image assets) — operational runbook

**You are a fresh session whose job is to RUN Stage 2 on an Epstein dataset** — the tool is built,
tested, and has already processed DataSet-12 and DataSet-11 end-to-end. Your job is to execute it
correctly and unattended, and to recognize the failure modes that have already cost real time this
session (a worker-starvation stall, an OOM from running alongside other jobs, a disk-fill risk, and a
catastrophically slow naive sidecar walk). **Read this whole doc before launching anything.**

- **What Stage 2 does:** for each PDF that has a Stage-1 sidecar, it **renders every page to PNG**,
  deterministically classifies each page's **`page_kind`** (`text|image|mixed|blank|redacted`), extracts
  embedded **photo artifacts** (deduped), makes one **thumbnail** per PDF, and **extends the sidecar in
  place** with per-page `page_kind`/`render_path` and a top-level `image_assets[]` + `stage2` block. 100%
  local, deterministic, no network/DB. Build spec:
  [`HANDOFF-stage2-render-classify-register.md`](HANDOFF-stage2-render-classify-register.md); umbrella plan:
  [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md). **This doc is the runbook.**
- **Status:** **DataSet-12 ✅ and DataSet-11 ✅ are Stage-2 complete.** **DataSet-10 is NOT started** and is
  the pending job (see §9) — it is large enough that disk dictates how you run it.

---

## 1. Where everything lives

| Thing | Location |
|---|---|
| Code (this repo) | `~/repos/epstein-ingest/` — package `src`, Stage 2 = `src/stage2` (run `python -m src.stage2`) |
| Data | `D:\Carlo\Projects\Data\epstein\DataSet-NN\VOL000NN\IMAGES\NNNN\*.pdf` (~1000 PDFs per `NNNN` dir) |
| **Reads** | each PDF + its Stage-1 `<name>.json` (must be `status:"ok"` **with** Stage-1 done) |
| **Writes** | beside each PDF: `<name>/images/`, `<name>/pages/`, `<name>/artifacts/` PNGs + `<name>/thumbnail.png`; **extends `<name>.json`**; `stage2-run-manifest.json` at the run root |
| Run root | the dataset's `…\IMAGES` (or the `DataSet-NN` root) — see §5 on per-subfolder runs |

**Stage 2 is additive and safe to re-run** — it extends the sidecar and writes PNGs; it never deletes the
Stage-1 text. The renders/artifacts are large; the sidecars remain the canonical product.

---

## 2. The execution environment — same split as Stage 1 (read or you WILL get stuck)

- **`D:` is NOT mounted in WSL.** The WSL venv / `run.sh` can't see the real data — they're for WSL-local
  test data only. `D:` is visible to **Windows** programs as `D:\` and to Git-Bash as `/d/`.
- **Run against `D:` with the Windows `doc-scanner` conda env:**
  `C:\Users\carlo\anaconda3\envs\doc-scanner\python.exe` (PyMuPDF 1.27.1). It reads `D:` and imports the
  package over the WSL UNC path.
- **MINGW mangles inline bash:** in the `Bash` tool, `bash -lc '...$VAR...'` eats variable
  assignments/expansions and back-slash paths. **Use literal paths, single-quote the `D:\…` arg,
  forward-slash the script/PYTHONPATH, and prefix `MSYS_NO_PATHCONV=1`.** For a per-subfolder loop, build
  the Windows path with **`win=$(cygpath -w "$sub")`** — a literal `"D:\\…\\$name"` does NOT expand through
  the bash/MSYS layer (this bit both Stage-2 and Stage-3 runs).

**The bridge command (single run against `D:`):**
```bash
MSYS_NO_PATHCONV=1 PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest' \
  "/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe" -u -m src.stage2 \
  "D:\Carlo\Projects\Data\epstein\DataSet-NN\VOL000NN\IMAGES" -j 10 --progress-every 2000 \
  > /c/Users/carlo/stage2_dsNN.log 2>&1
```
- Forward-slash UNC for `PYTHONPATH`; backslash Windows path for the data; `-u` so the log is live.
- For **unattended** runs, copy `src` to a Windows-local dir and point `PYTHONPATH` there (don't depend on
  WSL being up) — same pattern as the Stage-1 scheduled task.

---

## 3. DISK is the #1 Stage-2 lever — size it BEFORE you run

Unlike Stage 1 (tiny JSON sidecars), **Stage 2 writes a PNG for (potentially) every page** — this is where
runs die. **Always estimate the render footprint against free space first.**

- **Measured rate:** full page renders ≈ **~196 KB/page** at the default 200 DPI.
- **`--no-stage-pages` mode** renders only the *image* pages + thumbnails + artifacts ≈
  `image_pages × 230 KB + PDFs × 29 KB`. Because these corpora are ~**98% text / ~2% image-only**, this is
  **dramatically smaller** and loses nothing for vision (image pages + artifacts are still staged; only the
  text-page *display* PNGs are deferred — **Stage 7 regenerates those on ingest**, per Strategy §3/§10).

**Worked numbers (so you trust the math):**
| Dataset | PDFs | pages | full renders | `--no-stage-pages` |
|---|---|---|---|---|
| DataSet-12 | 152 | 1,525 | ~0.3 GB | tiny |
| DataSet-11 | 331,655 | ~517,000 | ~97 GB | ~25 GB |
| **DataSet-10** | **503,154** | **~3,300,000** | **~621 GB** | **~33 GB** |

> **DataSet-10 full renders need ~621 GB.** Only run **full** renders if the target drive has **≥ ~700 GB
> free**; otherwise use **`--no-stage-pages`** (≈33 GB). Check free space first:
> `python -c "import shutil;print(shutil.disk_usage('D:/').free/1e9)"`. **A full-render run that fills the
> disk dies mid-way and leaves a partial set** — confirm headroom before launching.

---

## 4. The non-negotiable operational facts (hard-won this session)

1. **Resume is automatic and idempotent — there is NO `--skip-existing` flag.** Re-running the same command
   skips any PDF whose sidecar already has a `stage2` block (and skips individual pages/artifacts whose
   files exist). To resume an interrupted run, **just run the same command again** (no `--force`).
   `--force` *re-does* completed work — only use it to deliberately re-render.
2. **Don't run Stage 2 concurrently with other heavy jobs.** Running it (`-j10`) alongside the Stage-1
   scheduled loop (`-j8`), Stage-3 vision, and a `summarize.py` walk **OOM-killed the whole Stage-2 process
   group** mid-run this session (no traceback, all workers vanished, frontier frozen). On a 12-core / 32 GB
   box, **run Stage 2 alone**, or throttle `-j` if something else must run. Memory per worker is ~50–190 MB;
   the failure is contention, not a leak.
3. **The walk descends into Stage-2 output dirs on a resume.** `find_pdfs` (reused from `src/stage1/walk`)
   does a plain `os.walk` of the whole root and does **not** prune `images/`/`pages/`/`artifacts/`. On a
   fresh dataset that's fine; on a **resume**, the root now contains tens of thousands of output dirs and the
   startup walk crawls them — it took **~12 min just to walk** DataSet-11 once. **Mitigation: run
   per-subfolder** (loop over `IMAGES\NNNN`, §5) so each run only walks ~1000 files, or use the
   prune-and-fast-forward driver (`C:\Users\carlo\resume_ds11.py` — but **update its imports from
   `text_first_extract.stage2.*` to `src.stage2.*`** before reuse).
4. **Worker-starvation symptom (already fixed, watch for regressions).** An earlier `walk.py` submitted all
   N tasks to the pool at once → on Windows `spawn` the main process pinned one core at ~800 MB while the 10
   workers sat idle (only ~10 docs done in 17 min). The current code uses a **bounded in-flight window**
   (`max_inflight = workers*4`, `wait(FIRST_COMPLETED)`) — workers stay balanced and memory is flat. **If you
   ever see 1 python hogging CPU while the others idle, that's this regression — stop and check `walk.py`.**
5. **AV (Windows Defender) is the dominant slowdown** — every PDF read + PNG write is scanned. Ask the user
   to add an exclusion (elevated): `Add-MpPreference -ExclusionPath "D:\Carlo\Projects\Data\epstein"`. It's
   the difference between a crawl and a healthy render rate. You (the model) can't apply it — ask.
6. **Sleep / lock / logoff kills the run** (same as Stage 1). Keep the machine awake on AC
   (`powercfg /change standby-timeout-ac 0`); for long runs use the scheduled-task + repeating-backstop
   pattern from the Stage-1 runbook.
7. **Determinism + big pages are handled.** Identical input ⇒ byte-identical PNGs (fixed DPI, no timestamps),
   so a re-render is safe. A **DPI clamp** (40 MP ceiling) renders large-format pages at a lower effective
   DPI so they don't OOM — normal Letter pages always render at the full 200 DPI.

---

## 5. Running it

**Small/quick dataset:** the single bridge command in §2 (point it at `…\IMAGES`, `-j 10`).

**Large dataset (DataSet-10/11-scale) — prefer a per-subfolder loop** (avoids the §4.3 walk cost and lets you
watch disk per chunk). Sketch (run via the Windows conda python; build the Windows path with `cygpath`):
```bash
IMG='//wsl.localhost/...'   # NOT NEEDED for data; data path is Windows-style below
for sub in /d/Carlo/Projects/Data/epstein/DataSet-10/VOL00010/IMAGES/*/ ; do
  win=$(cygpath -w "$sub")
  MSYS_NO_PATHCONV=1 PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest' \
    "/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe" -u -m src.stage2 \
    "$win" -j 10 --no-stage-pages --progress-every 500 \
    >> /c/Users/carlo/stage2_ds10.log 2>&1
done
```
- Each subfolder writes its own little `stage2-run-manifest.json`; aggregate at the end (§7).
- **Resumable for free:** re-running the loop skips finished subfolders' PDFs (stage2 block) almost
  instantly, and a folder that died mid-way only re-walks its own ~1000 files.
- **Flags that matter:** `-j N` (~10 on a 12-core box, *alone*); **`--no-stage-pages`** (disk-constrained or
  huge mostly-text sets — see §3); `--force` (re-render); `--render-dpi 200` (default; legible for display +
  gpt-5-mini); `--progress-every N` (log a line every N PDFs). All thresholds are `--flags` echoed into
  `stage2.config`.

---

## 6. Monitoring — what healthy looks like, and the failure table

- **Live progress:** `tail -f /c/Users/carlo/stage2_dsNN.log` → lines like
  `... 88000/105655 (6.9/s, eta 43m) | rendered=… skipped=… image=… text=… vision=…`. The **first
  `find_pdfs` walk is silent for minutes** (normal — not a stall).
- **Process health (the real signal):** `Get-Process python` → **(workers+1) procs, all accumulating CPU
  roughly evenly**, each ~50–190 MB. One process hogging CPU while others idle = the §4.4 regression.
- **DISK (Stage-2-specific — watch it):** `(Get-PSDrive D).Free/1GB` trending down toward zero is the Stage-2
  killer. If it's dropping faster than your §3 estimate predicted, stop and switch to `--no-stage-pages`.

| Symptom | Cause | Action |
|---|---|---|
| 1 python proc ~800 MB pinned, workers idle, frontier barely moves | submit-storm regression (§4.4) | stop; verify bounded-window in `walk.py` |
| all python procs vanish, **no** `stage2-run-manifest.json`, log frozen | OOM/contention crash (§4.2) | it's resumable — re-run (or per-subfolder); stop competing jobs |
| disk free heading to 0 | full renders too big for the drive (§3) | switch to `--no-stage-pages`; the already-rendered work is kept |
| 11 procs balanced but slow (~5–7/s) | AV scanning + big scanned pages | apply the AV exclusion; large image pages are legit |
| startup silent ~10+ min on a resume | walk descending into output dirs (§4.3) | normal-ish; prefer per-subfolder next time |

---

## 7. Verifying completion

- **The `stage2-run-manifest.json` at the run root is written ONLY on a clean finish.** Its **absence means
  the run did not complete** (the segmented DataSet-11 runs that died left none — that's how we knew). The
  finishing log line is `DONE: N rendered, M skipped, 0 errored …`.
- **Tally `page_kind` across all sidecars to validate — but do it the RIGHT way.** A naive
  `glob("**/*.json")` descends into the ~1 PNG-per-page output dirs and is **catastrophically slow** (a
  cost-scan ran **5.5 h and never finished**). Instead: an `os.walk` that **prunes `images/pages/artifacts`**,
  parsing sidecars across a **process pool** (the `tally_ds11.py` pattern). Report: `page_kind` distribution,
  `vision_workload = image pages + kept artifacts`, `low_quality_text` count, artifacts, any sidecar still
  missing a `stage2` block.

---

## 8. What the output means (set expectations correctly)

- **Only `page_kind == image` pages (≈2%) go to vision (Stage 4).** A redacted page that still has a text
  layer is `text`/`mixed`, **not** vision — this is the corpus cost win. Ignore the old Stage-1 `needs_vision`
  flag as a workload measure.
- **`low_quality_text`** flags pages Stage-1 rejected as garbage OCR that still routed to `text` — they're
  kept in the text lane but flagged so Stage 3/6/7 can down-weight/escalate (~16% on DataSet-12; far fewer
  elsewhere).
- **`image_assets[]`** is the manifest Stage 4 iterates (image pages + kept photo artifacts), all relative
  paths.
- **Sanity baselines** (a wildly different split means investigate):
  - **DataSet-12:** 152 PDFs / 1,525 pages → 1,327 text · 190 image · 8 mixed · 0 blank · 0 redacted;
    vision_workload **202 (13%)**; 251 `low_quality_text`; 12 artifacts kept; ~2–3 min; ~0.3 GB full renders.
  - **DataSet-11:** 331,655 PDFs / ~517k pages → ~98% text-routed, ~**2%** image-only true vision; ~97 GB full
    renders (completed across four resumed segments).
- Sidecars contain document text incl. names + `[REDACTED]`; renders are images of the same. **Sensitive —
  store under the same access controls as the PDFs.**

---

## 9. Runbook checklist for the pending job (DataSet-10) and any new dataset

1. **Confirm Stage 1 is done** for the dataset: every `IMAGES\NNNN` has ~1000 `.json` sidecars, `status:"ok"`.
   (DataSet-10 is Stage-1 complete.) Stage 2 **skips any PDF without an ok sidecar** and records it.
2. **AV exclusion** for `D:\Carlo\Projects\Data\epstein` (ask user, elevated) — biggest perf lever.
3. **Keep the machine awake** on AC; for multi-hour runs use the scheduled-task + 30-min-backstop pattern.
4. **Size the disk (§3).** DataSet-10 ≈ **3.3 M pages → ~621 GB full**. With < ~700 GB free, **use
   `--no-stage-pages`** (≈33 GB). Check `shutil.disk_usage('D:/').free` first.
5. **Refresh the Windows-local `src` copy** (for unattended) or run over the UNC (interactive).
6. **Run alone** (no concurrent Stage-1/3 jobs), **per-subfolder loop** (§5), `-j 10`,
   `--no-stage-pages` (DataSet-10), `--progress-every 500`, logging to a file.
7. **Monitor (§6):** balanced workers, flat memory, and **free disk** vs your estimate. First walk per
   subfolder is quick (~1000 files); the run is resumable per-PDF and per-folder.
8. **Verify (§7):** every subfolder shows `DONE`; tally `page_kind` with the **pruned parallel** walk; expect
   ~98% text / ~2% image. Then report the aggregated breakdown.

When in doubt, the Stage-2 levers are: **disk sizing + `--no-stage-pages`** (the one that actually sinks
runs), **AV exclusion**, **run it alone**, and **per-subfolder** for anything big. The tool is solid; the
risks are operational — disk, contention, and the AV-scanned mass-file walk.
