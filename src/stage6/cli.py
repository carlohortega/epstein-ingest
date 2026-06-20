"""Stage-6 command-line interface.

    python -m src.stage6 ROOT [-j N] [--force] [--dry-run] [--generated-at ISO8601]

Stage 6 is chunk-only: it makes NO API calls, so there are NO Azure-connection flags and no api key. Every
Stage6Config field is exposed as a ``--flag`` and echoed into each sidecar's ``stage6.config`` so a run is
reproducible from its own output. The sv-kb chunker import is resolved by the launcher (run_stage6.py) or,
for ``python -m src.stage6`` / tests, by ``chunk._import_svkb`` (~/repos/sv-kb/pipeline/shared or
``$SVKB_SHARED``).
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import STAGE6_TOOL_NAME, STAGE6_TOOL_VERSION
from .config import Stage6Config


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-stage6",
        description=f"{STAGE6_TOOL_NAME} {STAGE6_TOOL_VERSION} — chunk the document body for retrieval by "
                    f"reusing sv-kb's chunk_pages verbatim; writes a sibling <name>.chunks.json + a stage6 "
                    f"block (append-only). Chunk-only: no embedding, no API calls.")
    p.add_argument("root", help="root folder to walk recursively for *.pdf (with Stage-1.. sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="concurrent per-document tasks (default: config.concurrency)")
    p.add_argument("--force", action="store_true",
                   help="re-chunk even docs that already have a stage6 block")
    p.add_argument("--dry-run", action="store_true",
                   help="run the chunker for exact page/parent/child counts but write nothing (no API "
                        "calls were ever made — chunking is local)")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now)")
    p.add_argument("--progress-every", type=int, default=5000,
                   help="print a progress line every N completed PDFs (0 = silent until done)")
    p.add_argument("--version", action="version", version=f"{STAGE6_TOOL_NAME} {STAGE6_TOOL_VERSION}")

    g = p.add_argument_group("knobs (Handoff §7) — all echoed into stage6.config")
    for f in dataclasses.fields(Stage6Config):
        flag = "--" + f.name.replace("_", "-")
        if f.type == "bool" or isinstance(f.default, bool):
            g.add_argument(flag, dest=f.name, action="store_true", default=None,
                           help=f"(default: {f.default})")
            g.add_argument("--no-" + f.name.replace("_", "-"), dest=f.name, action="store_false",
                           help=argparse.SUPPRESS)
        else:
            g.add_argument(flag, dest=f.name, type=type(f.default), default=None,
                           help=f"(default: {f.default})")
    return p


def config_from_args(args: argparse.Namespace) -> Stage6Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Stage6Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Stage6Config(), **overrides)


def _print_manifest(m: dict) -> None:
    c = m["counts"]
    print(f"{STAGE6_TOOL_NAME}: {m['pdfs_chunked']} chunked ({m['pdfs_empty']} empty), "
          f"{m['pdfs_skipped_already_done']} already-done, {m['pdfs_errored']} errored, "
          f"{m['pdfs_no_sidecar'] + m['pdfs_no_stage1']} missing upstream "
          f"({m['pdfs_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  output: {c['parents']} parents / {c['children']} children over {c['pages_chunked']} pages "
          f"({c['skipped_pages']} text-less pages skipped)  (manifest: stage6-run-manifest.json)")
    if m.get("errors"):
        print(f"  errors: {len(m['errors'])} (see manifest 'errors')")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.dry_run:
        from .walk import dry_run
        d = dry_run(args.root, cfg, force=args.force)
        print(f"{STAGE6_TOOL_NAME} dry-run: {d['eligible_documents']} eligible docs "
              f"({d['empty_documents']} would be empty, {d['identifier_errors']} identifier-errors)")
        print(f"  would produce {d['parents']} parents / {d['children']} children over "
              f"{d['pages_chunked']} pages ({d['skipped_pages']} text-less pages skipped); no API calls")
        return 0

    from .walk import run
    m = run(args.root, cfg, generated_at, force=args.force, workers=args.workers,
            progress_every=args.progress_every)
    _print_manifest(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
