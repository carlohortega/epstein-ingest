"""Command-line interface (Handoff §8).

    python -m src.summary [ROOTS...] [--all] [--out DIR] [--format json|md|html|all]
                          [--sample K] [--chunks] [--safety-list] [--no-cache] [--rebuild-cache]
                          [--datasets DataSet-09,DataSet-11] [--generated-at ISO8601] [-j N]

Read-only: the only writes are the report artifacts + the per-dataset incremental cache. Default scan is
full, JSON+Markdown+HTML, incremental cache on. The corpus root for ``--all`` (when no ROOTS are given)
comes from ``$SUMMARY_CORPUS_ROOT`` or the conventional Windows data path."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from . import SUMMARY_TOOL_NAME, SUMMARY_TOOL_VERSION
from .config import SummaryConfig

_DEFAULT_CORPUS_ROOT = os.environ.get("SUMMARY_CORPUS_ROOT", r"C:\Projects\Data\epstein")
_ALL_FORMATS = ("json", "md", "html")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-summary",
        description=f"{SUMMARY_TOOL_NAME} {SUMMARY_TOOL_VERSION} — read-only cross-stage progress/cost/"
                    f"safety dashboard. Pruned-walks the sidecars, derives state from the per-doc blocks "
                    f"(not run-manifests), and writes JSON + Markdown + HTML. Never mutates the data tree.")
    p.add_argument("roots", nargs="*", help="dataset roots or a corpus root (DataSet-* are auto-discovered)")
    p.add_argument("--all", action="store_true",
                   help=f"scan every DataSet-* under the corpus root (default: {_DEFAULT_CORPUS_ROOT})")
    p.add_argument("--datasets", default=None,
                   help="comma-separated DataSet-NN names to restrict to (e.g. DataSet-09,DataSet-11)")
    p.add_argument("--out", default=None, help="write all reports into this directory instead of beside data")
    p.add_argument("--format", default="all", choices=("json", "md", "html", "all"),
                   help="report format(s) to emit (default: all = json+md+html)")
    p.add_argument("--safety-list", action="store_true",
                   help="also write a quarantine-list.json of flagged doc paths + reasons")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into reports (default: now)")
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel parse workers (default: cpu count)")
    p.add_argument("--sample", type=int, default=0, metavar="K",
                   help="read at most K sidecars per leaf folder for a fast ESTIMATE (labeled, extrapolated)")
    p.add_argument("--chunks", action="store_true",
                   help="also read each <name>.chunks.json for dedup/Stage-7-cost projection + exact per-doc "
                        "S7 embed coverage (slower)")
    p.add_argument("--store", default="", metavar="DIR",
                   help="Stage-7 vector store dir for S7 embed coverage (default: <corpus_root>/.embeddings)")
    p.add_argument("--include-non-pipeline", dest="images_only", action="store_false",
                   help="also count docs outside IMAGES/ (DS-09 MISSING-NATIVES/EMPTY/...); default excludes them")
    p.add_argument("--no-cache", dest="use_cache", action="store_false", help="disable the incremental cache")
    p.add_argument("--rebuild-cache", action="store_true", help="ignore + overwrite the incremental cache")
    p.add_argument("--version", action="version", version=f"{SUMMARY_TOOL_NAME} {SUMMARY_TOOL_VERSION}")
    return p


def _resolve_roots(args) -> list[str]:
    roots = list(args.roots)
    if args.all or not roots:
        roots = roots or [_DEFAULT_CORPUS_ROOT]
    return roots


def _filter_datasets(roots, names_csv):
    """Apply --datasets DataSet-NN[,NN...] by expanding roots and keeping only matching dataset dirs."""
    from .walk import discover_datasets
    wanted = {n.strip().lower() for n in names_csv.split(",") if n.strip()}
    out = []
    for r in roots:
        for ds in discover_datasets(r):
            if os.path.basename(ds).lower() in wanted:
                out.append(ds)
    return out


def config_from_args(args) -> SummaryConfig:
    return SummaryConfig(
        workers=args.workers or 0,
        use_cache=args.use_cache,
        rebuild_cache=args.rebuild_cache,
        sample=max(0, args.sample),
        chunks=args.chunks,
        store=args.store,
        images_only=args.images_only,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    roots = _resolve_roots(args)
    if args.datasets:
        roots = _filter_datasets(roots, args.datasets)
        if not roots:
            print(f"error: no datasets matched --datasets {args.datasets}", file=sys.stderr)
            return 2
    for r in roots:
        if not os.path.isdir(r):
            print(f"error: not a directory: {r}", file=sys.stderr)
            return 2

    if args.out:
        os.makedirs(args.out, exist_ok=True)

    cfg = config_from_args(args)
    formats = set(_ALL_FORMATS) if args.format == "all" else {args.format}
    generated_at = args.generated_at or _utc_now_iso()

    from .run import run
    print(f"{SUMMARY_TOOL_NAME}: scanning {len(roots)} root(s) (datasets auto-discovered) "
          f"({'sample ' + str(cfg.sample) if cfg.sample else 'full'}, "
          f"cache {'off' if not cfg.use_cache else 'on'})", flush=True)
    report = run(roots, cfg, generated_at=generated_at, formats=formats,
                 out_dir=args.out, safety_list=args.safety_list)

    c = report["corpus"]
    cost = c["cost"]
    print(f"\n{SUMMARY_TOOL_NAME}: {c['totals']['docs']:,} docs across {len(report['datasets'])} dataset(s) · "
          f"spent ${cost['total_usd']:,.2f} (~${cost['projected_remaining_usd']:,.2f} to finish) · "
          f"{c['safety']['docs_quarantined']:,} quarantined")
    for pth in report.get("_written", []):
        print(f"  wrote {pth}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
