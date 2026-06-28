"""Thin ``ffprobe`` / ``ffmpeg`` subprocess wrappers: probe metadata, audio segmentation, video
keyframes, and posters (HANDOFF-media-track §2/§3/§5).

ffmpeg/ffprobe are a HARD prerequisite for the media track and may be absent (confirmed absent on this
host). Every call raises :class:`FfmpegError` with a typed ``code`` — ``no_ffprobe`` / ``no_ffmpeg``
when the binary is missing, ``probe_failed`` / ``ffmpeg_failed`` on a non-zero exit — so a stage records
an error sidecar and continues (fault isolation) rather than crashing the run. Binary paths come from
``FFMPEG_BIN`` / ``FFPROBE_BIN`` (default ``ffmpeg`` / ``ffprobe`` on PATH).
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess

DEFAULT_FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
DEFAULT_FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")


class FfmpegError(RuntimeError):
    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def have(bin_path: str) -> bool:
    return shutil.which(bin_path) is not None or os.path.isfile(bin_path)


def _run(argv: list[str], *, code: str, timeout: float, capture: bool = True) -> bytes:
    try:
        proc = subprocess.run(argv, capture_output=capture, timeout=timeout)
    except FileNotFoundError as exc:
        miss = "no_ffprobe" if "probe" in argv[0] else "no_ffmpeg"
        raise FfmpegError(f"{argv[0]} not found: {exc}", miss) from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"{argv[0]} timed out after {timeout}s", "timeout") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or b"")[-500:].decode("utf-8", "replace") if capture else ""
        raise FfmpegError(f"{argv[0]} exit {proc.returncode}: {tail}", code)
    return proc.stdout if capture else b""


# --- probe ------------------------------------------------------------------------------------------
def probe_media(path: str, *, ffprobe_bin: str = DEFAULT_FFPROBE, timeout: float = 120.0) -> dict:
    """Run ``ffprobe -show_format -show_streams`` (JSON). Returns the raw parsed object; callers derive
    typed fields via :func:`typed_metadata`. Raises FfmpegError on a missing binary / bad file."""
    out = _run([ffprobe_bin, "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", path],
               code="probe_failed", timeout=timeout)
    try:
        return json.loads(out or b"{}")
    except json.JSONDecodeError as exc:
        raise FfmpegError(f"ffprobe returned non-JSON: {exc}", "probe_failed") from exc


def typed_metadata(probe: dict) -> dict:
    """Derive the typed ``media_metadata`` fields from a raw ffprobe object (best-effort; missing values
    are ``None``). The full raw object is kept separately as the JSONB bag."""
    fmt = probe.get("format") or {}
    streams = probe.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    video = next((s for s in streams if s.get("codec_type") == "video"), None)

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    duration = _f(fmt.get("duration"))
    if duration is None and video:
        duration = _f(video.get("duration"))
    if duration is None and audio:
        duration = _f(audio.get("duration"))

    md = {
        "duration_seconds": duration,
        "bitrate": _i(fmt.get("bit_rate")),
        "format_name": fmt.get("format_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
        "audio_channels": _i(audio.get("channels")) if audio else None,
        "sample_rate": _i(audio.get("sample_rate")) if audio else None,
        "video_codec": video.get("codec_name") if video else None,
        "video_width": _i(video.get("width")) if video else None,
        "video_height": _i(video.get("height")) if video else None,
        "frame_rate": _fps(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None,
        "has_audio": audio is not None,
        "has_video": video is not None,
    }
    return md


def _fps(rate: str | None) -> float | None:
    """Parse ffprobe's ``"30000/1001"`` rational frame-rate to a float."""
    if not rate or rate in ("0/0", "N/A"):
        return None
    try:
        if "/" in rate:
            num, den = rate.split("/", 1)
            den = float(den)
            return round(float(num) / den, 4) if den else None
        return float(rate)
    except (TypeError, ValueError):
        return None


# --- audio segmentation (for transcription) ---------------------------------------------------------
def segment_audio(path: str, out_dir: str, *, window_seconds: int, sample_rate: int = 16000,
                  channels: int = 1, bitrate_kbps: int = 64, ffmpeg_bin: str = DEFAULT_FFMPEG,
                  timeout: float = 3600.0) -> list[str]:
    """Down-mix to mono/16 kHz mp3 and split into ``window_seconds`` segments (``-vn`` drops video so the
    same path serves audio AND a video's audio track). Returns the ordered list of segment file paths."""
    os.makedirs(out_dir, exist_ok=True)
    pattern = os.path.join(out_dir, "segment_%04d.mp3")
    _run([ffmpeg_bin, "-y", "-i", path, "-vn", "-ac", str(channels), "-ar", str(sample_rate),
          "-b:a", f"{bitrate_kbps}k", "-f", "segment", "-segment_time", str(window_seconds),
          pattern], code="ffmpeg_failed", timeout=timeout, capture=True)
    segs = sorted(f for f in os.listdir(out_dir) if f.startswith("segment_") and f.endswith(".mp3"))
    return [os.path.join(out_dir, f) for f in segs]


