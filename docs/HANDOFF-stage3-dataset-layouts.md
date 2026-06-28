# Handoff (brief): Dataset directory layouts — DON'T hardcode the path (Stage 3+)

**Why this exists:** the 12 datasets do **not** share one folder shape. There are **three** layouts. Hardcoding
`DataSet-NN\VOL000NN\IMAGES` (or assuming `NNNN` subfolders) **will fail** on some datasets — it already cost
a wrong-path run this session. Resolve the IMAGES dir and detect flat-vs-nested per dataset; don't assume.

## The three shapes (verified 2026-06-21, all on `C:\Projects\Data\epstein\`)

| Datasets | Shape | IMAGES path | Inside IMAGES |
|---|---|---|---|
| **DS‑01 … DS‑08** | nested, **NO VOL layer** | `DataSet-NN\IMAGES` | `NNNN\` subfolders → `*.pdf` + `*.json` |
| **DS‑10, DS‑11, DS‑12** | nested, **with VOL** | `DataSet-NN\VOL000NN\IMAGES` | `NNNN\` subfolders → `*.pdf` + `*.json` |
| **DS‑09** | **FLAT, with VOL** | `DataSet-09\VOL00009\IMAGES` | `*.pdf` + `*.json` **directly** (no `NNNN`); ~529k files |

Two independent axes that bit this session:
1. **VOL layer present or not** — DS‑01–08 have **no** `VOL000NN`; DS‑09–12 do.
2. **Flat vs nested** — only **DS‑09 is flat**; everyone else has `NNNN` batch subfolders (~1,000 PDFs each).

## Resolve it, don't assume (PowerShell)

```powershell
function Get-ImagesDir($dsRoot) {                      # works for all three shapes
  if (Test-Path "$dsRoot\IMAGES") { return "$dsRoot\IMAGES" }            # DS-01..08
  $v = Get-ChildItem $dsRoot -Directory -Filter 'VOL*' -EA SilentlyContinue | Select-Object -First 1
  if ($v -and (Test-Path "$($v.FullName)\IMAGES")) { return "$($v.FullName)\IMAGES" }  # DS-09..12
  throw "no IMAGES under $dsRoot"
}
$img  = Get-ImagesDir "C:\Projects\Data\epstein\DataSet-03"
$nested = @(Get-ChildItem $img -Directory -EA SilentlyContinue).Count -gt 0   # $true except DS-09
```
- `$nested = $true` → **drive Stage 3 per `NNNN` subfolder** (loop `Get-ChildItem $img -Directory`), the
  canonical pattern (instant start, cheap resume).
- `$nested = $false` (DS‑09 only) → point Stage 3 at `$img` itself, **but first** mitigate the flat-walk
  (next section).

## DS‑09 flat-layout caveat (the one real gotcha)

After Stage 2 + escalate, DS‑09's flat IMAGES holds ~529k PDFs **plus ~529k per-PDF output dirs**
(`<name>/images/…`). The stock `find_pdfs` (`os.walk`) descends into all of them → a multi-minute startup
stall before any work (the Handoff §4.3 slow-walk). Mitigate one of:
- add a pruned walk to `src/stage3/walk.py` mirroring `src/stage2/escalate.py::_find_pdfs_pruned` (skips the
  `images/`/`pages/`/`artifacts/` output dirs) — **recommended**, or
- accept one slow startup walk and run once (resume re-walks each time).

DS‑01–08 and DS‑10–12 are nested → driven per-subfolder → **no slow-walk** (each `NNNN` walk is ~1k files).

## Other per-dataset facts the Stage‑3 session needs

- **Stage‑3 readiness (Stage 2 done):** DS‑01–08 ✅ (2026-06-21), DS‑09 ✅, DS‑10 ✅, DS‑11 ✅, DS‑12 ✅
  (DS‑12 already Stage‑3-complete). So all 12 are Stage‑3-ready; DS‑11 is *partial* (resume), DS‑12 *done*.
- **`content-filter-log`:** put one per dataset at its IMAGES root, e.g. `Join-Path $img 'content-filter-failures.json'`.
- **Run-root for the per-subfolder loop** is the resolved `$img`; the manifest lands at `$img\stage3-run-manifest.json`
  (nested datasets also get one per `NNNN` if you loop subfolders).
- DS‑01–08 are small (**~14.7k PDFs total**) and **image-heavy** (DS‑01–04 are image-dominant) — fast for
  Stage 3 (few eligible text pages) but a larger Stage‑4 share later.

**See also:** [`HANDOFF-stage3-run-ds09-12.md`](HANDOFF-stage3-run-ds09-12.md) (full Stage‑3 run handoff:
machine/creds/commands/state) — this doc is just the layout cheat-sheet to bolt onto it.
