"""media_stage2 engine — transcribe A/V (HANDOFF-media-track §2/§5).

ffmpeg-segment the audio track (``-vn`` so a video's audio transcribes identically) into
``window_seconds`` windows → ``gpt-4o-mini-transcribe`` per window → ``TranscriptWindow``s. Writes the
``transcript.md`` / ``transcript.vtt`` / ``transcribe.meta.json`` blobs and appends a ``media_transcribe``
block (with the windows S4 chunks) to the sidecar, updating ``media_metadata`` transcript fields.

This is the premium line (cost ∝ audio minutes). It mirrors Stage 3: an injected client (tests pass a
fake), shared rate limiter, per-window fault isolation, idempotent/resumable, never destructive. A
transcription/ffmpeg failure records a ``media_transcribe_error`` (NOT a ``media_transcribe`` block) so a
resume retries it; the probe sidecar is preserved.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from . import TRANSCRIBE_TOOL_NAME, TRANSCRIBE_TOOL_VERSION
from . import common
from . import ffmpeg
from .config import TranscribeConfig
from .chunking import transcript_markdown, transcript_vtt
from .transcribe import TranscriptWindow, TranscribeCallError, estimate_cost_usd

_MANIFEST_NAME = "media-transcribe-run-manifest.json"
_AV_KINDS = ("audio", "video")


def _gate(asset: str, *, force: bool):
    """(sidecar_or_None, skip_action_or_None). Requires a successful probe + an A/V kind."""
    sc = common.load_json(common.sidecar_path(asset))
    if sc is None:
        return None, "no_sidecar"
    if sc.get("status") != "ok" or not sc.get("media_probe"):
        return None, "no_probe"
    if (sc.get("document") or {}).get("source_kind") not in _AV_KINDS:
        return None, "not_av"
    if sc.get("media_transcribe") and not force:
        return sc, "skipped_done"
    return sc, None


def _transcribe_windows(client, segments: list[str], duration: float, cfg: TranscribeConfig,
                        nonsilent):
    """Transcribe each segment (bounded per-asset window pool), SKIPPING silent windows (no API call) when
    ``nonsilent`` is provided. Returns (windows, errored, skipped_silent, transcribed_seconds) — billed
    cost is derived from transcribed_seconds, so silence directly cuts spend."""
    n = len(segments)

    def one(i: int):
        start = i * cfg.window_seconds
        end = min(duration, (i + 1) * cfg.window_seconds) if duration > 0 else (i + 1) * cfg.window_seconds
        if nonsilent is not None and not ffmpeg.interval_has_sound(start, end, nonsilent):
            return TranscriptWindow(i, float(start), float(end), ""), None, True   # silent → no API call
        try:
            text = client.transcribe_file(segments[i])
            return TranscriptWindow(i, float(start), float(end), text), None, False
        except TranscribeCallError as exc:
            return TranscriptWindow(i, float(start), float(end), ""), exc, False

    workers = max(1, min(cfg.window_concurrency, n))
    if workers <= 1:
        results = [one(i) for i in range(n)]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, range(n)))
    windows = [w for w, _, _ in results]
    errored = sum(1 for _, e, _ in results if e is not None)
    skipped_silent = sum(1 for _, _, s in results if s)
    transcribed_seconds = sum((w.end_s - w.start_s) for w, _, s in results if not s)
    return windows, errored, skipped_silent, transcribed_seconds


def process_asset(asset: str, kind: str, root: str, cfg: TranscribeConfig, generated_at: str,
                  *, client, provenance: dict, force: bool) -> dict:
    rel = common.rel_path(asset, root)
    sc, skip = _gate(asset, force=force)
    if skip is not None and sc is None:
        return _result(skip, rel, kind)
    if skip == "skipped_done":
        return _result(skip, rel, kind, duration=_duration(sc))

    duration = _duration(sc)
    has_audio = (sc.get("media_metadata") or {}).get("has_audio")
    out_dir = common.asset_dir(asset)
    seg_dir = os.path.join(out_dir, "segments")
    ff = cfg.ffmpeg_bin or ffmpeg.DEFAULT_FFMPEG

    # (1) no audio stream at all → skip transcription entirely (not an error; e.g. silent surveillance video)
    if cfg.skip_no_audio and has_audio is False:
        return _finalize_skipped(asset, sc, rel, kind, cfg, generated_at, provenance, duration,
                                 reason="no_audio_stream")

    # (2) silence gating: detect sound; a fully-silent file is skipped, partially-silent windows are not
    #     transcribed (the cost saver on long ambient surveillance audio)
    nonsilent = None
    if cfg.skip_silent and duration > 0:
        try:
            silent = ffmpeg.detect_silence(asset, noise_db=cfg.silence_noise_db,
                                           min_seconds=cfg.silence_min_seconds, ffmpeg_bin=ff,
                                           timeout=cfg.silence_timeout_seconds)
            nonsilent = ffmpeg.non_silent_intervals(silent, duration)
            if not nonsilent:
                return _finalize_skipped(asset, sc, rel, kind, cfg, generated_at, provenance, duration,
                                         reason="silent_audio")
        except ffmpeg.FfmpegError:
            nonsilent = None  # detection failed → fall back to transcribing everything (safe)

    try:
        segments = ffmpeg.segment_audio(
            asset, seg_dir, window_seconds=cfg.window_seconds, sample_rate=cfg.sample_rate,
            channels=cfg.channels, bitrate_kbps=cfg.bitrate_kbps,
            ffmpeg_bin=ff, timeout=cfg.segment_timeout_seconds)
    except ffmpeg.FfmpegError as exc:
        return _record_error(asset, sc, rel, kind, generated_at, exc.code, str(exc))

    if not segments:
        return _finalize_skipped(asset, sc, rel, kind, cfg, generated_at, provenance, duration,
                                 reason="no_audio_stream")

    windows, errored, skipped_silent, transcribed_seconds = _transcribe_windows(
        client, segments, duration, cfg, nonsilent)
    shutil.rmtree(seg_dir, ignore_errors=True)  # intermediate audio segments aren't kept

    win_dicts = [w.to_dict() for w in windows]
    word_count = sum(len((w.text or "").split()) for w in windows)
    cost = (transcribed_seconds / 60.0) * cfg.cost_per_audio_minute  # billed = transcribed, not total

    # blobs
    md_rel = os.path.join(os.path.basename(out_dir), "transcript.md").replace(os.sep, "/")
    common.write_text(transcript_markdown(win_dicts), os.path.join(out_dir, "transcript.md"))
    common.write_text(transcript_vtt(win_dicts), os.path.join(out_dir, "transcript.vtt"))
    meta = {"asset": rel, "source_kind": kind, "duration_seconds": duration,
            "window_seconds": cfg.window_seconds, "windows": len(windows),
            "windows_errored": errored, "word_count": word_count, "model": cfg.model,
            "language": cfg.language, "estimated_cost_usd": round(cost, 6),
            "deployment": provenance.get("deployment"), "api_version": provenance.get("api_version")}
    common.dump_json(meta, os.path.join(out_dir, "transcribe.meta.json"))

    # update media_metadata (transcript fields) + append the media_transcribe block
    sc.setdefault("media_metadata", {})
    sc["media_metadata"].update({
        "transcript_blob_path": md_rel, "transcript_model": cfg.model,
        "transcript_word_count": word_count, "transcript_language": cfg.language,
        "speaker_count": None,  # diarization deferred (HANDOFF §9)
    })
    sc.pop("media_transcribe_error", None)
    sc["media_transcribe"] = {
        "tool": TRANSCRIBE_TOOL_NAME, "tool_version": TRANSCRIBE_TOOL_VERSION,
        "model": cfg.model, "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"), "generated_at_utc": generated_at,
        "config": cfg.echo(),
        "duration_seconds": duration, "window_count": len(windows), "windows_errored": errored,
        "windows_skipped_silent": skipped_silent,
        "word_count": word_count, "speaker_label": None,
        "transcript_md_path": md_rel,
        "transcript_vtt_path": os.path.join(os.path.basename(out_dir), "transcript.vtt").replace(os.sep, "/"),
        "usage": {"audio_minutes": round(duration / 60.0, 4),
                  "transcribed_minutes": round(transcribed_seconds / 60.0, 4),
                  "estimated_cost_usd": round(cost, 6)},
        "windows": win_dicts,
    }
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("transcribed", rel, kind, duration=duration, windows=len(windows),
                   windows_errored=errored, windows_skipped=skipped_silent, cost=cost)


def _finalize_skipped(asset, sc, rel, kind, cfg, generated_at, provenance, duration, *, reason) -> dict:
    """No-audio / fully-silent A/V: write a media_transcribe block with zero windows (NOT an error, NOT a
    spend) so downstream proceeds (video caption still uses keyframes; chunk emits no transcript chunks)."""
    sc.setdefault("media_metadata", {})
    sc["media_metadata"].update({
        "transcript_blob_path": None, "transcript_model": cfg.model,
        "transcript_word_count": 0, "transcript_language": cfg.language, "speaker_count": None,
    })
    sc.pop("media_transcribe_error", None)
    sc["media_transcribe"] = {
        "tool": TRANSCRIBE_TOOL_NAME, "tool_version": TRANSCRIBE_TOOL_VERSION,
        "model": cfg.model, "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"), "generated_at_utc": generated_at,
        "config": cfg.echo(), "skipped_reason": reason,
        "duration_seconds": duration, "window_count": 0, "windows_errored": 0,
        "windows_skipped_silent": 0, "word_count": 0, "speaker_label": None,
        "transcript_md_path": None, "transcript_vtt_path": None,
        "usage": {"audio_minutes": round(duration / 60.0, 4), "transcribed_minutes": 0.0,
                  "estimated_cost_usd": 0.0},
        "windows": [],
    }
    common.dump_json(sc, common.sidecar_path(asset))
    return _result(reason, rel, kind, duration=duration)


def _duration(sc: dict) -> float:
    return float((sc.get("media_metadata") or {}).get("duration_seconds") or 0.0)


def _record_error(asset, sc, rel, kind, generated_at, code, message) -> dict:
    """Record a transcribe failure without clobbering the probe sidecar; resume retries it."""
    sc["media_transcribe_error"] = {"code": code, "message": message[:300],
                                    "generated_at_utc": generated_at}
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("error", rel, kind, error_code=code, message=message[:300])


def _result(action: str, rel: str, kind: str, *, error_code=None, message=None, duration=0.0,
            windows=0, windows_errored=0, windows_skipped=0, cost=0.0) -> dict:
    return {"action": action, "rel": rel, "kind": kind, "error_code": error_code, "message": message,
            "duration_seconds": duration, "windows": windows, "windows_errored": windows_errored,
            "windows_skipped": windows_skipped, "cost": cost}


def run(root: str, cfg: TranscribeConfig, generated_at: str, *, client, provenance: dict,
        force: bool, workers: int | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    assets = [(p, k) for p, k in common.find_media(root) if k in _AV_KINDS]
    total = len(assets)
    workers = workers or cfg.concurrency

    actions = {"transcribed": 0, "skipped_done": 0, "no_sidecar": 0, "no_probe": 0,
               "not_av": 0, "no_audio_stream": 0, "silent_audio": 0, "error": 0}
    agg = {"windows": 0, "windows_errored": 0, "windows_skipped": 0, "cost": 0.0, "minutes": 0.0}
    errors: list[dict] = []
    missing: list[str] = []

    def consume(r: dict) -> None:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "transcribed":
            agg["windows"] += r["windows"]
            agg["windows_errored"] += r["windows_errored"]
            agg["windows_skipped"] += r["windows_skipped"]
            agg["cost"] += r["cost"]
            agg["minutes"] += (r["duration_seconds"] or 0.0) / 60.0
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})
        elif r["action"] in ("no_sidecar", "no_probe"):
            missing.append(r["rel"])

    def worker(item):
        asset, kind = item
        return process_asset(asset, kind, root, cfg, generated_at, client=client,
                             provenance=provenance, force=force)

    common.drive(assets, worker, workers=workers, on_result=consume, total=total, label="A/V assets",
                 started=started)

    manifest = {
        "tool": TRANSCRIBE_TOOL_NAME, "tool_version": TRANSCRIBE_TOOL_VERSION,
        "model": cfg.model, "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"), "generated_at_utc": generated_at, "root": root,
        "assets_seen": total,
        "assets_transcribed": actions["transcribed"],
        "assets_skipped_already_done": actions["skipped_done"],
        "assets_no_audio_stream": actions["no_audio_stream"],
        "assets_silent_audio": actions["silent_audio"],
        "assets_no_sidecar": actions["no_sidecar"],
        "assets_no_probe": actions["no_probe"],
        "assets_errored": actions["error"],
        "windows": agg["windows"], "windows_errored": agg["windows_errored"],
        "windows_skipped_silent": agg["windows_skipped"],
        "audio_minutes": round(agg["minutes"], 2),
        "usage": {"estimated_cost_usd": round(agg["cost"], 6)},
        "errors": sorted(errors, key=lambda e: e["file"]),
        "skipped_missing_upstream": sorted(missing),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    common.dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest


def dry_run(root: str, cfg: TranscribeConfig) -> dict:
    """Project transcription tokens/cost from probed durations with NO API calls (HANDOFF §0a/§8)."""
    root = os.path.abspath(root)
    minutes = 0.0
    assets = pending = 0
    for asset, kind in common.find_media(root):
        if kind not in _AV_KINDS:
            continue
        sc = common.load_json(common.sidecar_path(asset))
        if not sc or sc.get("status") != "ok" or not sc.get("media_probe"):
            continue
        assets += 1
        minutes += _duration(sc) / 60.0
        if not sc.get("media_transcribe"):
            pending += 1
    return {"av_assets_probed": assets, "av_assets_pending": pending,
            "audio_minutes": round(minutes, 2),
            "estimated_cost_usd": round(minutes * cfg.cost_per_audio_minute, 4)}