# --- video keyframes + poster -----------------------------------------------------------------------
def adaptive_interval(duration_seconds: float, *, base_seconds: float, max_frames: int) -> float:
    """Duration-adaptive keyframe interval with a per-video cap (HANDOFF §3): ``max(base,
    ceil(duration/max_frames))`` — long videos sample coarser so a fixed 1/10s doesn't explode into
    millions of frames at this corpus's scale."""
    if duration_seconds <= 0 or max_frames <= 0:
        return max(1.0, float(base_seconds))
    return max(float(base_seconds), float(math.ceil(duration_seconds / max_frames)))


def extract_frames(path: str, out_dir: str, *, interval_seconds: float, max_px: int = 480,
                   quality: int = 80, prefix: str = "frame", ffmpeg_bin: str = DEFAULT_FFMPEG,
                   timeout: float = 3600.0) -> list[str]:
    """Sample one WebP frame every ``interval_seconds`` (``fps=1/interval``), longest side ≤ ``max_px``.
    Returns the ordered list of frame file paths (``frame_0001.webp`` …)."""
    os.makedirs(out_dir, exist_ok=True)
    fps = 1.0 / interval_seconds if interval_seconds > 0 else 1.0
    vf = f"fps={fps:.6f},scale='min({max_px},iw)':'-2'"
    pattern = os.path.join(out_dir, f"{prefix}_%04d.webp")
    _run([ffmpeg_bin, "-y", "-i", path, "-vf", vf, "-vsync", "vfr", "-q:v", str(quality), pattern],
         code="ffmpeg_failed", timeout=timeout, capture=True)
    frames = sorted(f for f in os.listdir(out_dir) if f.startswith(f"{prefix}_") and f.endswith(".webp"))
    return [os.path.join(out_dir, f) for f in frames]


def extract_poster(path: str, dest: str, *, at_seconds: float, max_px: int = 480,
                   ffmpeg_bin: str = DEFAULT_FFMPEG, timeout: float = 300.0) -> None:
    """Grab a single WebP poster frame at ``at_seconds`` (≈50% duration) → ``dest``."""
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    _run([ffmpeg_bin, "-y", "-ss", f"{max(0.0, at_seconds):.3f}", "-i", path, "-frames:v", "1",
          "-vf", f"scale='min({max_px},iw)':'-2'", dest],
         code="ffmpeg_failed", timeout=timeout, capture=True)


def extract_frames_at(path: str, out_dir: str, timestamps: list[float], *, max_px: int = 480,
                      quality: int = 80, prefix: str = "frame", ffmpeg_bin: str = DEFAULT_FFMPEG,
                      timeout: float = 300.0) -> list[tuple[str, float]]:
    """Extract one WebP frame at each timestamp (input-seek per frame, fast on keyframes). Returns
    ``[(path, timestamp_seconds), ...]`` in timestamp order. Used for scene-change keyframes, where the
    frame set is content-driven and already bounded by the per-video cap (so the seek count is small)."""
    os.makedirs(out_dir, exist_ok=True)
    out: list[tuple[str, float]] = []
    for i, ts in enumerate(timestamps, start=1):
        dest = os.path.join(out_dir, f"{prefix}_{i:04d}.webp")
        try:
            _run([ffmpeg_bin, "-y", "-ss", f"{max(0.0, ts):.3f}", "-i", path, "-frames:v", "1",
                  "-vf", f"scale='min({max_px},iw)':'-2'", "-q:v", str(quality), dest],
                 code="ffmpeg_failed", timeout=timeout, capture=True)
        except FfmpegError:
            continue  # a single bad seek shouldn't drop the whole video's keyframes
        if os.path.exists(dest):
            out.append((dest, float(ts)))
    return out


# --- content-aware detection (scene changes for keyframes; silence for transcription gating) ---------
_PTS_RE = re.compile(r"pts_time:([0-9]+\.?[0-9]*)")
_SIL_START_RE = re.compile(r"silence_start:\s*(-?[0-9]+\.?[0-9]*)")
_SIL_END_RE = re.compile(r"silence_end:\s*(-?[0-9]+\.?[0-9]*)")


