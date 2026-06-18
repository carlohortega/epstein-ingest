# Handoff: Stage 3 — EXECUTION runbook (operating the stage on real datasets)

**Audience:** a session responsible for **running** Stage 3 on the Epstein datasets — not building it.
Stage 3 is built and validated; this is the operational playbook (how to launch, monitor, resume, and the
hard-won gotchas).
**Build spec / design:** [`HANDOFF-stage3-text-summaries.md`](HANDOFF-stage3-text-summaries.md) (requirements +
amendments box) and [`STRATEGY-pdf-ingestion-stages.md`](STRATEGY-pdf-ingestion-stages.md) (§Update section).
Read those for *why*; read this for *how to run*.

---

## 0. What Stage 3 does (30-second version)

For every Stage-2 `text`/`mixed` page it makes **one bundled gpt-4.1-nano call** → page `summary` +
`summary_json` (key_points/entities/topics) + `text_safety` triage flag, then a **provisional** per-document
summary. **Text only — no vision** (that's Stage 4). It **extends the local sidecar JSON in place** and writes
a run manifest + a content-filter failures log. **No Azure Storage, no DB** (that's Stage 7). Idempotent,
resumable, fault-isolated.

---

## 1. Where everything lives

| Thing | Path |
|---|---|
| Code (package `src`) | `~/repos/epstein-ingest` = `\\wsl$\Ubuntu\home\carlo\repos\epstein-ingest` = `\\wsl.localhost\ubuntu\...` (all the same WSL dir) |
| Invoke | `python -m src.stage3 <ROOT> [flags]` (needs `PYTHONPATH=<repo root>`) |
| Datasets (Windows-only drive) | `D:\Carlo\Projects\Data\epstein\DataSet-XX\VOL000XX\IMAGES\<0001..NNNN>\*.pdf` (+ `*.json` sidecars) |
| Azure creds | `~/repos/sv-kb/.env.local` (env vars; **API key is a secret — never print/commit**) |
| WSL venv (tests / non-D: data) | `~/repos/sv-kb/pipeline/shared/.venv/bin/python` (PyMuPDF + requests; no `openai` SDK) |
| Windows conda env (D: data) | `/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe` (PyMuPDF 1.27.1 + requests 2.32) |

> **`\\wsl$\Ubuntu\...` and `\\wsl.localhost\ubuntu\...` are two aliases for the same WSL path.** If you ever
> *physically* relocate the code, update every command's `PYTHONPATH`; a running loop won't notice but the next
> per-subfolder `python -m src.stage3` it spawns will fail to import `src`.

---

## 2. The execution model (read this before running anything)

**`D:` is NOT mounted in WSL** (`/mnt/d` absent; can't self-mount — sudo needs a password). It **is** visible
from the Windows/Git-Bash shell as `/d/` and to Windows Python as `D:\…`. So:

- **Datasets on `D:` → run with the Windows `doc-scanner` conda Python**, importing the package over the WSL
  UNC path. This is the normal production case (DataSet-11/12 are on `D:`).
- **Tests, or data living inside WSL → use the WSL venv Python** with `PYTHONPATH=.` from the repo.

**Git-Bash path rules (critical — these caused real failures this session):**
- Export `MSYS_NO_PATHCONV=1` and `MSYS2_ARG_CONV_EXCL='*'` so Git-Bash doesn't mangle `D:\…` args or the
  `//wsl.localhost/…` `PYTHONPATH`.
- **Build Windows path args with `cygpath -w`, not by hand.** A literal `"D:\\...\\$name"` string did **not**
  expand `$name` through the Git-Bash/MSYS layer (errored `not a directory: …IMAGES$name`). Always:
  `win=$(cygpath -w "$sub")`.
- **Shell variables get mangled when passed through `wsl.exe …`** — inside `wsl.exe -- bash -c '…'` use
  **inline absolute paths**, not `$VAR`.

**Loading Azure creds without leaking the key** (the `.env.local` has inline comments + connection strings, so
don't blindly `source` it). Extract just the four vars:

```bash
ENVFILE='//wsl.localhost/ubuntu/home/carlo/repos/sv-kb/.env.local'
get() { grep -E "^$1=" "$ENVFILE" | head -1 | sed -E "s/^$1=//; s/[[:space:]]*#.*$//; s/[[:space:]]+$//; s/\r$//"; }
export AZURE_OPENAI_ENDPOINT="$(get AZURE_OPENAI_ENDPOINT)"
export AZURE_OPENAI_API_KEY="$(get AZURE_OPENAI_API_KEY)"
export AZURE_OPENAI_CHAT_API_VERSION="$(get AZURE_OPENAI_CHAT_API_VERSION)"
export AZURE_OPENAI_CHAT_NANO_DEPLOYMENT="$(get AZURE_OPENAI_CHAT_NANO_DEPLOYMENT)"
```

---

## 3. Azure connection & content filter (current, working config)

- **Model deployment:** `AZURE_OPENAI_CHAT_NANO_DEPLOYMENT=gpt-4.1-nano`. **Use gpt-4.1-nano, not gpt-5-mini.**
  nano is non-reasoning (chat shape: `max_tokens`+`temperature=0`+`seed`; deterministic) and **~6× cheaper**.
  The client auto-detects the family, so a gpt-5/o-series deployment would also work (reasoning shape) — but
  don't; it's pricier and non-deterministic. (Earlier in the project the "nano" env slot pointed at gpt-5-mini;
  it was corrected.) Override per-run with `--deployment` if needed.
- **API version:** `2025-04-01-preview` (supports `json_schema` structured outputs).
- **Content filter** (Azure AI Foundry → Guardrails+Controls → the filter assigned to the deployment):
  the working setup is the four categories (Hate/Sexual/Violence/Self-harm) at **"Lowest blocking"**
  (= block only *high* severity; "Highest blocking" means block ALL severities — the slider reads backwards)
  **and both Prompt Shields (jailbreak + indirect) = Off**. Prompt Shields false-positive heavily on evidence
  OCR. **This is the most permissive setup possible without a Limited Access approval.**
- **What still blocks even then:** genuine **`sexual:high`** content. Recovering *adult* high-severity pages
  needs the Azure **Limited Access "modified content filter"** application; **minors-related content is hard-
  blocked regardless** and must stay quarantined (see §7). Stage 3 handles blocks gracefully — it does not
  need the filter relaxed to run.

---

## 4. Prerequisites — verify before running

Stage 3 needs each sidecar to have `status:"ok"` AND a `stage2` block (page_kind). Spot-check:

```bash
PYDS="/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe"
MSYS_NO_PATHCONV=1 "$PYDS" -c "import json,sys;d=json.load(open(sys.argv[1],encoding='utf-8'));print('ok',d.get('status'),'stage2',bool(d.get('stage2')),'stage3',bool(d.get('stage3')))" "$(cygpath -w /d/Carlo/Projects/Data/epstein/DataSet-11/VOL00011/IMAGES/0001/<some>.json)"
```

(Stage 1's manifest may sit at the dataset root, and the Stage 2 manifest may be absent even when the per-sidecar
`stage2` blocks exist — the **per-sidecar block is what matters**, not the manifest.)

---

## 5. Running it

### 5a. Dry-run first (free — no API calls, projects tokens/cost)
```bash
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest' \
  "$PYDS" -m src.stage3 'D:\Carlo\Projects\Data\epstein\DataSet-XX\VOL000XX\IMAGES' --dry-run
```

### 5b. Small dataset / single folder (realtime, one process)
```bash
# after the get() creds block above:
MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest' \
  "$PYDS" -m src.stage3 'D:\Carlo\Projects\Data\epstein\DataSet-12\VOL00012\IMAGES' \
  -j 16 --progress-every 200 \
  --content-filter-log 'D:\Carlo\Projects\Data\epstein\DataSet-12\VOL00012\IMAGES\content-filter-failures.json'
```
DataSet-12 reference: 152 PDFs, ~3 min, **$0.17**, 1,039 summarized, 45 content-filtered (all sexual:high).

### 5c. LARGE dataset (100k+ docs) → **per-subfolder loop** (the canonical pattern)
**Do NOT point Stage 3 at the whole `IMAGES` root for a huge dataset** — `find_pdfs` `os.walk`s the entire tree
(DS11 ≈ 660k entries) on the AV-scanned `D:` drive and **stalls for many minutes before any processing** (looks
alive — CPU climbing, ~70 MB — but 0 docs done). Instead drive it **one subfolder at a time** (each ~2k files →
instant start), which also gives observable progress and cheap resumes:

```bash
PYDS="/c/Users/carlo/anaconda3/envs/doc-scanner/python.exe"
ENVFILE='//wsl.localhost/ubuntu/home/carlo/repos/sv-kb/.env.local'
get() { grep -E "^$1=" "$ENVFILE" | head -1 | sed -E "s/^$1=//; s/[[:space:]]*#.*$//; s/[[:space:]]+$//; s/\r$//"; }
export AZURE_OPENAI_ENDPOINT="$(get AZURE_OPENAI_ENDPOINT)"
export AZURE_OPENAI_API_KEY="$(get AZURE_OPENAI_API_KEY)"
export AZURE_OPENAI_CHAT_API_VERSION="$(get AZURE_OPENAI_CHAT_API_VERSION)"
export AZURE_OPENAI_CHAT_NANO_DEPLOYMENT="$(get AZURE_OPENAI_CHAT_NANO_DEPLOYMENT)"
export PYTHONPATH='//wsl.localhost/ubuntu/home/carlo/repos/epstein-ingest'
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
D="/d/Carlo/Projects/Data/epstein/DataSet-11/VOL00011/IMAGES"
CFLOG=$(cygpath -w "$D/content-filter-failures.json")
START="0001"   # bump to resume from a later subfolder, e.g. "0032"
for sub in "$D"/*/; do
  name=$(basename "$sub"); [[ "$name" < "$START" ]] && continue
  win=$(cygpath -w "$sub")
  echo ">>> [$name] ($((10#$name))/332) $(date +%H:%M:%S)"
  "$PYDS" -m src.stage3 "$win" -j 32 --progress-every 500 --content-filter-log "$CFLOG" \
    || echo "!!! [$name] FAILED (continuing)"
done
echo "DS11 loop DONE | $(date)"
```
Run it with `run_in_background: true`. Notes: **no `--force`** (already-done docs skip); `--content-filter-log`
points at the dataset root so all subfolders merge into one failures log; each subfolder also writes its own
`stage3-run-manifest.json`.

### 5d. Batch mode (`--batch`) — **not usable at full-dataset scale yet**
Azure caps a batch at ~100k requests; a full dataset is ~800k+ calls → needs multi-job chunking that **is not
implemented**. Until it is, use realtime (§5c). Batch is fine for a <100k-request slice.

---

## 6. Concurrency, resume, monitoring

- **Concurrency:** `-j N` = a thread pool of N concurrent HTTP calls (these are **threads, not agents**). One
  nano call ≈ **~1.5 s**; throughput ≈ N/1.5 calls/s. Measured: `-j16` ≈ 3.9 docs/s; `-j32` ≈ 7.8 docs/s
  (clean 2× — currently quota-bound nowhere, **0 throttling**). You can push higher (`-j48`) while errors stay
  at 0; backoff handles any 429s.
- **Resume:** re-running skips any doc that already has a `stage3` block (and any page that already has a
  `summary`). For the loop, set `START` to the subfolder it died on. Restarts only re-walk the current subfolder
  (cheap) — this is the whole reason for the per-subfolder pattern.
- **Monitor:** read the background task's output file (streams `>>> [NNNN]` markers + `... n/N pdfs` progress +
  a per-subfolder summary). Or count done docs on disk:
  `grep -l '"stage3"' <subfolder>/*.json | wc -l`. Final per-subfolder line reports pages summarized / skipped /
  errored / safety-review / content-filtered / spend.

---

## 7. Content-filter handling & safety governance (important)

- A page Azure blocks (HTTP 400 `content_filter`, or a 200 with `finish_reason=content_filter`) is **not** an
  error — Stage 3 sets `text_safety={flag:"review", content_filtered:true}` + a `content_filtered` page flag,
  writes **no summary**, counts it under `content_filtered`, and appends it to **`content-filter-failures.json`**
  (file / page / timestamp / error; merges across runs).
- **These blocked pages are the CSAM / minors quarantine bucket.** On DataSet-12 the entire blocked set was
  `sexual:high` and ~84% tripped a minor-indicator screen. **Do not pursue an exemption to auto-summarize
  minor-related content** — it must stay quarantined and route to human review (and any mandatory reporting
  process). The Limited Access modified-filter only covers adult high-severity content.
- **`text_safety` is `ok | review` only** (no `block`): sv-kb policy is *label, don't block*. `review` means
  "escalate to the downstream Azure Content Safety `text:analyze` scan." Review rate is **corpus-dependent**
  (DS12 57%, DS11 ~9%) — don't assume it's a tiny fraction.
- To re-categorize the failures log (which categories/severities are blocking), re-probe the logged pages
  directly against the live filter (a 400 returns `error.innererror.content_filter_result` with per-category
  severity + a `jailbreak` flag).

---

## 8. Gotchas / failure modes seen this session (and the fix)

| Symptom | Cause | Fix |
|---|---|---|
| Run "alive" (CPU up, low mem) but **0 docs, 0 API calls** for many minutes | `find_pdfs` walking ~660k files on the AV `D:` drive | Use the **per-subfolder loop** (§5c), never the whole root for huge datasets |
| `error: not a directory: …IMAGES$name` | `"D:\\...\\$name"` didn't expand through MSYS | Build the path with **`cygpath -w "$sub"`** |
| `-m: command not found` inside `wsl.exe` | `$VAR` mangled through the wsl arg layer | Use **inline absolute paths** inside `wsl.exe -- bash -c '…'` |
| Loop **silently halts**, log frozen, no `python.exe` | Machine slept / went idle / background task reaped while unattended | It's resumable — relaunch with `START=<last subfolder>`. For long unattended runs, **keep the machine awake** (`powercfg /change standby-timeout-ac 0`) or add a watchdog |
| Two runs at once (double spend / racing the same sidecar) | Old loop still alive when a new one starts | Before relaunch, confirm **exactly one** `python.exe`: `tasklist //FI "IMAGENAME eq python.exe"`; kill strays: `taskkill //F //T //PID <pid>` |
| Every call returns 400 content_filter | Prompt Shields on, or sliders at "Highest blocking" | Fix the deployment's filter (§3) — Shields Off, categories "Lowest blocking" |
| Every call 400/empty on a non-nano deployment | gpt-5-mini got the chat request shape | The client auto-detects; ensure `--deployment`/env names the real model so it picks the right shape |

**Kill a runaway / stuck run:** find the worker (`tasklist //FI "IMAGENAME eq python.exe" //FO CSV`) and
`taskkill //F //T //PID <pid>`. Killing the worker does **not** stop a bash loop (it spawns the next subfolder's
worker) — stop the loop's background task too, or kill its bash. Always verify 0 `python.exe` before relaunching.

---

## 9. Cost & throughput (measured, gpt-4.1-nano)

- **Per page:** ~$0.000157 realtime (input-dominated; ~900 in / ~150 out tokens/page).
- **DataSet-12:** 1,084 eligible pages → **$0.17**, ~3 min @ -j16.
- **DataSet-11:** ~$0.26 / 1,000 docs (~1.64 eligible pages/doc) → **~$85** for the full ~331,655 docs;
  ~11–13 h @ -j32.
- Batch would be ~50% cheaper but isn't usable at scale yet (§5d).

---

## 10. Current state (as of 2026-06-17 ~21:00 EDT)

- **DataSet-12: DONE** (gpt-4.1-nano, `--force`): 1,039 summarized, 45 content-filtered (all sexual:high,
  ~36 minor-indicators → quarantine), $0.17. Failures: `…\DataSet-12\…\IMAGES\content-filter-failures.json`
  (+ `epstein-ingest/content-filter-failures.json` and `…pre-fix-canary.json` for history).
- **DataSet-11: IN PROGRESS** via the §5c loop at **`-j32`**, currently around subfolder **`0032/332` (~10%)**,
  clean (7 content-filtered total, 0 errors, ~9% review). Resume with `START=<next undone subfolder>`. Verify
  the live position from the background task output or by counting `"stage3"` sidecars per subfolder.
- **Open items:** (1) keep the machine awake or add a watchdog so the long DS11 run finishes unattended;
  (2) decide on a Limited Access application for the adult `sexual:high` bucket; (3) batch-mode request-chunking
  is unimplemented if you ever want the ~50% batch discount at scale.

---

## 11. Healthy-run checklist

- [ ] Stage 1 + Stage 2 present on the target (`status:"ok"` + `stage2` per sidecar).
- [ ] `--dry-run` shows a sane eligible-page count and cost.
- [ ] `deployment=gpt-4.1-nano`, `reasoning_shape=False` in the first lines of output.
- [ ] First subfolder starts within seconds (not a multi-minute stall).
- [ ] Per-subfolder summaries show `0 errored`; content-filtered numbers look plausible for the corpus.
- [ ] Exactly one `python.exe` worker.
- [ ] `content-filter-failures.json` is growing only with genuine `sexual:high` (no jailbreak floods → Shields are off).
