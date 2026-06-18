# Session handoff: new machine reality + Stage-2 prep (written 2026-06-18)

**Read this before running Stage 2.** A previous session ran Stage 1 on this *new* machine and discovered the
existing `docs/HANDOFF-*.md` runbooks are written for a DIFFERENT box (old paths/user/distro). This doc
records what's actually true here so Stage 2 starts smoothly. Stage 1 on **DataSet-09 is COMPLETE & verified**.

---

## 1. The machine is NOT what the old runbooks describe

| Old runbooks say | Reality on THIS machine |
|---|---|
| WSL home `…/home/carlo`, distro `ubuntu` | `…/home/cortega`, distro **`Ubuntu-22.04`** |
| `D:` not mounted in WSL | **`/mnt/d` and `/mnt/c` ARE mounted** |
| Data at `D:\Carlo\Projects\…`, use `doc-scanner` conda env | `doc-scanner` env doesn't exist; see §2 |
| Process data on `D:` | **`D:` is a slow SMR HDD — DO NOT process on it** (see §3) |

Windows user is still `carlo` (`C:\Users\carlo`).

## 2. Python environments — both ready, PyMuPDF PINNED

**CRITICAL: PyMuPDF must be 1.27.2.** Version **1.27.2.3 has a fatal C-level crash** (`none_dealloc`
SIGABRT in `page.get_texttrace()` — uncatchable). This bites Stage 2 too (it imports `fitz` for rendering).
`pyproject.toml` now pins `PyMuPDF==1.27.2` (committed on branch `fix/pin-pymupdf`, commit `a577995`).

