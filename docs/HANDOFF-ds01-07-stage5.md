# Handoff: Stage 5 (finalize doc summary) for DataSet-01 → DataSet-07

## 0. Goal & the two gates

Run Stage 5 on DS-01–07 so every doc gets its **final** `document_summary_final` (text+vision fusion) →
completes the built pipeline (Stages 1–5) for those datasets. Stage 5 is **gpt-4.1-nano, cheap, append-only**:
it writes `document_summary_final` + a `stage5` block and **never** overwrites Stage 3's provisional
`document_summary`. Design spec: [`HANDOFF-stage5-finalize-summary.md`](HANDOFF-stage5-finalize-summary.md).

**Gate 1 — Stage 4 MUST be fully done on each dataset first.** Stage 5's per-doc gate skips any doc that
has `image_assets[]` but no `stage4` block (`no_stage4`). If you run Stage 5 before Stage 4 finishes, every
vision doc is silently **skipped** and left unfinalized. DS-01–07 Stage 3 is done; **Stage 4 is running now**
(scheduled task `epstein-stage4-ds01-07`). Do **not** start Stage 5 on a dataset until its Stage 4 is
complete (every `NNNN` folder has a `stage4-run-manifest.json`) **and** the Stage-4 task is idle.

**Gate 2 — never run Stage 5 concurrently with Stage 4 on the same dataset.** Both write the sidecars →
corruption risk. (Stage 5 *is* safe alongside a nano Stage-3 job on a different dataset, and alongside
Stage 4 on a *different* dataset, since Stage 5 uses the nano deployment, not gpt-5-mini.)

**Layout trap:** DS-01–08 are `DataSet-NN\IMAGES\NNNN` — **no `VOL000NN` layer** (unlike DS-09–12). Always
resolve the IMAGES dir; never hardcode:

```powershell
function Get-ImagesDir($dsRoot){
  $d=Join-Path $dsRoot 'IMAGES'; if(Test-Path -LiteralPath $d){return $d}              # DS-01..08
  $v=Get-ChildItem $dsRoot -Directory -Filter 'VOL*' -EA SilentlyContinue|Select-Object -First 1
  if($v){$vi=Join-Path $v.FullName 'IMAGES'; if(Test-Path $vi){return $vi}}            # DS-09..12
  return $null
}
```

---

## 1. REQUIRED pre-check: confirm Stage 4 is complete + idle (per dataset)

```powershell
# Stage-4 task must be idle (not Running) and its lock gone:
schtasks /query /tn epstein-stage4-ds01-07 /fo LIST | Select-String 'Status'
Test-Path 'C:\epstein-ingest\ds01_07_stage4.lock'      # should be False (no active instance)

# Every NNNN folder of each dataset must have a stage4-run-manifest.json:
foreach($n in 1..7){
  $img=Get-ImagesDir ("C:\Projects\Data\epstein\DataSet-{0:D2}" -f $n)
  $subs=Get-ChildItem $img -Directory|?{$_.Name -match '^\d{4}$'}
  $miss=$subs|?{ -not (Test-Path (Join-Path $_.FullName 'stage4-run-manifest.json')) }
  "DS-{0:D2}: folders={1} stage4_done={2} missing={3}" -f $n,$subs.Count,($subs.Count-$miss.Count),$miss.Count
}
```
Only run Stage 5 on datasets where `missing=0`. (Stage 5 can be run per dataset as each one's Stage 4
finishes — no need to wait for all seven.)

---

## 2. Machine / invocation

- **Python:** `C:\Users\carlo\anaconda3\python.exe`
- **Launcher (exists):** `C:\epstein-ingest\run_stage5.py <ROOT> [flags]` — loads `AZURE_*` from
  `~/.env.local`, puts the WSL repo on `sys.path`, calls `src.stage5.cli`. Use it (keys off the command line).
- **Stage 5 is realtime + threads.** No `--batch` (not implemented; the doc reduce is one cheap call).
  `--dry-run` needs no creds.
