"""Stage-7 (embed) command-line interface.

    python -m src.embed ROOT [--store DIR] [--model NAME --dimensions N]
                             [--deployment NAME --api-version VER]
                             [-j N] [--rpm N --tpm N] [--batch-size N]
                             [--force] [--dry-run] [--generated-at ISO8601]

The MODEL/DIMENSIONS are LOCKED config (baked into content_sha, pick the target column) — confirm them
against the target tenant before a bulk run (Handoff §3.2/§8). The Azure endpoint/deployment/api-version +
the api KEY come from the environment (connection.py); the KEY is env-only. ``--dry-run`` makes NO API calls
(it only projects the unique-sha / token / $ work), so it needs no creds.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from . import EMBED_TOOL_NAME, EMBED_TOOL_VERSION, DEFAULT_EMBEDDING_MODEL, DEFAULT_DIMENSIONS
from .config import EmbedConfig


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="text-first-extract-embed",
        description=f"{EMBED_TOOL_NAME} {EMBED_TOOL_VERSION} — embed the staged text child-chunks into a "
                    f"content_sha-keyed float32 vector store (text only, dedup-global, resumable, no DB). "
                    f"Stage 8 loads the store.")
    p.add_argument("root", help="root folder to walk recursively for *.chunks.json (Stage-6 output)")
    p.add_argument("--store", default=None,
                   help="vector-store directory (default: <root>/.embeddings)")
    p.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL,
                   help=f"embedding model name, baked into content_sha (default: {DEFAULT_EMBEDDING_MODEL})")
    p.add_argument("--dimensions", type=int, default=DEFAULT_DIMENSIONS,
                   help=f"vector dimensions (default: {DEFAULT_DIMENSIONS}; 1536->small, 3072->large)")
    p.add_argument("--deployment", default=None,
                   help="Azure embeddings deployment name (default: $AZURE_OPENAI_EMBED_DEPLOYMENT)")
    p.add_argument("--api-version", default=None,
                   help="Azure embeddings api-version (default: $AZURE_OPENAI_EMBED_API_VERSION)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="concurrent embedding requests (default: config.concurrency)")
    p.add_argument("--rpm", type=int, default=None, help="client-side requests-per-minute cap (0=off)")
    p.add_argument("--tpm", type=int, default=None, help="client-side tokens-per-minute cap (0=off)")
    p.add_argument("--batch-size", type=int, default=None, help="children per embeddings request")
    p.add_argument("--force", action="store_true",
                   help="re-embed every unique content_sha (ignore the store's resume set)")
    p.add_argument("--dry-run", action="store_true",
                   help="report unique-sha / token / $ projection with NO API calls and NO writes")
    p.add_argument("--generated-at", default=None,
                   help="ISO8601 UTC timestamp stamped into the manifest/store (default: now)")
    p.add_argument("--progress-every", type=int, default=20000,
                   help="print a progress line every N embedded vectors (0 = silent until done)")
    p.add_argument("--version", action="version", version=f"{EMBED_TOOL_NAME} {EMBED_TOOL_VERSION}")
    return p


def config_from_args(args: argparse.Namespace) -> EmbedConfig:
    overrides: dict = {"model": args.model, "dimensions": args.dimensions}
    if args.rpm is not None:
        overrides["requests_per_minute"] = args.rpm
    if args.tpm is not None:
        overrides["tokens_per_minute"] = args.tpm
    if args.batch_size is not None:
        overrides["batch_size"] = args.batch_size
    return EmbedConfig(**overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args)
    generated_at = args.generated_at or _utc_now_iso()

    if args.dry_run:
        from .walk import dry_run
        d = dry_run(args.root, cfg, store_root=args.store, force=args.force)
        print(f"{EMBED_TOOL_NAME} dry-run: {d['children_seen']} children over {d['files_seen']} chunks.json "
              f"-> {d['unique_content_sha']} unique content_sha "
              f"({d['cached']} already embedded, {d['to_embed']} to embed)")
        print(f"  to embed: ~{d['to_embed_tokens']} tokens -> ~${d['estimated_cost_usd']:.4f} "
              f"(pre-dedup ceiling ~${d['ceiling_cost_usd']:.4f}); model={cfg.model}@{cfg.dimensions}; "
              f"no API calls")
        return 0

    # Real run: build the embeddings connection + provider (needs creds).
    from .connection import from_env, MissingConnection
    from .provider import AzureEmbedProvider
    from .walk import run
    try:
        conn = from_env(deployment=args.deployment, api_version=args.api_version)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    provider = AzureEmbedProvider(conn, cfg)
    m = run(args.root, cfg, generated_at, provider=provider, api_version=conn.api_version,
            deployment=conn.deployment, store_root=args.store, force=args.force, workers=args.workers,
            progress_every=args.progress_every)
    _print_manifest(m)
    return 0


def _print_manifest(m: dict) -> None:
    print(f"{EMBED_TOOL_NAME}: embedded {m['embedded']} unique content_sha "
          f"({m['cached']} cached/skipped) of {m['unique_content_sha']} unique "
          f"({m['children_seen']} children over {m['files_seen']} chunks.json) in "
          f"{m['wall_clock_seconds']}s ~${m['estimated_cost_usd']:.4f}")
    print(f"  model={m['embedding_model']}@{m['dimensions']} api={m['api_version']}  store: {m['store']}  "
          f"(manifest: embed-run-manifest.json)")
    if m.get("batch_errors"):
        print(f"  batch errors: {m['batch_errors']} ({m['errors'][:1]}...) — those shas re-embed on re-run")
    if m.get("files_errored"):
        print(f"  unreadable chunks.json: {m['files_errored']}")


if __name__ == "__main__":
    raise SystemExit(main())