Two working interpreters, both with PyMuPDF 1.27.2:
- **Native Windows (USE THIS for the real run):** `C:\Users\carlo\anaconda3\python.exe` (Python 3.12.7).
  A copy of the package is at **`C:\epstein-ingest\src`** (so the run doesn't depend on WSL being up).
- **WSL venv (handy for local/test):** `~/repos/epstein-ingest/.venv` (pip was bootstrapped via get-pip.py
  because WSL's system python had no pip/ensurepip).

## 3. Disk lessons (this is why Stage 1 took a detour)

- `D:` = **Seagate ST8000DM004, an 8 TB SMR HDD**. Stage 1's ~1M small random I/O ops crawled at **1.4 files/s**.
- `C:` = **NVMe SSD**. The data was staged to `C:` and everything got fast.
- **WSL over `/mnt/c` (9p) only reached ~7.5/s** — 9p per-syscall latency left the CPU 35% idle.
- **Native Windows reading `C:\` directly was CPU-bound at ~11–55/s** (cores ~95%). This is the way.
  → **Run Stage 2 natively against `C:\`, `-j 14` (16 cores − 2). More workers won't help (CPU-bound).**

**The native bridge command pattern (from Git Bash):**
```bash
MSYS_NO_PATHCONV=1 PYTHONPATH='C:\epstein-ingest' \
  "/c/Users/carlo/anaconda3/python.exe" -u -m src.stage2 \
  "C:\Projects\Data\epstein\DataSet-09" -j 14 2>&1 | tee /c/epstein-ingest/dataset09-stage2.out.txt
```
- `MSYS_NO_PATHCONV=1` stops Git Bash mangling the `C:\` paths.
- **Never put `$VAR` inside an inline `wsl … bash -lc '…'`** (MINGW eats it) — write a script file instead.

## 4. Where the data is

- **Run root: `C:\Projects\Data\epstein\DataSet-09`** (`/mnt/c/Projects/Data/epstein/DataSet-09` from WSL).
- PDFs + Stage-1 sidecars (`<name>.json`) are together in `…\VOL00009\IMAGES` (~528,740) plus sibling dirs
  (CORRUPTED / DB-EXTRACT / NATIVES / EMPTY / …). **528,995 sidecars total, 0 errors.**
- The original `D:` copy was cleaned back to pristine PDFs (no sidecars).

## 5. Stage 2 specifics (from the code)

- **Local & offline — no Azure/network.** (`.env.local` creds are for Stages 3–6 only.) Invoke: `python -m src.stage2 ROOT`.
- Reads each Stage-1 sidecar **in place** and extends it with a `stage2` block; renders go to **`<name>/pages/`**,
  photo artifacts to **`<name>/artifacts/`** (deduped by content hash), plus a per-PDF thumbnail. Manifest:
  `stage2-run-manifest.json` at the root.
- **Resumable:** skips any PDF whose sidecar already has a `stage2` block (use `--force` to redo). Same
  `-j`, `--generated-at`, `--progress-every N` (default 2000) flags. It re-reads Stage-1 thresholds from each
  sidecar's `generator.config`, so classification matches exactly what Stage 1 used.

## 6. ⚠️ Page staging vs. disk space — ALREADY UNDERWAY (see memory `dataset09-stage2-run`)

> **UPDATE 2026-06-18:** Stage 2 has already been launched and progressed by another session. The live,
> authoritative record (run config, ops, gotchas, findings) is the memory file **`dataset09-stage2-run`** —
> read it first. The numbers below are the CORRECTED reality; my earlier estimate here was wrong.

`Stage2Config.stage_pages` defaults **True** → renders **every non-image page** to PNG under `<name>/pages/`.

**CORRECTED disk math** (empirical, supersedes any "~1.12M pages / ~220 GB" figure I wrote earlier — that was
wrong): the corpus is **~5–7 pages/PDF (~3M+ pages)**; full staging costs **~1.7 MB/PDF ≈ ~900 GB** for all
529k — **impossible** on this disk (`C:` ~291 GB free). Size full-render space at **~1.7 MB/PDF**, not pages×DPI.

**What was actually done (the do-half decision, executed):** full staging for the **first ~78,000 PDFs**
(they keep full page renders), then the remainder switched to **`--no-stage-pages`** (full-page renders
deferred to Stage 7, which regenerates them on ingest; artifacts + thumbnails + image-page renders still
staged). Net = a **mixed dataset**, which is fine. After the switch, disk use went flat (remainder ≈ +15–20 GB).

## 7. Operating the in-flight Stage 2 run (from memory `dataset09-stage2-run`)

- **Run root:** `C:\Projects\Data\epstein\DataSet-09\VOL00009` (528,995 PDFs). Native `anaconda3\python.exe`,
  PyMuPDF 1.27.2, `-j 14`. Log: `C:\epstein-ingest\dataset09-stage2.out.txt`.
- **How it runs (survives logoff):** Scheduled Task **`epstein-stage2-ds09`** (S4U, 30-min auto-restart
  backstop) → `stage2-ds09-guard.ps1` (no double-launch; stops relaunching once the manifest exists) →
  `stage2-ds09-run.cmd`.
- **⚠️ S4U kill gotcha:** workers run detached in the task's S4U session. `Stop-ScheduledTask` does NOT kill
  them, and non-elevated `Stop-Process`/`taskkill` gets Access Denied. **To stop/restart you MUST use an
  ELEVATED shell:** `taskkill /F /IM python.exe`.
- **Monitor:** `Get-Content C:\epstein-ingest\dataset09-stage2.out.txt -Tail 25`; free disk `(Get-PSDrive C).Free/1GB`.
  From a non-elevated shell you canNOT read CPU/cmdline of the S4U python procs — judge progress by PNG count + disk.
- **Done when:** `…\VOL00009\stage2-run-manifest.json` exists (written only on clean finish); verify
  `pdfs_errored` and `pdfs_no_sidecar` ≈ 0. To retire: `Unregister-ScheduledTask -TaskName epstein-stage2-ds09` (elevated).

## 8. Pointers
- Memory files: `stage1-execution-env`, `dataset09-stage1-run`, **`dataset09-stage2-run`** (live Stage-2 ops + vision-workload findings).
- The PyMuPDF fix is on branch `fix/pin-pymupdf` (not yet merged to `main`).
- Run logs / helper scripts (provenance): `C:\epstein-ingest\` (`dataset09-native.out.txt`, `…-verify.out.txt`, `dataset09-stage2.out.txt`, `register-stage2-task.ps1`, `switch-to-nostage.ps1`, etc.).