- **CLI:** `<ROOT> [-j N] [--force] [--dry-run] [--content-filter-log PATH] [--progress-every N]`.

## 3. Azure stack

Reuses the **nano** slot: `AZURE_OPENAI_CHAT_NANO_DEPLOYMENT` = `gpt-4.1-nano` on `fd-spyvault-ai` (live in
`~/.env.local`). **Different deployment from gpt-5-mini vision**, so Stage 5 does not contend with any
Stage-4 vision run on another dataset.

## 4. Run — per dataset (these are tiny, so whole-IMAGES-dir is simplest)

DS-01–07 total ~4,100 docs, mostly **promote** (no API call). Run the whole IMAGES dir per dataset; resume
is per-doc (skips any doc that already has a `stage5` block).

```powershell
$py='C:\Users\carlo\anaconda3\python.exe'; $L='C:\epstein-ingest\run_stage5.py'
foreach($n in 1..7){
  $img=Get-ImagesDir ("C:\Projects\Data\epstein\DataSet-{0:D2}" -f $n)
  # dry-run first (free) to see the route breakdown + nano $:
  & $py $L $img --dry-run
  # then finalize (DISTINCT cflog name — Stage 3 already wrote content-filter-failures.json here):
  $cflog=Join-Path $img 'stage5-content-filter-failures.json'
  & $py $L $img -j 8 --content-filter-log $cflog
}
```

- **Resumable:** the per-doc gate skips docs with a `stage5` block; re-running is a cheap no-op on done docs.
- **Robustness:** Stage 5 on these tiny datasets finishes in **minutes**, so foreground is fine. If you ever
  run it on a large dataset (DS-09/10) and it would exceed ~13 min unattended, use the scheduled-task
  pattern (template: `C:\epstein-ingest\run_ds10_stage4.ps1`) — harness background commands get reaped when
  the session goes idle.

## 5. Scope / cost

nano, **pennies total**. Reference: DS-12 Stage 5 = **$0.0027** (152 docs); DS-11 = **$0.17** (331,655 docs).
Most DS-01–07 docs are text-only → **promote, zero calls**; only `fused`/`image_only` docs make one cheap
reduce. The `--dry-run` gives the exact promote/fuse/image-only/no-content split + projected $ before you run.

## 6. Safety / governance

- A content-filter block on the fusion **reduce** is rare (it summarizes summaries+captions, not raw
  sensitive content), but if it happens the doc is quarantined (sentinel summary + `safety.flag=review` +
  `content_filtered`) and logged to the dataset's `stage5-content-filter-failures.json` → human review /
  mandatory reporting; do not auto-recover.
- Empty docs (no text, no kept vision) get the deterministic `"No readable content."` sentinel +
  `no_content:true` — not a fabricated summary.

## 7. Verify done (per dataset)

The run manifest (`stage5-run-manifest.json` at the IMAGES root) is the report:
- **`pdfs_no_stage4` MUST be 0** — any >0 means a vision doc was skipped because Stage 4 wasn't complete
  (re-finish Stage 4, then re-run Stage 5).
- `pdfs_errored = 0`; `routes:` promoted/fused/image_only/no_content sum to the finalized docs.
- Spot-check: a `fused` doc's sidecar has a `document_summary_final` with `source:"fused"` and a non-empty
  `text`, while the provisional `document_summary` is unchanged.

When every dataset shows `no_stage4=0` and `errored=0`, **DS-01–07 are through the full built pipeline
(Stages 1–5).** (Stages 6–7 are not built.)

---

**Bottom line for the executor:** wait for each dataset's Stage 4 to finish + go idle, then
`run_stage5.py <IMAGES> -j 8 --content-filter-log <IMAGES>\stage5-content-filter-failures.json` (dry-run
first). It's nano-cheap, append-only, resumable; the one trap is starting before Stage 4 is done, which the
manifest's `pdfs_no_stage4` count will catch.
