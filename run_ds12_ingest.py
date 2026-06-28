#!/usr/bin/env python3
"""DS-12 Stage-8 ingest launcher — prove the live load path on the canary dataset (task #1).

Runs **S8 prepare** (offline, no creds) then **S9 apply** (the live write) on DataSet-12, with a strict
preflight so it is safe to fire the moment the environment is ready and stops with a clear, specific message
on whatever is missing. The prepare step needs only DS-12 on disk; the apply step needs DB + Storage creds.

    # offline: emit + review the apply-package (no creds needed)
    python run_ds12_ingest.py --ds12 <…/DataSet-12> --prepare-only

    # full: prepare then load the live system
    python run_ds12_ingest.py --ds12 <…/DataSet-12> --tenant <UUID> [--ensure-tenancy]

Env the apply step reads: DATABASE_URL, AZURE_STORAGE_ACCOUNT (+ AZURE_STORAGE_SAS or `azcopy login`), and
sv-kb's shared package on PYTHONPATH (SVKB_SHARED, else ~/repos/sv-kb/pipeline/shared is tried).
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

_DS12_GUESSES = (
    os.environ.get("EPSTEIN_DS12"),
    "/d/Carlo/Projects/Data/epstein/DataSet-12",
    r"D:\Carlo\Projects\Data\epstein\DataSet-12",
    os.path.expanduser("~/repos/epstein-data/DataSet-12"),
)


def _find_ds12(arg: str | None) -> str | None:
    for c in (arg, *_DS12_GUESSES):
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def _ensure_svkb() -> bool:
    try:
        import svkb_pipeline  # noqa: F401
        return True
    except ModuleNotFoundError:
        for cand in (os.environ.get("SVKB_SHARED"),
                     os.path.expanduser("~/repos/sv-kb/pipeline/shared"),
                     r"\\wsl.localhost\Ubuntu-22.04\home\cortega\repos\sv-kb\pipeline\shared"):
            if cand and os.path.isdir(cand) and cand not in sys.path:
                sys.path.insert(0, cand)
    try:
        import svkb_pipeline  # noqa: F401
        return True
    except ModuleNotFoundError:
        return False


def _has_embed_store(ds12: str) -> bool:
    from src.ingest.discover import default_store_dir
    from src.ingest.store_reader import VectorStore
    return VectorStore(default_store_dir(ds12), 1536).exists()


def _print_prepare(pm: dict) -> None:
    c = pm["corpus"]; t = c["totals"]
    print(f"  docs to ingest: {c['documents_to_ingest']}  |  text vectors {pm['text_vectors_written']} "
          f"(MISSING {t['vectors_missing']})  |  image vectors {pm['image_vectors_written']}")
    print(f"  CSAM held out: {c['csam_documents_NOT_ingested']}  |  images withheld {t['images_withheld']} "
          f"{c['withheld_tiers'] or ''}  |  source_withheld docs {c['source_withheld_docs']}  |  "
          f"blank pages {t['blank_pages']}")
    if t["vectors_missing"]:
        print(f"  ** WARNING: {t['vectors_missing']} child content_sha NOT in the embed store — the apply "
              f"guard provider WOULD raise. Re-embed (Stage 7) before the live apply. **")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="run_ds12_ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ds12", default=None, help="path to the DataSet-12 dir (else EPSTEIN_DS12 / common locations)")
    p.add_argument("--out", default=None, help="apply-package dir (default <DataSet-12>/_apply)")
    p.add_argument("--tenant", default=None, help="tenant_id UUID (required for the live apply)")
    p.add_argument("--ensure-tenancy", action="store_true", help="upsert cases/datasets (else require pre-created)")
    p.add_argument("--prepare-only", action="store_true", help="emit the package and stop (no creds, no writes)")
    p.add_argument("--force", action="store_true", help="re-ingest completed docs")
    p.add_argument("-j", "--workers", type=int, default=4)
    args = p.parse_args(argv)

    print("== DS-12 Stage-8 ingest — preflight ==")
    ds12 = _find_ds12(args.ds12)
    if not ds12:
        print("  [MISSING] DataSet-12 not found — pass --ds12 <path> or set EPSTEIN_DS12")
        return 2
    print(f"  [ok]   DS-12: {ds12}")
    if _has_embed_store(ds12):
        print("  [ok]   embed store present (Stage 7)")
    else:
        print("  [WARN] no embed store found — prepare will report vectors_missing; re-run Stage 7 if so")

    from src.ingest.config import IngestConfig
    from src.ingest.emit import prepare
    cfg = IngestConfig(ensure_tenancy=args.ensure_tenancy)
    out = args.out or os.path.join(ds12, "_apply")

    print(f"\n== S8 prepare → {out} ==")
    pm = prepare([ds12], cfg, out, workers=args.workers)
    _print_prepare(pm)

    if args.prepare_only:
        print(f"\nprepare-only: apply-package ready at {out}. Review it, then re-run with --tenant + creds.")
        return 0

    # --- apply preflight (creds) ---
    print("\n== S9 apply — preflight ==")
    missing = []
    for v in ("DATABASE_URL", "AZURE_STORAGE_ACCOUNT"):
        ok = bool(os.environ.get(v))
        print(f"  [{'ok' if ok else 'MISSING'}] {v}")
        if not ok:
            missing.append(v)
    if not args.tenant:
        print("  [MISSING] --tenant <UUID>"); missing.append("--tenant")
    else:
        print(f"  [ok]   tenant {args.tenant}")
    svkb = _ensure_svkb()
    print(f"  [{'ok' if svkb else 'MISSING'}] svkb_pipeline importable")
    if not svkb:
        missing.append("svkb_pipeline (set SVKB_SHARED)")
    azc = shutil.which("azcopy")
    print(f"  [{'ok' if azc else 'MISSING'}] azcopy on PATH")
    if not azc:
        missing.append("azcopy")
    if missing:
        print(f"\n** apply SKIPPED — missing: {missing}.\n   The reviewable package is ready at {out}; "
              f"re-run with those set to load the live system. **")
        return 0

    print("\n== S9 apply → live system ==")
    from src.ingest.apply import apply_package
    am = apply_package(out, cfg, tenant_id=args.tenant, workers=args.workers, force=args.force)
    d = am["documents"]
    print(f"  ingested {d['ingested']} / skipped {d['skipped_complete']} / failed {d['failed']} "
          f"of {d['total']}  |  preload {am['preload']}  |  blob objects {am['blob_objects']}")
    if d["failed"]:
        print(f"  ** {d['failed']} failed — see {out}/apply-manifest.json (first: {am['errors'][0]}) **")
        return 1
    print(f"\nDS-12 ingested. Manifest: {out}/apply-manifest.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
