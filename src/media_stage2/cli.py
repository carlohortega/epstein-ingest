"""media_stage2 CLI — ``python -m src.media_stage2 ROOT [--dry-run] [-j N] [--force] [flags]``.

The Azure connection (endpoint/deployment/api-version) is read from the environment; the api KEY is
env-only (never a flag). ``--dry-run`` projects cost from probed durations with NO API calls.
"""

from __future__ import annotations

import argparse
import os
import sys

from .. import media
from ..media import transcribe_stage
from ..media.config import TranscribeConfig
from ..media.connection import transcribe_from_env, MissingConnection
from ..media.transcribe import AzureTranscribeClient
from ..media.cliutil import add_config_args, config_from_args, default_workers, utc_now_iso


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="media-transcribe",
        description=f"{media.TRANSCRIBE_TOOL_NAME} {media.TRANSCRIBE_TOOL_VERSION} — transcribe A/V via "
                    f"Azure OpenAI gpt-4o-mini-transcribe (the premium line; cost ∝ audio minutes).")
    p.add_argument("root", help="root folder of probed media (needs media_stage1 sidecars)")
    p.add_argument("-j", "--workers", type=int, default=None,
                   help="parallel asset threads (default: config concurrency)")
    p.add_argument("--force", action="store_true", help="re-transcribe even if a media_transcribe block exists")
    p.add_argument("--dry-run", action="store_true", help="project audio-minutes + cost from probes; no API calls")
    p.add_argument("--generated-at", default=None, help="ISO8601 UTC timestamp stamped into every JSON")
    p.add_argument("--endpoint", default=None, help="override AZURE_OPENAI_ENDPOINT")
    p.add_argument("--deployment", default=None, help="override AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT")
    p.add_argument("--api-version", default=None, help="override AZURE_OPENAI_CHAT_API_VERSION")
    p.add_argument("--version", action="version",
                   version=f"{media.TRANSCRIBE_TOOL_NAME} {media.TRANSCRIBE_TOOL_VERSION}")
    g = p.add_argument_group("config (echoed into media_transcribe.config)")
    add_config_args(g, TranscribeConfig)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: not a directory: {args.root}", file=sys.stderr)
        return 2

    cfg = config_from_args(args, TranscribeConfig)

    if args.dry_run:
        d = transcribe_stage.dry_run(args.root, cfg)
        print(f"transcribe dry-run: {d['av_assets_probed']} A/V probed "
              f"({d['av_assets_pending']} pending), {d['audio_minutes']} audio-minutes "
              f"→ ~${d['estimated_cost_usd']} at ${cfg.cost_per_audio_minute}/min")
        return 0

    try:
        conn = transcribe_from_env(endpoint=args.endpoint, deployment=args.deployment,
                                   api_version=args.api_version)
    except MissingConnection as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    client = AzureTranscribeClient(conn, cfg)
    generated_at = args.generated_at or utc_now_iso()
    m = transcribe_stage.run(args.root, cfg, generated_at, client=client, provenance=conn.provenance(),
                             force=args.force, workers=args.workers)

    print(f"{media.TRANSCRIBE_TOOL_NAME}: {m['assets_transcribed']} transcribed, "
          f"{m['assets_no_audio_stream']} no-audio, {m['assets_silent_audio']} silent, "
          f"{m['assets_skipped_already_done']} skipped, {m['assets_errored']} errored "
          f"({m['assets_seen']} A/V seen) in {m['wall_clock_seconds']}s")
    print(f"  windows: {m['windows']} ({m['windows_errored']} errored, "
          f"{m['windows_skipped_silent']} silent-skipped), {m['audio_minutes']} min, "
          f"~${m['usage']['estimated_cost_usd']}  (manifest: media-transcribe-run-manifest.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
