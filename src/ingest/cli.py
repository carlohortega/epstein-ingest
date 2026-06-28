"""Stage-8 (ingest) CLI — three modes (Handoff §0a/§7):

    # project the write plan (no creds, no files written except an optional --out JSON)
    python -m src.ingest ROOT [ROOT ...] --dry-run [--store DIR] [-j N] [--out FILE]

    # S8 prepare: emit the reviewable apply-package (no creds)
    python -m src.ingest ROOT [ROOT ...] --prepare [--out <corpus>/_apply] [--store DIR] [-j N]

    # S9 apply: consume the package, write the live system (the ONLY live-writing mode; needs creds)
    python -m src.ingest --apply <corpus>/_apply --tenant <UUID> [--ensure-tenancy] [-j N] [--force]

``--dry-run`` / ``--prepare`` are offline and deterministic. ``--apply`` resolves tenancy, uploads blobs via
azcopy, bulk-loads the COPY vectors, and calls ``index_document`` verbatim (see load.py / apply.py).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys

from . import INGEST_TOOL_NAME, INGEST_TOOL_VERSION
from .config import IngestConfig


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-ingest",
        description=f"{INGEST_TOOL_NAME} {INGEST_TOOL_VERSION} — load a staged corpus into the live sv-kb "
                    f"system (Postgres + Azure Storage). The ONLY stage that writes the live system.")
    p.add_argument("roots", nargs="*", help="dataset root(s) (e.g. .../DataSet-09); IMAGES is located per "
                                            "dataset. Not needed for --apply (the package already lists docs).")
    p.add_argument("--prepare", action="store_true", help="S8: emit the apply-package to --out (no creds)")
    p.add_argument("--apply", default=None, metavar="PKG_DIR",
                   help="S9: consume the apply-package at PKG_DIR and write the live system (needs --tenant)")
    p.add_argument("--tenant", default=None, help="tenant_id UUID (required for --apply; sets RLS)")
    p.add_argument("--source-kind", default="pdf", help="pdf | image | audio | video (default: pdf)")
    p.add_argument("--store", default=None, help="embed store dir override (default: per-dataset auto-detect)")
    p.add_argument("--dimensions", type=int, default=1536, help="vector dims (1536 small / 3072 large)")
    p.add_argument("--model", default="text-embedding-3-small", help="embedding model (must match the store)")
    p.add_argument("--ensure-tenancy", action="store_true",
                   help="(§3a option b) upsert cases/datasets on first sight; default requires them pre-created")
    p.add_argument("--threshold", type=int, default=4, help="Content-Safety severity that redacts (§4a)")
    p.add_argument("-j", "--workers", type=int, default=1, help="documents in flight")
    p.add_argument("--dry-run", action="store_true", help="project the write plan; NO DB/Storage writes")
    p.add_argument("--out", default=None,
                   help="--dry-run: write the plan JSON here. --prepare: apply-package dir (default <root>/_apply)")
    p.add_argument("--force", action="store_true", help="re-ingest completed docs (live only)")
    p.add_argument("--version", action="version", version=f"{INGEST_TOOL_NAME} {INGEST_TOOL_VERSION}")
    return p


def config_from_args(args) -> IngestConfig:
    return IngestConfig(source_kind=args.source_kind, embedding_model=args.model, dimensions=args.dimensions,
                        ensure_tenancy=args.ensure_tenancy, content_safety_threshold=args.threshold)


def _print_dry_run(m: dict) -> None:
    c = m["corpus"]; t = c["totals"]
    print(f"{INGEST_TOOL_NAME} dry-run: {c['documents_to_ingest']} docs to ingest across {len(m['datasets'])} "
          f"dataset(s) in {m['wall_clock_seconds']}s")
    for name, d in m["datasets"].items():
        dt = d["totals"]
        print(f"  {name}: {d['documents_to_ingest']}/{d['pdfs_seen']} docs  store={'ok' if d['store_present'] else 'MISSING'}  "
              f"chunks={dt['parents']}p/{dt['children']}c  vec(present/missing)={dt['vectors_present']}/{dt['vectors_missing']}  "
              f"img(embed/withhold)={dt['images_embed']}/{dt['images_withheld']}  blank_pages={dt['blank_pages']}  "
              f"csam_skip={d['csam_documents_NOT_ingested']}")
    print(f"  CORPUS: {t['parents']} parents / {t['children']} children; text vectors present "
          f"{t['vectors_present']} / MISSING {t['vectors_missing']}; image embed {t['images_embed']} / "
          f"WITHHELD {t['images_withheld']} {c['withheld_tiers'] or ''}; source_withheld docs "
          f"{c['source_withheld_docs']}; blank pages {t['blank_pages']}; tenancy pairs {len(c['tenancy_pairs'])}")
    if c["csam_documents_NOT_ingested"]:
        print(f"  ** CSAM-SUSPECTED: {c['csam_documents_NOT_ingested']} document(s) ({c['csam_assets']} assets) "
              f"will NOT be ingested — preserve + report in the staging system of record (do NOT delete). **")
    if t["vectors_missing"]:
        print(f"  ** WARNING: {t['vectors_missing']} child content_sha NOT in the embed store — the live guard "
              f"provider WOULD raise. Re-embed (Stage 7) before a live run. **")
    if c["errors"]:
        print(f"  errors: {len(c['errors'])} (see plan; first: {c['errors'][0]})")


def _print_prepare(m: dict) -> None:
    c = m["corpus"]; t = c["totals"]
    print(f"{INGEST_TOOL_NAME} prepare: {c['documents_to_ingest']} docs across {len(m['datasets'])} dataset(s) "
          f"in {m['wall_clock_seconds']}s  (tenancy={m['tenancy_policy']})")
    print(f"  text vectors written {m['text_vectors_written']} (MISSING {t['vectors_missing']}); image vectors "
          f"written {m['image_vectors_written']}; blobs {t['blobs']}; image withheld {t['images_withheld']} "
          f"{c['withheld_tiers'] or ''}; source_withheld docs {c['source_withheld_docs']}; blank pages "
          f"{t['blank_pages']}; tenancy pairs {len(c['tenancy_pairs'])}")
    if c["csam_documents_NOT_ingested"]:
        print(f"  ** CSAM-SUSPECTED: {c['csam_documents_NOT_ingested']} document(s) NOT ingested — preserve + "
              f"report in the staging system of record (do NOT delete). **")
    if t["vectors_missing"]:
        print(f"  ** WARNING: {t['vectors_missing']} child content_sha NOT in the embed store — apply's guard "
              f"provider WOULD raise. Re-embed (Stage 7) before --apply. **")
    if c["errors"]:
        print(f"  errors: {len(c['errors'])} (see prepare-manifest; first: {c['errors'][0]})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = config_from_args(args)

    # --- S9 apply: consume the package (the only live-writing mode) ---
    if args.apply:
        if not args.tenant:
            print("error: --apply requires --tenant <UUID>", file=sys.stderr)
            return 2
        if not os.path.isdir(args.apply):
            print(f"error: apply-package dir not found: {args.apply}", file=sys.stderr)
            return 2
        from .apply import apply_package
        try:
            m = apply_package(args.apply, cfg, tenant_id=args.tenant, workers=args.workers, force=args.force)
        except (NotImplementedError, RuntimeError) as exc:   # missing creds / preflight fail-loud / etc.
            print(f"error: {exc}", file=sys.stderr)
            return 3
        d = m["documents"]
        print(f"{INGEST_TOOL_NAME} apply: {d['ingested']} ingested / {d['skipped_complete']} skipped / "
              f"{d['failed']} failed of {d['total']}; preload {m['preload']}; blobs {m['blob_objects']}")
        if d["failed"]:
            print(f"  ** {d['failed']} document(s) failed — see apply-manifest.json (first: {m['errors'][0]}) **")
        return 0 if not d["failed"] else 1

    # --- offline modes need roots ---
    if not args.roots:
        print("error: provide dataset root(s) for --dry-run/--prepare (or --apply <PKG_DIR>)", file=sys.stderr)
        return 2
    bad = [r for r in args.roots if not os.path.isdir(r)]
    if bad:
        print(f"error: not a directory: {bad}", file=sys.stderr)
        return 2

    if args.prepare:
        out = args.out or os.path.join(os.path.abspath(args.roots[0]), "_apply")
        from .emit import prepare
        m = prepare(args.roots, cfg, out, store_override=args.store, workers=args.workers)
        _print_prepare(m)
        print(f"  apply-package: {out}")
        return 0

    if args.dry_run:
        from .walk import dry_run
        m = dry_run(args.roots, cfg, store_override=args.store, workers=args.workers, write_plan_to=args.out)
        _print_dry_run(m)
        return 0

    print("error: choose a mode — --dry-run | --prepare | --apply <PKG_DIR>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
