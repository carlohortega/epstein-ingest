"""media_stage1 engine — probe / register (HANDOFF-media-track §5).

Detect ``source_kind`` (extension; the walk already excludes the PDF ``IMAGES`` tree), run ``ffprobe`` to
capture container/codec metadata (duration, channels, sample_rate, width/height, frame_rate, bitrate),
and write the asset's sidecar: the typed ``media_metadata`` field set + the raw ffprobe JSONB bag.
Deterministic, local, no API.

The probe-FIRST discipline (HANDOFF §0a): this stage computes **total audio-minutes** — the cost driver
— so transcription $ can be projected before media_stage2 spends anything.
"""

from __future__ import annotations

import os
import time

from . import SCHEMA_VERSION, PROBE_TOOL_NAME, PROBE_TOOL_VERSION
from . import common
from . import ffmpeg
from .config import ProbeConfig

_MANIFEST_NAME = "media-probe-run-manifest.json"


def _generator(cfg: ProbeConfig, generated_at: str) -> dict:
    return {"tool": PROBE_TOOL_NAME, "tool_version": PROBE_TOOL_VERSION,
            "generated_at_utc": generated_at, "config": cfg.echo()}


def _base_sidecar(asset: str, root: str, kind: str, sha: str, cfg: ProbeConfig,
                  generated_at: str) -> dict:
    import mimetypes
    content_type = mimetypes.guess_type(asset)[0]
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "error": None,
        "generator": _generator(cfg, generated_at),
        "source": {
            "filename": os.path.basename(asset),
            "relative_path": common.rel_path(asset, root),
            "bytes": os.path.getsize(asset) if os.path.exists(asset) else 0,
            "sha256": sha,
            "content_type": content_type,
        },
        "document": {"source_kind": kind},
        "media_metadata": {},
        "media_probe": None,
    }


def _gate(asset: str, *, force: bool) -> tuple[dict | None, str | None]:
    """Return (existing_sidecar_or_None, skip_action_or_None). Re-probes on a prior ERROR so a corpus
    re-run after ffprobe is installed picks the failures up; skips only a successful prior probe."""
    sc = common.load_json(common.sidecar_path(asset))
    if sc is None:
        return None, None
    if not force and sc.get("media_probe") and sc.get("status") == "ok":
        return sc, "skipped_done"
    return sc, None


def process_asset(asset: str, kind: str, root: str, cfg: ProbeConfig, generated_at: str,
                  *, force: bool) -> dict:
    rel = common.rel_path(asset, root)
    existing, skip = _gate(asset, force=force)
    if skip is not None:
        return {"action": skip, "rel": rel, "kind": kind, "error_code": None,
                "duration_seconds": (existing.get("media_metadata") or {}).get("duration_seconds") or 0.0}

    try:
        sha = common.sha256_file(asset)
    except OSError as exc:
        return _error(asset, root, kind, cfg, generated_at, "read_error", str(exc))

    sidecar = _base_sidecar(asset, root, kind, sha, cfg, generated_at)
    try:
        raw = ffmpeg.probe_media(asset, ffprobe_bin=cfg.ffprobe_bin or ffmpeg.DEFAULT_FFPROBE,
                                 timeout=cfg.probe_timeout_seconds)
    except ffmpeg.FfmpegError as exc:
        return _error(asset, root, kind, cfg, generated_at, exc.code, str(exc), sha=sha)

    md = ffmpeg.typed_metadata(raw)
    # Additive on a forced re-probe: refresh the probe-derived container fields but PRESERVE downstream
    # stage state (transcript_*/keyframe_*/poster_* media_metadata keys + the media_transcribe/frames/
    # caption/chunk blocks). A fresh first probe / a prior-error sidecar simply has nothing to carry.
    if existing is not None and existing.get("status") == "ok":
        merged = dict(existing.get("media_metadata") or {})
        merged.update(md)
        sidecar["media_metadata"] = merged
        for k in ("media_transcribe", "media_transcribe_error", "media_frames", "media_frames_error",
                  "media_caption", "media_caption_error", "media_chunk"):
            if existing.get(k) is not None:
                sidecar[k] = existing[k]
    else:
        sidecar["media_metadata"] = md
    sidecar["media_probe"] = {
        "tool": PROBE_TOOL_NAME, "tool_version": PROBE_TOOL_VERSION,
        "generated_at_utc": generated_at, "config": cfg.echo(),
        "ffprobe": raw,  # the JSONB bag (full ffprobe output)
    }
    common.dump_json(sidecar, common.sidecar_path(asset))
    return {"action": "probed", "rel": rel, "kind": kind, "error_code": None,
            "duration_seconds": float(md.get("duration_seconds") or 0.0)}


def _error(asset: str, root: str, kind: str, cfg: ProbeConfig, generated_at: str,
           code: str, message: str, *, sha: str | None = None) -> dict:
    rel = common.rel_path(asset, root)
    import mimetypes
    sidecar = {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "error": {"code": code, "message": message[:500]},
        "generator": _generator(cfg, generated_at),
        "source": {"filename": os.path.basename(asset), "relative_path": rel,
                   "bytes": os.path.getsize(asset) if os.path.exists(asset) else 0,
                   "sha256": sha, "content_type": mimetypes.guess_type(asset)[0]},
        "document": {"source_kind": kind},
        "media_metadata": {},
        "media_probe": None,
    }
    common.dump_json(sidecar, common.sidecar_path(asset))
    return {"action": "error", "rel": rel, "kind": kind, "error_code": code,
            "message": message[:300], "duration_seconds": 0.0}


def run(root: str, cfg: ProbeConfig, generated_at: str, *, force: bool, workers: int) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    assets = common.find_media(root)
    total = len(assets)

    counts = {"probed": 0, "skipped_done": 0, "error": 0}
    by_kind = {"audio": 0, "video": 0, "image": 0}
    duration_by_kind = {"audio": 0.0, "video": 0.0, "image": 0.0}
    errors: list[dict] = []

    def consume(r: dict) -> None:
        counts[r["action"]] = counts.get(r["action"], 0) + 1
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
        duration_by_kind[r["kind"]] = duration_by_kind.get(r["kind"], 0.0) + (r.get("duration_seconds") or 0.0)
        if r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})

    def worker(item):
        asset, kind = item
        return process_asset(asset, kind, root, cfg, generated_at, force=force)

    common.drive(assets, worker, workers=workers, on_result=consume, total=total,
                 heartbeat_seconds=cfg.heartbeat_seconds, label="assets", started=started)

    av_minutes = (duration_by_kind["audio"] + duration_by_kind["video"]) / 60.0
    manifest = {
        "tool": PROBE_TOOL_NAME, "tool_version": PROBE_TOOL_VERSION,
        "generated_at_utc": generated_at, "root": root,
        "assets_seen": total,
        "assets_probed": counts["probed"],
        "assets_skipped_already_done": counts["skipped_done"],
        "assets_errored": counts["error"],
        "by_kind": by_kind,
        "audio_video_minutes": round(av_minutes, 2),
        "duration_seconds_by_kind": {k: round(v, 2) for k, v in duration_by_kind.items()},
        "errors": sorted(errors, key=lambda e: e["file"]),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    common.dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
