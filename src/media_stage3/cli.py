"""media_stage3 CLI — ``python -m src.media_stage3 ROOT [-j N] [--force] [flags]``.

Builds the Azure AI Vision (vectorizeImage) + Content Safety clients from the environment. Content Safety
is non-negotiable on this corpus (the CSAM net); ``--skip-safety`` exists only for offline testing and
prints a loud warning.
"""

from __future__ import annotations

import argparse
import os
import sys

from .. import media
from ..media import frames_stage
from ..media.config import FramesConfig
from ..media.connection import (vision_from_env, content_safety_from_env, MissingConnection)
from ..media.vision import AzureVisionClient, ContentSafetyClient
from ..media.cliutil import add_config_args, config_from_args, utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media-frames",
        description=f"{media.FRAMES_TOOL_NAME} {media.FRAMES_TOOL_VERSION} — video keyframes + standalone "
                    f"image vision (1024-d) + Content Safety; emits image vectors + occurrences.")
    p.add_argument("root", help="root folder of probed media (needs media_stage1 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None, help="parallel asset threads (default: config concurrency)")
    p.add_argument("--force", action="store_true", help="re-frame even if a media_frames block exists")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into every JSON")
    p.add_argument("--vision-endpoint", default=None, help="override AZURE_AI_VISION_ENDPOINT")
    p.add_argument("--safety-endpoint", default=None, help="override AZURE_CONTENT_SAFETY_ENDPOINT")
    p.add_argument("--skip-safety", action="store_true",
                   help="DANGER: run without Content Safety (offline testing only)")
    p.add_argument("--version", action="version", version=f"{media.FRAMES_TOOL_NAME} {media.FRAMES_TOOL_VERSION}")
    g = p.add_argument_group("config (echoed into media_frames.config)")
    add_config_args(g, FramesConfig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args, FramesConfig)
    try:
        vconn = vision_from_env(endpoint=args.vision_endpoint)
        vision_client = AzureVisionClient(vconn, cfg)
        provenance = {"vision": vconn.provenance()}
        if args.skip_safety:
            print("WARNING: running WITHOUT Content Safety (--skip-safety). High-risk imagery will NOT "
                  "be redacted. Use for offline testing only.", file=sys.stderr)
            safety_client = None
        else:
            sconn = content_safety_from_env(endpoint=args.safety_endpoint)
            safety_client = ContentSafetyClient(sconn, cfg)
            provenance["content_safety"] = sconn.provenance()
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    generated_at = args.generated_at or utc_now_iso()
    m = frames_stage.run(args.root, cfg, generated_at, vision_client=vision_client,
                         safety_client=safety_client, provenance=provenance, force=args.force,
                         workers=args.workers)

    print(f"{media.FRAMES_TOOL_NAME}: {m['assets_framed']} framed, "
          f"{m['assets_skipped_already_done']} skipped, {m['assets_errored']} errored "
          f"({m['assets_seen']} visual seen) in {m['wall_clock_seconds']}s")
    print(f"  keyframes: {m['keyframes']} ({m['keyframes_embedded']} embedded, "
          f"{m['keyframes_redacted']} redacted) | safety: {m['safety_block']} block, "
          f"{m['safety_review']} review  (manifest: media-frames-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
