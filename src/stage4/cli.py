"""Stage-4 command-line interface.

    python -m src.stage4 ROOT [-j N] [--force] [--dry-run]
                                   [--no-embeddings] [--no-content-safety]
                                   [--generated-at ISO8601] [connection + knob overrides]

Every Stage4Config field is exposed as a ``--flag`` (field name, underscores -> dashes) and echoed into
each sidecar's ``stage4.config`` so a run is reproducible from its own output. Booleans get a paired
``--flag`` / ``--no-flag`` form. The Azure connections come from the environment (see connection.py); the
api KEYS are env-only and never appear as flags.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from datetime import datetime, timezone

from . import STAGE4_TOOL_NAME, STAGE4_TOOL_VERSION
from .config import Stage4Config
from .connection import from_env, MissingConnection


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-stage4",
        description=f"{STAGE4_TOOL_NAME} {STAGE4_TOOL_VERSION} — bundled gpt-5-mini image verdict "
                    f"(caption + description + has_visual_content gate + safety) + image embeddings + "
                    f"content safety (Azure; writes only the sidecar).")
    p.add_argument("root", help="root folder to walk recursively for *.pdf (with Stage-1/2 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="concurrent realtime calls (default: config.concurrency)")
    p.add_argument("--force", action="store_true",
                   help="re-process even assets/docs that already have a stage4 verdict")
    p.add_argument("--dry-run", action="store_true",
                   help="project eligible assets / tokens / cost from existing sidecars; make NO API calls")
    p.add_argument("--batch", action="store_true",
                   help="(not implemented for Stage 4 — realtime only; see Handoff §6)")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into every JSON (default: now)")
    p.add_argument("--progress-every", type=int, default=1000,
                   help="print a progress line every N completed PDFs (0 = silent until done)")
    p.add_argument("--content-filter-log", default=None,
                   help="path for the content-filter failures log "
                        "(default: <ROOT>/content-filter-failures-stage4.json). Merges across runs.")
    p.add_argument("--version", action="version", version=f"{STAGE4_TOOL_NAME} {STAGE4_TOOL_VERSION}")

    c = p.add_argument_group("Azure connection overrides (keys are env-only)")
    c.add_argument("--endpoint", default=None, help="override AZURE_OPENAI_ENDPOINT")
    c.add_argument("--deployment", default=None, help="override AZURE_OPENAI_VISION_DEPLOYMENT")
    c.add_argument("--api-version", default=None, help="override AZURE_OPENAI_CHAT_API_VERSION")

    g = p.add_argument_group("knobs (Handoff §2/§4/§5/§6) — all echoed into stage4.config")
    for f in dataclasses.fields(Stage4Config):
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


def config_from_args(args: argparse.Namespace) -> Stage4Config:
    overrides = {f.name: getattr(args, f.name)
                 for f in dataclasses.fields(Stage4Config) if getattr(args, f.name, None) is not None}
    return dataclasses.replace(Stage4Config(), **overrides)


def _print_manifest(m: dict) -> None:
    u = m["usage"]
    cnt = m["counts"]
    print(f"{STAGE4_TOOL_NAME}: {m['pdfs_processed']} processed, "
          f"{m['pdfs_skipped_already_done']} already-done, {m['pdfs_no_assets']} no-assets, "
          f"{m['pdfs_errored']} errored ({m['pdfs_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  assets: {cnt['assets']} total | has_visual_content={cnt['has_visual_content_true']} "
          f"captioned={cnt['captioned']} pregated_blank={cnt['pregated_blank']} "
          f"errored={cnt['errored']}")
    print(f"  embeddings: {cnt['embedded']} ({cnt['embed_errors']} errors) | "
          f"content-safety: {cnt['content_safety_scanned']} scanned, "
          f"{cnt['content_safety_review']} review | safety_review={cnt['safety_review']}")
    if m.get("content_filter_failures"):
        print(f"  content-filter: {m['content_filter_failures']} asset(s) blocked by Azure -> "
              f"flagged review + logged at {m['content_filter_log']}")
    print(f"  spend: {u['prompt_tokens']} in + {u['completion_tokens']} out tokens over {u['calls']} "
          f"calls => ${u['estimated_cost_usd']:.4f}  (manifest: {os.path.basename('stage4-run-manifest.json')})")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2
    if args.batch:
        print("error: Stage-4 batch mode is not implemented (realtime only). See Handoff §6 "
              "(Azure caps a batch at ~100k requests; multi-job chunking is the open work).",
              file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.dry_run:                                    # no connection / no API calls (Handoff §10)
        from .walk import dry_run
        d = dry_run(args.root, cfg, force=args.force)
        print(f"{STAGE4_TOOL_NAME} dry-run: {d['eligible_assets']} eligible assets across "
              f"{d['documents_with_vision']} docs with vision work")
        print(f"  metric pre-gate removes {d['pregated_blank_metric']} blank(s) -> {d['vision_calls']} "
              f"vision call(s) (PNG-measured pre-gate at run time removes more)")
        print(f"  projected ~{d['estimated_prompt_tokens']} in + {d['estimated_completion_tokens']} out "
              f"tokens => realtime ${d['estimated_cost_usd_realtime']:.4f} / "
              f"batch ${d['estimated_cost_usd_batch']:.4f}")
        return 0

    try:
        conn = from_env(endpoint=args.endpoint, deployment=args.deployment, api_version=args.api_version,
                        want_embeddings=cfg.embeddings, want_content_safety=cfg.content_safety)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # Build the (optional) embedder + scanner, warning loudly when a requested capability isn't configured.
    from .client import VisionClient
    from .embed import ImageEmbedder
    from .safety import ContentSafetyScanner

    client = VisionClient(conn.vision, cfg)
    embedder = None
    embedding_model = None
    if cfg.embeddings:
        if conn.ai_vision is not None:
            embedder = ImageEmbedder(conn.ai_vision, cfg)
            embedding_model = "azure-ai-vision-multimodal"
        else:
            print("warning: --embeddings requested but AZURE_AI_VISION_* is not configured; "
                  "image embeddings will be SKIPPED for this run.", file=sys.stderr)
    scanner = None
    content_safety_prov = None
    if cfg.content_safety:
        if conn.content_safety is not None:
            scanner = ContentSafetyScanner(conn.content_safety, cfg)
            content_safety_prov = "azure-content-safety"
        else:
            print("WARNING: --content-safety requested but AZURE_CONTENT_SAFETY_* is not configured. "
                  "The independent image CSAM net is OFF for this run (vision safety tags still apply). "
                  "Provision Azure Content Safety + set the env vars, then re-run --force to add it.",
                  file=sys.stderr)
            content_safety_prov = "not_configured"

    provenance = {"deployment": conn.vision.deployment, "api_version": conn.vision.api_version,
                  "embedding_model": embedding_model, "content_safety": content_safety_prov}

    from .walk import run
    m = run(args.root, cfg, generated_at, client=client, embedder=embedder, scanner=scanner,
            provenance=provenance, force=args.force, workers=args.workers,
            progress_every=args.progress_every, content_filter_log=args.content_filter_log)
    _print_manifest(m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
