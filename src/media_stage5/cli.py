"""media_stage5 CLI — ``python -m src.media_stage5 ROOT [-j N] [--force] [--case-key K] [flags]``."""

from __future__ import annotations

import argparse
import os
import sys

from .. import media
from ..media import chunk_stage
from ..media.config import ChunkConfig
from ..media.cliutil import add_config_args, config_from_args, default_workers, utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media-chunk",
        description=f"{media.CHUNK_TOOL_NAME} {media.CHUNK_TOOL_VERSION} — chunk A/V transcripts + media "
                    f"captions into <name>.chunks.json (the shared S7/S8 chunk contract). Local.")
    p.add_argument("root", help="root folder of transcribed/captioned media (needs media_stage2/4 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=default_workers(),
                   help="parallel worker threads (default: CPU-2)")
    p.add_argument("--force", action="store_true", help="re-chunk even if a media_chunk block exists")
    p.add_argument("--case-key", default="",
                   help="case_key for content_sha (live sv-kb convention, e.g. 'epstein'); "
                        "falls back to the VOLxxxxx path segment if omitted")
    p.add_argument("--dataset-key", default="",
                   help="dataset_key for content_sha; falls back to the DataSet-NN path segment if omitted")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into every JSON")
    p.add_argument("--version", action="version", version=f"{media.CHUNK_TOOL_NAME} {media.CHUNK_TOOL_VERSION}")
    g = p.add_argument_group("config (echoed into media_chunk.config)")
    add_config_args(g, ChunkConfig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args, ChunkConfig)
    generated_at = args.generated_at or utc_now_iso()
    m = chunk_stage.run(args.root, cfg, generated_at, case_key=args.case_key,
                        dataset_key=args.dataset_key, force=args.force, workers=args.workers)

    print(f"{media.CHUNK_TOOL_NAME}: {m['assets_chunked']} chunked, "
          f"{m['assets_skipped_already_done']} skipped, {m['assets_nothing_to_chunk']} with nothing to chunk, "
          f"{m['assets_errored']} errored ({m['assets_seen']} seen) in {m['wall_clock_seconds']}s")
    print(f"  chunks: {m['parents']} parents, {m['children']} children  "
          f"(manifest: media-chunk-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
