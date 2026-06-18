"""Stage-3 command-line interface.

    python -m src.stage3 ROOT [-j N] [--force] [--batch] [--dry-run]
                                              [--generated-at ISO8601] [connection + knob overrides]

Every Stage3Config field is exposed as a ``--flag`` (field name, underscores -> dashes) and echoed into
each sidecar's ``stage3.config`` so a run is reproducible from its own output. Booleans get a paired
``--flag`` / ``--no-flag`` form. The Azure connection comes from the environment (see connection.py); the
api KEY is env-only and never appears as a flag.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import STAGE3_TOOL_NAME, STAGE3_TOOL_VERSION
from .config import Stage3Config
from .connection import from_env, MissingConnection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-stage3",
        description=f"{STAGE3_TOOL_NAME} {STAGE3_TOOL_VERSION} — bundled gpt-4.1-nano page summaries + "
                    f"safety triage + provisional doc summary (Azure OpenAI; writes only the sidecar).")
    p.add_argument("root", help="root folder to walk recursively for *.pdf (with Stage-1/2 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="concurrent realtime calls (default: config.concurrency)")
    p.add_argument("--force", action="store_true",
                   help="re-summarize even pages/docs that already have a stage3 result")
    p.add_argument("--batch", action="store_true",
                   help="use the Azure OpenAI Batch API (~50%% cheaper, async <=24h) instead of realtime")
    p.add_argument("--dry-run", action="store_true",
                   help="project eligible pages / tokens / cost from existing sidecars; make NO API calls")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now)")
    p.add_argument("--progress-every", type=int, default=2000,
                   help="print a progress line every N completed PDFs (0 = silent until done)")
    p.add_argument("--poll-interval", type=float, default=30.0, help="batch poll interval seconds")
    p.add_argument("--max-wait", type=float, default=86_400.0, help="batch max wait seconds (24h)")
    p.add_argument("--content-filter-log", default=None,
                   help="path for the content-filter failures log (default: <ROOT>/content-filter-failures.json). "
                        "Merges across runs; lists every page Azure's content policy blocked.")
    p.add_argument("--version", action="version", version=f"{STAGE3_TOOL_NAME} {STAGE3_TOOL_VERSION}")

    c = p.add_argument_group("Azure connection overrides (key is env-only: AZURE_OPENAI_API_KEY)")
    c.add_argument("--endpoint", default=None, help="override AZURE_OPENAI_ENDPOINT")
    c.add_argument("--deployment", default=None, help="override AZURE_OPENAI_CHAT_NANO_DEPLOYMENT")
    c.add_argument("--api-version", default=None, help="override AZURE_OPENAI_CHAT_API_VERSION")

    g = p.add_argument_group("knobs (Handoff §3/§4/§5/§8) — all echoed into stage3.config")
    for f in dataclasses.fields(Stage3Config):
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


def config_from_args(args: argparse.Namespace) -> Stage3Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Stage3Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Stage3Config(), **overrides)


def _print_manifest(m: dict) -> None:
    u = m["usage"]
    print(f"{STAGE3_TOOL_NAME}: {m['pdfs_summarized']} summarized, "
          f"{m['pdfs_skipped_already_done']} already-done, {m['pdfs_errored']} errored, "
          f"{m['pdfs_no_sidecar'] + m['pdfs_no_stage2']} missing upstream "
          f"({m['pdfs_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  pages: {m['pages_summarized']} summarized, {m['pages_skipped']} skipped, "
          f"{m['pages_errored']} errored ({m['low_confidence']} low_confidence); "
          f"safety review={m['safety_review']}, content-filtered={m.get('content_filtered', 0)}")
    if m.get("content_filter_failures"):
        print(f"  content-filter: {m['content_filter_failures']} page(s) blocked by Azure -> "
              f"flagged review + logged at {m['content_filter_log']}")
    print(f"  spend{' (batch)' if u['batch'] else ''}: {u['prompt_tokens']} in + "
          f"{u['completion_tokens']} out tokens over {u['calls']} calls "
          f"=> ${u['estimated_cost_usd']:.4f}  (manifest: {os.path.basename('stage3-run-manifest.json')})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.dry_run:                                    # no connection / no API calls (Handoff §11)
        from .walk import dry_run
        d = dry_run(args.root, cfg)
        print(f"{STAGE3_TOOL_NAME} dry-run: {d['eligible_pages']} eligible pages across "
              f"{d['eligible_documents']} docs")
        print(f"  projected ~{d['estimated_prompt_tokens']} in + {d['estimated_completion_tokens']} out "
              f"tokens => realtime ${d['estimated_cost_usd_realtime']:.4f} / "
              f"batch ${d['estimated_cost_usd_batch']:.4f}")
        return 0

    try:
        conn = from_env(endpoint=args.endpoint, deployment=args.deployment, api_version=args.api_version)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    provenance = {"deployment": conn.deployment, "api_version": conn.api_version}

    if args.batch:
        from .batch import run_batch
        m = run_batch(args.root, cfg, generated_at, conn=conn, provenance=provenance, force=args.force,
                      poll_interval=args.poll_interval, max_wait=args.max_wait,
                      content_filter_log=args.content_filter_log)
    else:
        from .walk import run
        from .client import NanoClient
        client = NanoClient(conn, cfg)
        m = run(args.root, cfg, generated_at, client=client, provenance=provenance, force=args.force,
                workers=args.workers, mode="realtime", progress_every=args.progress_every,
                content_filter_log=args.content_filter_log)
    _print_manifest(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
