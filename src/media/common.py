"""Shared media plumbing: source-kind detection, the media walk, sidecar/blob paths, hashing, atomic
JSON/byte writes, time helpers, and the bounded-window thread driver used by every media stage.

Deliberately **stdlib-only** (no PyMuPDF, no ``requests``) so the media engine imports and its
deterministic logic runs even on a host without the PDF stack or network libs. The PDF track's
``stage1.walk.dump_json`` is intentionally NOT imported here (it pulls ``fitz``); we re-implement the
same deterministic atomic write so the media track stands alone.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

# --- source-kind detection (mirrors svkb models.SOURCE_KINDS subset) --------------------------------
# Extension → source_kind. Lowercased, no leading dot. The media walk targets these and EXCLUDES the
# PDF ``IMAGES`` tree (those rasters belong to the PDF track / Stage 4), so the standalone images we see
# here are the genuine non-PDF assets (NATIVES etc.).
AUDIO_EXTS = frozenset({"m4a", "wav", "mp3", "aac", "flac", "ogg", "oga", "wma", "aiff", "aif", "opus"})
VIDEO_EXTS = frozenset({"mp4", "avi", "mov", "mkv", "wmv", "flv", "webm", "mpg", "mpeg", "m4v",
                        "3gp", "3g2", "ts", "mts", "m2ts", "vob", "ogv"})
IMAGE_EXTS = frozenset({"jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "heif"})

# Output dirs we create next to an asset (``<basename>.media/``) — pruned from the walk so a processed
# corpus never makes os.walk descend into thousands of frame dirs (the flat-DS-09 slow-walk lesson).
_OUTPUT_DIR_SUFFIX = ".media"
_PRUNE_DIR_NAMES = {"IMAGES"}  # the PDF track tree (case-insensitive); see HANDOFF-media-track §0a/§4


def detect_kind(filename: str) -> str | None:
    """Return ``"audio"|"video"|"image"`` for a media filename, else ``None`` (not a media asset)."""
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in IMAGE_EXTS:
        return "image"
    return None


def find_media(root: str) -> list[tuple[str, str]]:
    """Recursively find media assets as ``(path, source_kind)``, sorted for determinism.

    Prunes the PDF ``IMAGES`` tree and our own ``*.media`` output dirs so the walk is fast and never
    re-ingests rendered frames/posters as if they were source images.
    """
    out: list[tuple[str, str]] = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs
                   if d.upper() not in _PRUNE_DIR_NAMES and not d.endswith(_OUTPUT_DIR_SUFFIX)]
        for fn in files:
            kind = detect_kind(fn)
            if kind is not None:
                out.append((os.path.join(dirpath, fn), kind))
    return sorted(out)


# --- per-asset paths (collision-free: media stems can collide across extensions, so keep the ext) ----
def sidecar_path(asset: str) -> str:
    """``clip.mp4`` → ``clip.mp4.media.json`` (distinct from the PDF track's ``<stem>.json``)."""
    return asset + ".media.json"


def chunks_path(asset: str) -> str:
    """``clip.mp4`` → ``clip.mp4.chunks.json`` (the shared chunker output S7/S8 consume)."""
    return asset + ".chunks.json"


def asset_dir(asset: str) -> str:
    """``clip.mp4`` → ``clip.mp4.media`` (holds transcript.md/.vtt, frames/, poster.webp, *.emb.json)."""
    return asset + _OUTPUT_DIR_SUFFIX


def rel_path(path: str, root: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


# --- hashing ----------------------------------------------------------------------------------------
def sha256_file(path: str, _bufsize: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_bufsize), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- atomic, deterministic writes (same contract as stage1.walk.dump_json, sans the fitz import) -----
def dump_json(obj: dict, path: str) -> None:
    """Deterministic write: fixed indent, UTF-8, construction key order, trailing newline; atomic."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)


def write_bytes(data: bytes, path: str) -> None:
    """Atomic blob write (frames, posters, transcripts) — a crash never leaves a half-written blob."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def write_text(text: str, path: str) -> None:
    write_bytes(text.encode("utf-8"), path)


def load_json(path: str) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


# --- time / misc helpers ----------------------------------------------------------------------------
def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 2)


def hms(seconds: float) -> str:
    """Seconds → ``hh:mm:ss`` (transcript headings / VTT use this; deterministic, floor)."""
    s = max(0, int(seconds))
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def vtt_ts(seconds: float) -> str:
    """Seconds → ``hh:mm:ss.mmm`` for WebVTT cue boundaries."""
    ms = max(0, int(round(seconds * 1000)))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"


# --- bounded-window thread driver (shared by all media stages) --------------------------------------
# Media work is I/O-bound (subprocess ffmpeg/ffprobe + network), so a THREAD pool with a bounded
# in-flight window keeps workers fed while memory stays flat at hundreds of thousands of assets — the
# same streaming pattern as stage2.walk, adapted to threads (no pickling; share one Azure client).
def drive(items: list, worker, *, workers: int, on_result, total: int | None = None,
          heartbeat_seconds: int = 60, label: str = "assets",
          started: float | None = None) -> None:
    """Run ``worker(item)`` across a bounded thread-pool window, calling ``on_result(result)`` as each
    completes (order-independent), with a periodic heartbeat. ``worker`` MUST be fault-isolated (return a
    result dict, never raise) — a raised exception is surfaced as a generic ``worker_error`` result via
    ``on_result`` so one bad asset never aborts the run."""
    total = len(items) if total is None else total
    started = time.monotonic() if started is None else started
    last_beat = started
    done = 0
    it = iter(items)

    def _emit(r: dict) -> None:
        nonlocal done, last_beat
        on_result(r)
        done += 1
        now = time.monotonic()
        if heartbeat_seconds and now - last_beat >= heartbeat_seconds:
            el = now - started
            rate = done / el if el else 0.0
            eta = (total - done) / rate / 60.0 if rate else 0.0
            print(f"[progress] {done}/{total} {label} ({rate:.1f}/s, eta {eta:.0f}m)", flush=True)
            last_beat = now

    if workers <= 1 or total <= 1:
        for item in it:
            _emit(_safe(worker, item))
        return

    max_inflight = max(workers * 2, workers + 1)
    import itertools
    with ThreadPoolExecutor(max_workers=workers) as ex:
        inflight = {ex.submit(_safe, worker, item) for item in itertools.islice(it, max_inflight)}
        while inflight:
            completed, inflight = wait(inflight, return_when=FIRST_COMPLETED)
            for fut in completed:
                _emit(fut.result())
                nxt = next(it, None)
                if nxt is not None:
                    inflight.add(ex.submit(_safe, worker, nxt))


def _safe(worker, item):
    """Run a worker, converting any unexpected raise into a generic error result so the pool survives."""
    try:
        return worker(item)
    except Exception as exc:  # the worker should fault-isolate itself; this is the backstop
        path = item[0] if isinstance(item, tuple) else item
        kind = item[1] if isinstance(item, tuple) and len(item) > 1 else None
        return {"action": "error", "rel": str(path), "kind": kind, "error_code": "worker_error",
                "message": f"{type(exc).__name__}: {exc}"}
