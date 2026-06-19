"""Stage-5 command-line interface.

    python -m src.stage5 ROOT [-j N] [--force] [--dry-run]
                                              [--generated-at ISO8601] [connection + knob overrides]

Every Stage5Config field is exposed as a ``--flag`` (field name, underscores -> dashes) and echoed into
each sidecar's ``stage5.config`` so a run is reproducible from its own output. Booleans get a paired
``--flag`` / ``--no-flag`` form. The Azure connection comes from the environment (reusing Stage 3's
connection.py); the api KEY is env-only and never appears as a flag.

Stage 5 is realtime-only (no --batch): it finalizes one document per call (a promote/no-content doc makes
NO call; a fused/image-only doc makes one cheap nano reduce), so the Batch API's async round-trip buys
little here.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import STAGE5_TOOL_NAME, STAGE5_TOOL_VERSION
from .config import Stage5Config
from ..stage3.connection import from_env, MissingConnection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-stage5",
        description=f"{STAGE5_TOOL_NAME} {STAGE5_TOOL_VERSION} — finalize the document summary by fusing the "
                    f"Stage-3 text lane and the Stage-4 vision lane (gpt-4.1-nano; append-only sidecar).")
    p.add_argument("root", help="root folder to walk recursively for *.pdf (with Stage-1..4 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="concurrent realtime calls (default: config.concurrency)")
    p.add_argument("--force", action="store_true",
                   help="re-finalize even docs that already have a stage5 block")
    p.add_argument("--dry-run", action="store_true",
                   help="project the promote/fuse/image-only/no-content breakdown + nano $ from existing "
                        "sidecars; make NO API calls")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now)")
    p.add_argument("--progress-every", type=int, default=2000,
                   help="print a progress line every N completed PDFs (0 = silent until done)")
    p.add_argument("--content-filter-log", default=None,
                   help="path for the content-filter failures log (default: <ROOT>/content-filter-failures.json). "
                        "Merges across runs; lists every reduce Azure's content policy blocked (rare).")
    p.add_argument("--version", action="version", version=f"{STAGE5_TOOL_NAME} {STAGE5_TOOL_VERSION}")

    c = p.add_argument_group("Azure connection overrides (key is env-only: AZURE_OPENAI_API_KEY)")
    c.add_argument("--endpoint", default=None, help="override AZURE_OPENAI_ENDPOINT")
    c.add_argument("--deployment", default=None, help="override AZURE_OPENAI_CHAT_NANO_DEPLOYMENT")
    c.add_argument("--api-version", default=None, help="override AZURE_OPENAI_CHAT_API_VERSION")

    g = p.add_argument_group("knobs (Handoff §3/§9) — all echoed into stage5.config")
    for f in dataclasses.fields(Stage5Config):
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


def config_from_args(args: argparse.Namespace) -> Stage5Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Stage5Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Stage5Config(), **overrides)


def _print_manifest(m: dict) -> None:
    c = m["counts"]
    u = m["usage"]
    print(f"{STAGE5_TOOL_NAME}: {m['pdfs_finalized']} finalized, "
          f"{m['pdfs_skipped_already_done']} already-done, {m['pdfs_errored']} errored, "
          f"{m['pdfs_no_sidecar'] + m['pdfs_no_stage3'] + m['pdfs_no_stage4']} missing upstream "
          f"({m['pdfs_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  routes: promoted={c['promoted']} fused={c['fused']} image_only={c['image_only']} "
          f"no_content={c['no_content']}; quarantined={c['quarantined']} safety_review={c['safety_review']}")
    if m.get("content_filter_failures"):
        print(f"  content-filter: {m['content_filter_failures']} reduce(s) blocked by Azure -> "
              f"quarantined + logged at {m['content_filter_log']}")
    print(f"  spend: {u['prompt_tokens']} in + {u['completion_tokens']} out tokens over {u['calls']} "
          f"calls => ${u['estimated_cost_usd']:.4f}  (manifest: stage5-run-manifest.json)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.dry_run:                                    # no connection / no API calls (Handoff §10.8)
        from .walk import dry_run
        d = dry_run(args.root, cfg, force=args.force)
        print(f"{STAGE5_TOOL_NAME} dry-run: {d['eligible_documents']} eligible docs -> "
              f"promoted={d['promoted']} fused={d['fused']} image_only={d['image_only']} "
              f"no_content={d['no_content']}")
        print(f"  {d['reduce_calls']} reduce call(s); projected ~{d['estimated_prompt_tokens']} in + "
              f"{d['estimated_completion_tokens']} out tokens => realtime "
              f"${d['estimated_cost_usd_realtime']:.4f}")
        return 0

    try:
        conn = from_env(endpoint=args.endpoint, deployment=args.deployment, api_version=args.api_version)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    provenance = {"deployment": conn.deployment, "api_version": conn.api_version}

    from .walk import run
    from ..stage3.client import NanoClient
    client = NanoClient(conn, cfg)
    m = run(args.root, cfg, generated_at, client=client, provenance=provenance, force=args.force,
            workers=args.workers, mode="realtime", progress_every=args.progress_every,
            content_filter_log=args.content_filter_log)
    _print_manifest(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