def _run_detect(argv: list[str], *, timeout: float) -> str:
    """Run a detection pass (``-f null`` or metadata print) and return combined stdout+stderr text.
    Detection filters log to stderr (silencedetect) or stdout (metadata print); we read both. A missing
    binary raises; a non-zero exit returns whatever was captured (detection is best-effort)."""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FfmpegError(f"{argv[0]} not found: {exc}", "no_ffmpeg") from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegError(f"{argv[0]} timed out after {timeout}s", "timeout") from exc
    out = (proc.stdout or b"").decode("utf-8", "replace")
    err = (proc.stderr or b"").decode("utf-8", "replace")
    return out + "\n" + err


def detect_scene_changes(path: str, *, threshold: float = 0.4, downscale_px: int = 320,
                         ffmpeg_bin: str = DEFAULT_FFMPEG, timeout: float = 3600.0) -> list[float]:
    """Return sorted timestamps (seconds) where the visual scene changes by more than ``threshold``
    (0..1). Decodes the video once at ``downscale_px`` for speed. Static surveillance footage yields few
    or zero timestamps — the whole point: keyframes track *content change*, not wall-clock."""
    vf = f"scale={downscale_px}:-2,select='gt(scene,{threshold})',metadata=print:file=-"
    text = _run_detect([ffmpeg_bin, "-i", path, "-an", "-vf", vf, "-f", "null", "-"], timeout=timeout)
    seen: set[float] = set()
    out: list[float] = []
    for m in _PTS_RE.finditer(text):
        ts = round(float(m.group(1)), 3)
        if ts not in seen:
            seen.add(ts)
            out.append(ts)
    return sorted(out)


def detect_silence(path: str, *, noise_db: float = -30.0, min_seconds: float = 2.0,
                   ffmpeg_bin: str = DEFAULT_FFMPEG, timeout: float = 3600.0) -> list[tuple[float, float | None]]:
    """Return silent intervals ``[(start, end|None), ...]`` via ``silencedetect`` (end=None when silence
    runs to EOF). Audio quieter than ``noise_db`` for ≥ ``min_seconds`` counts as silence."""
    af = f"silencedetect=noise={noise_db}dB:d={min_seconds}"
    text = _run_detect([ffmpeg_bin, "-i", path, "-af", af, "-vn", "-f", "null", "-"], timeout=timeout)
    starts = [float(m.group(1)) for m in _SIL_START_RE.finditer(text)]
    ends = [float(m.group(1)) for m in _SIL_END_RE.finditer(text)]
    intervals: list[tuple[float, float | None]] = []
    for i, s in enumerate(starts):
        intervals.append((max(0.0, s), ends[i] if i < len(ends) else None))
    return intervals


# --- pure helpers (no subprocess; unit-testable) ----------------------------------------------------
def non_silent_intervals(silent: list[tuple[float, float | None]], duration: float) -> list[tuple[float, float]]:
    """Complement of the silent intervals within ``[0, duration]`` → the spans that contain sound."""
    if duration <= 0:
        return []
    spans: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in sorted(silent, key=lambda x: x[0]):
        e = duration if e is None else min(e, duration)
        s = max(0.0, min(s, duration))
        if s > cur:
            spans.append((cur, s))
        cur = max(cur, e)
    if cur < duration:
        spans.append((cur, duration))
    return spans


def interval_has_sound(start: float, end: float, nonsilent: list[tuple[float, float]]) -> bool:
    """True if window ``[start, end)`` overlaps any non-silent span."""
    for a, b in nonsilent:
        if start < b and end > a:
            return True
    return False


def floor_grid(duration: float, interval: float) -> list[float]:
    """Coarse evenly-spaced timestamps every ``interval`` seconds over ``[0, duration]`` (the safety
    backstop grid / fixed-interval sampler)."""
    if duration <= 0 or interval <= 0:
        return [0.0]
    n = int(duration // interval) + 1
    return [round(i * interval, 3) for i in range(n) if i * interval < duration]


def subsample_evenly(items: list, max_n: int) -> list:
    """Evenly thin ``items`` down to at most ``max_n`` (keeps first/last-ish), preserving order."""
    if max_n <= 0 or len(items) <= max_n:
        return list(items)
    step = len(items) / max_n
    return [items[int(i * step)] for i in range(max_n)]
