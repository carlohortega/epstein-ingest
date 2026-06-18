# Handoff: EXECUTING Stage 1 (text-first PDF extraction) — operational runbook

**You are a fresh session whose job is to RUN Stage 1 on an Epstein dataset** — not to build the tool
(it's built and battle-tested) but to execute it correctly, unattended, and recognize/handle the failure
modes that have already cost real time. Read this whole doc before launching anything.

- **What Stage 1 does:** walks a folder of PDFs, writes a sidecar `<name>.json` next to each `<name>.pdf`
  (verbatim reading-order text, `[REDACTED]` markers, per-page metrics, a routing hint), plus a root
  `extract-run-manifest.json`. 100% local, deterministic, no network/DB. The spec is
  [`HANDOFF-text-first-pdf-extraction.md`](HANDOFF-text-first-pdf-extraction.md); the umbrella plan is
  [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md). This doc is the *runbook*.
- **Status:** DataSet-12, DataSet-11 (~331k files), DataSet-10 (~504k files) are **Stage-1 complete** —
  their sidecars are on disk. New datasets follow the exact same procedure.

---

## 1. Where everything lives

| Thing | Location |
|---|---|
| Code (this repo) | `~/repos/epstein-ingest/` — package `src`, Stage 1 = `src/stage1` (run `python -m src.stage1`) |
| Data | `D:\Carlo\Projects\Data\epstein\DataSet-NN\VOL000NN\IMAGES\NNNN\*.pdf` (~1000 PDFs per `NNNN` dir) |
| Output | `<name>.json` beside each PDF + `extract-run-manifest.json` at the dataset root |
| Run root | **Always use the dataset root** `D:\...\DataSet-NN` so `source.relative_path` is consistent |

**The sidecars are the product — never delete them.** They are the canonical extracted text that Stages
2–7 consume; deleting means re-running the entire multi-hour/day extraction.

---

## 2. The execution environment — read this or you WILL get stuck

This is a Windows box. The data and the toolchain are split, which is the #1 source of confusion:

- **`D:` is NOT mounted in WSL** (`/mnt/d` is absent; you cannot self-mount — `sudo` needs a password).
  `D:` *is* visible from the **Windows/Git-Bash** side as `/d/`, and to Windows programs as `D:\`.
- **The WSL venv cannot read `D:`.** So you CANNOT use `run.sh` (which uses the sv-kb WSL venv) for the
  real data. `run.sh` is only for WSL-local test data.
- **Use the Windows `doc-scanner` conda env** to run against `D:`:
  `C:\Users\carlo\anaconda3\envs\doc-scanner\python.exe` (has PyMuPDF 1.27.1). It can read `D:` and
  import the package over the WSL UNC path.
- **MINGW mangles inline bash.** In the `Bash` tool, `wsl.exe ... bash -lc '...$VAR...'` silently eats
  `$VAR` assignments/expansions. **Use literal paths or write a script file** — never variables inside an
  inline `bash -lc`. (This bit us repeatedly.)

**The bridge command (one-off run against `D:`):**
```bash
MSYS_NO_PATHCONV=1 PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest' \
  "/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe" -m src.stage1 \
  "D:\Carlo\Projects\Data\epstein\DataSet-NN" -j 11 --skip-existing
```
- Forward-slash UNC for `PYTHONPATH`; backslash Windows path for the data root.
- For an **unattended** scheduled run, do NOT depend on the UNC (it needs WSL up): **copy the `src`
  package to a Windows-local dir** and point `PYTHONPATH` at that (see §4).

**Flags that matter:** `-j N` workers (use ~11 on a 12-core box); `--skip-existing` (resume — see §3);
`--generated-at <ISO8601>` (fixed timestamp → byte-reproducible re-runs). All thresholds are `--flags`
echoed into each JSON's `generator.config`.

---

## 3. The non-negotiable operational facts (hard-won)

1. **`--skip-existing` skips on sidecar EXISTENCE (a stat), not by reading files.** This is essential and
   already in the tool. An earlier version opened+parsed every sidecar each pass — at 100k+ files on an
   AV-scanned disk that "skip-scan" took **hours** and looked exactly like a hang. With existence-skip a
   full resume pass is seconds-to-minutes.
2. **AV (Windows Defender) is the dominant slowdown.** Every PDF read + sidecar write is scanned; with N
   workers reading large files this thrashes the disk. **Add an exclusion (elevated PowerShell):**
   ```powershell
   Add-MpPreference -ExclusionPath "D:\Carlo\Projects\Data\epstein"
   ```
   This is the difference between ~3 files/s and ~14 files/s. It needs admin — you (the model) can't
   apply it; ask the user. It's the single biggest lever, especially for image-heavy datasets.
3. **Some datasets are image-heavy and inherently slow.** DataSet-10 averaged ~5.8 MB/PDF with clusters of
   **35–54 MB, 100+ page scans** that take ~40s of CPU *each* (and much longer under I/O contention). These
   are **legitimate documents — never quarantine them.** The tool already optimizes this (no-bar pages use
   cheap span-level `dict`, not per-glyph `rawdict`), but big clusters are still slow without the AV
   exclusion.
4. **Sleep / lock / logoff kills the run.** The scheduled task runs as the user *only while logged on*; a
   sleep or session-lock terminates the whole task tree. Mitigations are in §4 (repeating backstop trigger)
   and: keep the machine awake — `powercfg /change standby-timeout-ac 0` (on AC).
5. **The tool is hardened** (in `src/stage1/walk.py`): **bounded in-flight window** (memory-safe at 1M
   files), a **`[progress]` heartbeat** every 60s to stdout, and **stall recovery** (kills+recycles the
   pool and quarantines a file as `timeout` if *nothing* completes for `stall_timeout_seconds`). Note the
   stall recovery only catches a *total* stop, not a slow crawl through big files.

---

## 4. Unattended, resumable runs — the scheduled-task pattern

For anything beyond a quick run, use a Windows Scheduled Task driving a PowerShell wrapper loop. The
templates from the last runs live in `C:\Users\carlo\text-first-extract\` as `run-dataset10.ps1` /
`run-dataset11.ps1` and `summarize.py` — **copy and adapt** (they point at the *old* package dir, so update
the `PYTHONPATH`/package path to the new Windows-local copy of `src`).

Setup steps:
1. **Copy the package to a Windows-local dir** so the task doesn't depend on WSL being up:
   `Copy-Item "\\wsl.localhost\ubuntu\home\carlo\repos\epstein-ingest\src" -Destination "C:\Users\carlo\epstein-ingest\src" -Recurse -Force` (pick a stable dir; set `PYTHONPATH` to its parent).
2. **Wrapper (`run-datasetNN.ps1`)** loops: run `python -u -m src.stage1 <root> -j 11 --skip-existing`
   (redirect stdout to `datasetNN.out.txt` for the live heartbeat), read the manifest, and declare DONE
   only after several consecutive **stable** passes (`files_extracted == 0` and `files_seen` unchanged) so
   late-added files are absorbed; on DONE it writes a marker, runs `summarize.py`, and unregisters itself.
   It must NEVER reboot the machine — "resume" only means it continues if something else restarts it.
3. **Triggers:** `Once` (start now) **+ a 30-min repeating backstop** (`-RepetitionInterval 30min`,
   indefinite) **+ AtLogOn**, with settings `-MultipleInstances IgnoreNew -StartWhenAvailable
   -ExecutionTimeLimit 0 -RestartCount 3`. The **repeating backstop is critical** — without it, a sleep/
   lock leaves the task `Ready` and it never resumes (this stranded a run for an hour). With it, the run
   resumes within ~30 min of the machine being available.
4. **Start it**, then verify it's producing (§5).

> Cleanup when done: `Stop-ScheduledTask` + `Unregister-ScheduledTask` for `TextFirstExtract_*`, and
> `Stop-Process -Name python`. Confirm `Get-ScheduledTask -TaskName 'TextFirstExtract_*'` returns nothing
> so nothing starts on reboot.

---

## 5. Monitoring — DON'T trust the log; trust these

The wrapper's run log only writes a line at **pass completion**. A pass over a large dataset is silent for
its whole duration — the initial **walk of ~500k files alone is ~5–9 minutes of quiet** (no sidecars, no
heartbeat yet). **A silent log mid-pass is normal, not a stall.** Instead:

- **Live progress:** `Get-Content C:\Users\carlo\...\datasetNN.out.txt -Tail 5` → the `[progress] done/total
  rate eta` heartbeat (every 60s). The cumulative rate is noisy for the first ~20 min; let it settle.
- **Sidecar count:** the newest `IMAGES\NNNN` dir filling toward 1000 `.json` files.
- **Liveness:** Task Manager → Background processes → `python.exe` ×(workers+1), accumulating CPU. There is
  **no console window** (the task runs hidden) — "I don't see the process" usually means you're looking for
  a window that doesn't exist.

**Distinguishing the failure modes** (all seen this session):
| Symptom | Cause | Action |
|---|---|---|
| `python` procs gone, task `Ready`, log frozen | sleep/lock killed the task | restart; ensure the repeating backstop + no-sleep |
| 1 `python` proc, low CPU, no sidecars for many min | stuck in the slow per-file skip-scan (only if `--skip-existing` is reading files — should be fixed) / huge walk | verify existence-skip; wait out the walk |
| 11 procs, ~65% CPU, heartbeat barely advancing (~1/min) | I/O contention on a cluster of big scanned files | apply the AV exclusion; do NOT quarantine — they're legit |
| heartbeat advancing fine, one dir stays at 999/1000 | one slow/large file still processing | normal; the dir completes when it finishes |

---

## 6. Verifying completion

Read the dataset's `extract-run-manifest.json` (it reflects the latest pass). **Done when:**
`files_seen == files_skipped`, `files_extracted == 0`, `files_errored == 0`. That means every PDF has a
sidecar and a pass found nothing new. The wrapper then writes a `DONE` marker + a summary and unregisters.
(Cumulative page/routing totals: sum the per-pass lines in the run log, or run `summarize.py <root>`.)

---

## 7. What the output means (so you set expectations correctly)

- **`needs_vision` in the sidecars is the OLD, conservative flag** (`redaction bar → vision`). On these
  corpora that flags ~70% of pages — but **~99.5% of redacted pages still have a full text layer**, so the
  REAL vision need is **~2%** (only true image-only pages). **Do not report the `needs_vision` count as the
  vision workload.** Stage 2 derives the real `page_kind` routing from metrics already in the sidecars —
  **no Stage-1 re-run is needed** for that.
- Sidecars contain document text incl. names + `[REDACTED]` markers where bars were detected. Treat as
  sensitive; store under the same access controls as the PDFs.

---

## 8. Runbook for a NEW dataset (the checklist)

1. **AV exclusion** in place for `D:\Carlo\Projects\Data\epstein` (ask user, elevated) — do this first.
2. **Keep the machine awake** on AC (`powercfg /change standby-timeout-ac 0`).
3. **Confirm the dataset**: `ls /d/Carlo/Projects/Data/epstein/DataSet-NN/VOL000NN/IMAGES` → count dirs ×
   ~1000 ≈ file count; note 0 existing `.json` (fresh) for the ETA.
4. **Refresh the Windows-local package copy** from `~/repos/epstein-ingest/src`.
5. **Create the wrapper + scheduled task** (§4) pointing at the dataset root; `-j 11 --skip-existing`.
6. **Start**, then **verify producing** (§5): after the ~5–9 min walk, sidecars appear and the heartbeat
   advances. A background watcher polling the first dir's `.json` count is a good confirmation.
7. **Monitor** via `out.txt`; expect the quiet walk window; don't restart on a silent log.
8. **Verify completion** via the manifest (§6); then **clean up** the task (§4) so nothing auto-starts.

When in doubt, the levers are: AV exclusion (biggest), keep it awake, and `--skip-existing` for fast
resume. The data being large/slow is usually the dataset, not a bug — confirm with the heartbeat rate
before assuming a stall.
