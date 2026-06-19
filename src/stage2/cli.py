"""Stage-2 command-line interface.

    python -m src.stage2 ROOT [-j N] [--force] [--generated-at ISO8601] [--no-pages]
                                              [threshold overrides]

Every Stage2Config field is exposed as a ``--flag`` (dataclass field name with underscores -> dashes)
and echoed into each sidecar's ``stage2.config`` so a run is reproducible from its own output.
Booleans get a paired ``--flag`` / ``--no-flag`` form.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import STAGE2_TOOL_NAME, STAGE2_TOOL_VERSION
from .config import Stage2Config
from .walk import run


def _default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-stage2",
        description=f"{STAGE2_TOOL_NAME} {STAGE2_TOOL_VERSION} — render pages, classify page_kind, "
                    f"register image assets (local, offline; extends the Stage-1 sidecar).",
    )
    p.add_argument("root", help="root folder to walk recursively for *.pdf (with Stage-1 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=_default_workers(),
                   help="parallel worker processes (default: CPU-2)")
    p.add_argument("--force", action="store_true",
                   help="re-render/re-classify even if a sidecar already has a stage2 block")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now). "
                        "Pass a fixed value for byte-reproducible runs.")
    p.add_argument("--progress-every", type=int, default=2000,
                   help="print a progress line every N completed PDFs (0 = silent until done); "
                        "useful for tailing the log on large corpora")
    p.add_argument("--escalate-vision", action="store_true",
                   help="POST-PASS (not a normal run): render low_quality_text pages to images/ and register "
                        "them in image_assets[] — forward-fills the over-inclusive vision set after a "
                        "--no-stage-pages run. Idempotent; needs an existing stage2 block per PDF.")
    p.add_argument("--purge-staged-pages", action="store_true",
                   help="POST-PASS: delete the deferred <name>/pages/ display renders (regenerated at Stage 7 "
                        "on ingest); keeps images/ artifacts/ thumbnail.png. DRY-RUN unless --confirm.")
    p.add_argument("--confirm", action="store_true",
                   help="with --purge-staged-pages: actually delete (default prints a dry-run report only).")
    p.add_argument("--version", action="version", version=f"{STAGE2_TOOL_NAME} {STAGE2_TOOL_VERSION}")

    g = p.add_argument_group("thresholds (Handoff §4/§5/§6) — all echoed into stage2.config")
    for f in dataclasses.fields(Stage2Config):
        flag = "--" + f.name.replace("_", "-")
        if f.type == "bool" or isinstance(f.default, bool):
            # paired form: --stage-pages / --no-stage-pages (default from the dataclass)
            g.add_argument(flag, dest=f.name, action="store_true", default=None,
                           help=f"(default: {f.default})")
            g.add_argument("--no-" + f.name.replace("_", "-"), dest=f.name, action="store_false",
                           help=argparse.SUPPRESS)
        else:
            g.add_argument(flag, dest=f.name, type=type(f.default), default=None,
                           help=f"(default: {f.default})")
    return p


def config_from_args(args: argparse.Namespace) -> Stage2Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Stage2Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Stage2Config(), **overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    s2cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.escalate_vision:
        from .escalate import run_escalate
        m = run_escalate(args.root, s2cfg, generated_at, workers=args.workers,
                         progress_every=args.progress_every or 5000)
        print(f"escalate-vision: {m['pdfs_escalated']} pdfs escalated, {m['pages_rendered']} low_quality_text "
              f"pages rendered to images/ ({m['pdfs_seen']} seen, {m['errors']} errors) "
              f"in {m['wall_clock_seconds']}s")
        return 0

    if args.purge_staged_pages:
        from .escalate import run_purge
        m = run_purge(args.root, workers=args.workers, delete=args.confirm)
        tag = "DELETED" if args.confirm else "DRY-RUN (nothing deleted; pass --confirm to delete)"
        print(f"purge-staged-pages [{tag}]: {m['pdfs_with_pages_dir']} pdfs have a pages/ dir, "
              f"{m['page_png_files']} PNGs = {m['gb']} GB reclaimable; "
              f"dirs_deleted={m['dirs_deleted']}, render_paths_nulled={m['render_paths_nulled']} "
              f"in {m['wall_clock_seconds']}s")
        return 0

    m = run(args.root, s2cfg, generated_at, force=args.force, workers=args.workers,
            progress_every=args.progress_every)

    print(f"{STAGE2_TOOL_NAME}: {m['pdfs_rendered']} rendered, {m['pdfs_skipped_already_done']} skipped, "
          f"{m['pdfs_errored']} errored, {m['pdfs_no_sidecar']} without a Stage-1 sidecar "
          f"({m['pdfs_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  pages: {m['pages_rendered']} rendered — {m['image_pages']} image, {m['text_pages']} text, "
          f"{m['mixed_pages']} mixed, {m['blank_pages']} blank, {m['redacted_pages']} redacted "
          f"({m['text_quality_suspect']} text flagged low_quality_text)")
    print(f"  artifacts: {m['artifacts_kept']} kept, {m['artifacts_skipped']} skipped")
    print(f"  vision workload (Stage 4): {m['vision_workload']}  "
          f"(image pages + kept artifacts; manifest: {os.path.basename('stage2-run-manifest.json')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
