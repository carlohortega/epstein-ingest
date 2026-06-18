"""Command-line interface.

    python -m src.stage1 ROOT [-j N] [--force] [--generated-at ISO8601] [threshold overrides]

Every threshold in Config is exposed as a ``--flag`` (dataclass field name with underscores -> dashes)
and is echoed into each sidecar's ``generator.config`` so a run is reproducible from its own output.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import TOOL_NAME, TOOL_VERSION
from .config import Config
from .walk import run


def _default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract",
        description=f"{TOOL_NAME} {TOOL_VERSION} — offline text-first PDF pre-extraction (sidecar JSON).",
    )
    p.add_argument("root", help="root folder to walk recursively for *.pdf")
    p.add_argument("-j", "--workers", type=int, default=_default_workers(),
                   help="parallel worker processes (default: CPU-2)")
    p.add_argument("--force", action="store_true",
                   help="re-extract even if an up-to-date sidecar exists")
    p.add_argument("--skip-existing", action="store_true",
                   help="fast incremental/resume: skip any PDF that already has an OK sidecar (no re-hash); "
                        "(re)process missing or errored ones. Use for large, growing datasets.")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now). "
                        "Pass a fixed value for byte-reproducible runs.")
    p.add_argument("--version", action="version", version=f"{TOOL_NAME} {TOOL_VERSION}")

    g = p.add_argument_group("thresholds (Spec §6/§7/§8/§9) — all echoed into generator.config")
    for f in dataclasses.fields(Config):
        g.add_argument("--" + f.name.replace("_", "-"), dest=f.name,
                       type=type(f.default), default=None, help=f"(default: {f.default})")
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Config(), **overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()
    manifest = run(args.root, cfg, generated_at, force=args.force, workers=args.workers,
                   skip_existing=args.skip_existing)

    print(f"{TOOL_NAME}: {manifest['files_extracted']} extracted, "
          f"{manifest['files_skipped']} skipped, {manifest['files_errored']} errored "
          f"({manifest['files_seen']} seen) in {manifest['wall_clock_seconds']}s")
    print(f"  pages: {manifest['pages_total']} total, "
          f"{manifest['pages_text_sufficient']} text-sufficient, "
          f"{manifest['pages_need_vision']} need-vision, "
          f"{manifest['pages_redaction_flagged']} redaction-flagged, "
          f"{manifest['pages_blank']} blank/slip-sheet")
    if manifest["pages_total"]:
        pct = 100.0 * manifest["pages_text_sufficient"] / manifest["pages_total"]
        print(f"  vision-skip rate: {pct:.1f}%  (manifest: extract-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
