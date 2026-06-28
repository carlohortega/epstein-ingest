"""media_stage4 CLI — ``python -m src.media_stage4 ROOT [-j N] [--force] [flags]``.

Generates doc-level captions/descriptions via Azure OpenAI gpt-5-mini (the Stage-4 vision deployment).
The connection comes from the environment (``AZURE_OPENAI_ENDPOINT`` / ``_API_KEY`` /
``AZURE_OPENAI_VISION_DEPLOYMENT``); the api KEY is env-only.
"""

from __future__ import annotations

import argparse
import os
import sys

from .. import media
from ..media import caption_stage
from ..media.config import CaptionConfig
from ..media.connection import caption_from_env, MissingConnection
from ..media.caption import MediaCaptionClient
from ..media.cliutil import add_config_args, config_from_args, utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media-caption",
        description=f"{media.CAPTION_TOOL_NAME} {media.CAPTION_TOOL_VERSION} — doc caption + description "
                    f"(gpt-5-mini): video from transcript+keyframes, audio from transcript, image direct.")
    p.add_argument("root", help="root folder of transcribed/framed media (needs media_stage2/3 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel asset threads (default: config concurrency)")
    p.add_argument("--force", action="store_true", help="re-caption even if a media_caption block exists")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into every JSON")
    p.add_argument("--endpoint", default=None, help="override AZURE_OPENAI_ENDPOINT")
    p.add_argument("--deployment", default=None, help="override AZURE_OPENAI_VISION_DEPLOYMENT")
    p.add_argument("--api-version", default=None, help="override AZURE_OPENAI_CHAT_API_VERSION")
    p.add_argument("--version", action="version", version=f"{media.CAPTION_TOOL_NAME} {media.CAPTION_TOOL_VERSION}")
    g = p.add_argument_group("config (echoed into media_caption.config)")
    add_config_args(g, CaptionConfig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args, CaptionConfig)
    try:
        conn = caption_from_env(endpoint=args.endpoint, deployment=args.deployment,
                                api_version=args.api_version)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = MediaCaptionClient(conn, cfg)
    provenance = {"deployment": conn.deployment, "api_version": conn.api_version}
    generated_at = args.generated_at or utc_now_iso()
    m = caption_stage.run(args.root, cfg, generated_at, client=client, provenance=provenance,
                          force=args.force, workers=args.workers)

    print(f"{media.CAPTION_TOOL_NAME}: {m['assets_captioned']} captioned, "
          f"{m['assets_redacted_skipped']} redacted-skipped, {m['assets_skipped_already_done']} skipped, "
          f"{m['assets_errored']} errored ({m['assets_seen']} seen) in {m['wall_clock_seconds']}s")
    u = m["usage"]
    print(f"  tokens: {u['prompt_tokens']} in / {u['completion_tokens']} out "
          f"({u['cached_tokens']} cached) ~${u['estimated_cost_usd']}  "
          f"(manifest: media-caption-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
