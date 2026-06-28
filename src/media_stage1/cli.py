"""media_stage1 CLI — ``python -m src.media_stage1 ROOT [-j N] [--force] [--dry-run] [flags]``."""

from __future__ import annotations

import argparse
import os
import sys

from .. import media
from ..media import common, probe
from ..media.config import ProbeConfig
from ..media.cliutil import add_config_args, config_from_args, default_workers, utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media-probe",
        description=f"{media.PROBE_TOOL_NAME} {media.PROBE_TOOL_VERSION} — probe/register media assets "
                    f"(ffprobe → sidecar + media_metadata). Local, offline.")
    p.add_argument("root", help="root folder to walk for media (audio/video/image; excludes the PDF IMAGES tree)")
    p.add_argument("-j", "--workers", type=int, default=default_workers(),
                   help="parallel worker threads (default: CPU-2)")
    p.add_argument("--force", action="store_true", help="re-probe even if a sidecar already has a media_probe block")
    p.add_argument("--dry-run", action="store_true",
                   help="just inventory media (count + extensions) with no ffprobe — sizes the corpus")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into every JSON")
    p.add_argument("--version", action="version", version=f"{media.PROBE_TOOL_NAME} {media.PROBE_TOOL_VERSION}")
    g = p.add_argument_group("config (echoed into media_probe.config)")
    add_config_args(g, ProbeConfig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    if args.dry_run:
        assets = common.find_media(args.root)
        by_kind: dict[str, int] = {}
        for _p, k in assets:
            by_kind[k] = by_kind.get(k, 0) + 1
        print(f"media inventory: {len(assets)} assets — " +
              ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))
        return 0

    cfg = config_from_args(args, ProbeConfig)
    generated_at = args.generated_at or utc_now_iso()
    m = probe.run(args.root, cfg, generated_at, force=args.force, workers=args.workers)

    print(f"{media.PROBE_TOOL_NAME}: {m['assets_probed']} probed, "
          f"{m['assets_skipped_already_done']} skipped, {m['assets_errored']} errored "
          f"({m['assets_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  by kind: " + ", ".join(f"{k}={v}" for k, v in m['by_kind'].items()))
    print(f"  audio+video minutes: {m['audio_video_minutes']}  "
          f"(transcription cost driver; manifest: media-probe-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
