"""media_stage3 engine — video keyframes + image vision + Content Safety (HANDOFF-media-track §3/§4).

VIDEO: sample DURATION-ADAPTIVE keyframes with a per-video CAP (``max(base, ceil(duration/max_frames))``)
so long videos don't explode into millions of frames; each kept keyframe gets a 1024-d Azure AI Vision
vector (``.emb.json``, keyed by ``content_hash = sha256(webp)``) and an ``image_occurrences``-shaped row
(``artifact_kind="keyframe"``, ``frame_index``, ``timestamp_ms``, ``page_number=-1``). Content Safety is
scanned DENSELY (cheap) and embedding kept SPARSELY (the cap) — the safety/retrieval decoupling.

STANDALONE IMAGE (§4 decision): the recommended outcome of option A — **a 1024-d visual vector + safety
verdict + an occurrence, no text chunks** — produced natively here (no img2pdf/PyMuPDF round-trip).
``artifact_kind="image"``, ``frame_index=0``.

Redaction is enforced defensively at this stage too: a keyframe/image scored ``block`` is NOT embedded
and its blob is deleted (placeholder), so high-risk vectors/bytes are never even staged — strictly more
conservative than relying solely on the Stage-8 §4a gate.
"""

from __future__ import annotations

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor

from . import FRAMES_TOOL_NAME, FRAMES_TOOL_VERSION
from . import common
from . import ffmpeg
from .config import FramesConfig
from .vision import VisionCallError

_MANIFEST_NAME = "media-frames-run-manifest.json"
_VISUAL_KINDS = ("video", "image")


def _gate(asset: str, *, force: bool):
    sc = common.load_json(common.sidecar_path(asset))
    if sc is None:
        return None, "no_sidecar"
    if sc.get("status") != "ok" or not sc.get("media_probe"):
        return None, "no_probe"
    if (sc.get("document") or {}).get("source_kind") not in _VISUAL_KINDS:
        return None, "not_visual"
    if sc.get("media_frames") and not force:
        return sc, "skipped_done"
    return sc, None


def _scan_and_embed(image_bytes: bytes, *, vision_client, safety_client, cfg: FramesConfig):
    """Scan safety FIRST, then embed only if not high-risk (block). Returns
    (verdict, embed_result_or_None, redacted: bool)."""
    verdict = safety_client.analyze_image(image_bytes) if safety_client else None
    if verdict is not None and verdict.flag == "block":
        return verdict, None, True  # never embed/keep high-risk imagery
    content_hash = common.sha256_bytes(image_bytes)
    embed = vision_client.embed_image(image_bytes, content_hash) if vision_client else None
    return verdict, embed, False


def _even_timestamps(duration: float, n: int) -> list[float]:
    """``n`` evenly-spaced representative timestamps (frame centers). Fallback for a video with no
    detected scene changes — a fully static clip still gets a couple of representative keyframes."""
    n = max(1, n)
    if duration <= 0:
        return [0.0]
    return [round((i + 0.5) / n * duration, 3) for i in range(n)]


def _embed_timestamps(asset, duration, cfg, ff):
    """Choose EMBEDDED-keyframe timestamps. ``scene`` mode samples on visual change (static surveillance
    footage → few/zero → fall back to a tiny representative set), capped at ``keyframe_max_frames`` and
    floored at ``keyframe_min_frames``. ``interval`` mode is the legacy duration-adaptive time grid.
    Returns (timestamps, interval_or_None, mode)."""
    if cfg.keyframe_mode == "scene":
        scene = ffmpeg.detect_scene_changes(asset, threshold=cfg.scene_threshold,
                                            downscale_px=cfg.scene_downscale_px, ffmpeg_bin=ff)
        if not scene:
            scene = _even_timestamps(duration, cfg.keyframe_min_frames)
        elif len(scene) < cfg.keyframe_min_frames:
            scene = sorted(set(scene) | set(_even_timestamps(duration, cfg.keyframe_min_frames)))
        return ffmpeg.subsample_evenly(scene, cfg.keyframe_max_frames), None, "scene"
    interval = ffmpeg.adaptive_interval(duration, base_seconds=cfg.keyframe_base_seconds,
                                        max_frames=cfg.keyframe_max_frames)
    grid = ffmpeg.subsample_evenly(ffmpeg.floor_grid(duration, interval), cfg.keyframe_max_frames)
    return grid, interval, "interval"


def _process_video(asset, kind, root, sc, cfg, generated_at, vision_client, safety_client):
    rel = common.rel_path(asset, root)
    duration = float((sc.get("media_metadata") or {}).get("duration_seconds") or 0.0)
    out_dir = common.asset_dir(asset)
    frames_dir = os.path.join(out_dir, "frames")
    emb_dir = os.path.join(out_dir, "embeddings")
    ff = cfg.ffmpeg_bin or ffmpeg.DEFAULT_FFMPEG

    try:
        embed_ts, interval, mode = _embed_timestamps(asset, duration, cfg, ff)
        frames = ffmpeg.extract_frames_at(asset, frames_dir, embed_ts, max_px=cfg.frame_max_px,
                                          quality=cfg.frame_quality, ffmpeg_bin=ff)
    except ffmpeg.FfmpegError as exc:
        return _record_error(asset, sc, rel, kind, generated_at, exc.code, str(exc))

    keyframes, embedded, redacted = _embed_keyframes(frames, emb_dir, out_dir, vision_client,
                                                     safety_client, cfg)
    # safety backstop: scene mode → coarse floor grid (cost scales with content, not length);
    # interval mode → the legacy dense grid. The embedded (scene-change) frames are already scanned.
    floor = cfg.safety_floor_seconds if mode == "scene" else cfg.safety_scan_interval_seconds
    dense = _safety_floor_scan(asset, out_dir, duration, floor, safety_client, cfg, ff)

    poster_rel = _make_poster(asset, out_dir, duration, cfg)
    rollup = _safety_rollup(keyframes, dense)

    sc.setdefault("media_metadata", {})
    sc["media_metadata"].update({
        "keyframe_count": len(keyframes),
        "keyframe_interval_seconds": round(interval, 3) if interval else None,
        "poster_blob_path": poster_rel,
    })
    sc.pop("media_frames_error", None)
    sc["media_frames"] = _frames_block(generated_at, cfg, kind, mode=mode, interval=interval,
                                       keyframes=keyframes, dense=dense, rollup=rollup,
                                       poster_rel=poster_rel)
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("framed", rel, kind, keyframes=len(keyframes), embedded=embedded,
                   redacted=redacted, safety_block=rollup["block"], safety_review=rollup["review"])


def _process_image(asset, kind, root, sc, cfg, generated_at, vision_client, safety_client):
    rel = common.rel_path(asset, root)
    out_dir = common.asset_dir(asset)
    emb_dir = os.path.join(out_dir, "embeddings")
    try:
        with open(asset, "rb") as fh:
            image_bytes = fh.read()
    except OSError as exc:
        return _record_error(asset, sc, rel, kind, generated_at, "read_error", str(exc))

    try:
        verdict, embed, was_redacted = _scan_and_embed(image_bytes, vision_client=vision_client,
                                                        safety_client=safety_client, cfg=cfg)
    except VisionCallError as exc:
        return _record_error(asset, sc, rel, kind, generated_at, exc.code, str(exc))

    content_hash = common.sha256_bytes(image_bytes)
    emb_rel = None
    if embed is not None:
        emb_rel = os.path.join(os.path.basename(out_dir), "embeddings",
                               f"{content_hash}.emb.json").replace(os.sep, "/")
        common.dump_json(embed.emb_json(), os.path.join(emb_dir, f"{content_hash}.emb.json"))
    occ = {"artifact_kind": "image", "frame_index": 0, "timestamp_ms": None, "page_number": -1,
           "content_hash": content_hash, "embedded": embed is not None, "emb_path": emb_rel,
           "redacted": was_redacted, "safety": verdict.to_dict() if verdict else None}
    dense = {"scanned": 1 if verdict else 0,
             "block": int(bool(verdict and verdict.flag == "block")),
             "review": int(bool(verdict and verdict.flag == "review")),
             "max_severity": verdict.max_severity if verdict else 0}
    rollup = _safety_rollup([occ], dense)

    sc.pop("media_frames_error", None)
    sc["media_frames"] = _frames_block(generated_at, cfg, kind, mode="image", interval=None,
                                       keyframes=[occ], dense={"scanned": 0}, rollup=rollup,
                                       poster_rel=None)
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("framed", rel, kind, keyframes=1, embedded=int(embed is not None),
                   redacted=int(was_redacted), safety_block=rollup["block"], safety_review=rollup["review"])


def _embed_keyframes(frames, emb_dir, out_dir, vision_client, safety_client, cfg):
    """Scan+embed each kept keyframe (bounded pool). ``frames`` = ``[(path, timestamp_s), ...]``; the
    timestamp comes from the actual sampled position (scene-change time), so occurrences cite the real
    moment. Deletes blocked frames' blobs (redaction)."""
    def one(item):
        fidx, (path, ts) = item
        try:
            with open(path, "rb") as fh:
                data = fh.read()
        except OSError:
            return None
        verdict, embed, redacted = _scan_and_embed(data, vision_client=vision_client,
                                                    safety_client=safety_client, cfg=cfg)
        content_hash = common.sha256_bytes(data)
        emb_rel = None
        if redacted:
            try:
                os.remove(path)  # never keep high-risk bytes on disk
            except OSError:
                pass
        elif embed is not None:
            emb_rel = os.path.join(os.path.basename(out_dir), "embeddings",
                                   f"{content_hash}.emb.json").replace(os.sep, "/")
            common.dump_json(embed.emb_json(), os.path.join(emb_dir, f"{content_hash}.emb.json"))
        frame_rel = None if redacted else os.path.join(
            os.path.basename(out_dir), "frames", os.path.basename(path)).replace(os.sep, "/")
        return {"artifact_kind": "keyframe", "frame_index": fidx,
                "timestamp_ms": int(round(ts * 1000)), "page_number": -1,
                "content_hash": content_hash, "embedded": embed is not None and not redacted,
                "emb_path": emb_rel, "frame_path": frame_rel, "redacted": redacted,
                "safety": verdict.to_dict() if verdict else None}

    items = list(enumerate(frames))
    workers = max(1, min(cfg.frame_concurrency, len(items) or 1))
    if workers <= 1:
        results = [one(it) for it in items]
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, items))
    keyframes = [r for r in results if r is not None]
    embedded = sum(1 for r in keyframes if r["embedded"])
    redacted = sum(1 for r in keyframes if r["redacted"])
    return keyframes, embedded, redacted


def _safety_floor_scan(asset, out_dir, duration, interval_seconds, safety_client, cfg, ff):
    """Scan a COARSE floor grid of frames for Content Safety only (discarded after) — the backstop between
    embedded (scene-change) frames so an objectionable frame can't slip through a long static stretch. In
    scene mode this is coarse (``safety_floor_seconds``), so cost scales with content, not video length."""
    base = {"scanned": 0, "block": 0, "review": 0, "max_severity": 0}
    if safety_client is None or interval_seconds <= 0:
        return base
    scan_dir = os.path.join(out_dir, "_safety_scan")
    try:
        paths = ffmpeg.extract_frames(asset, scan_dir, interval_seconds=interval_seconds,
                                      max_px=cfg.frame_max_px, quality=cfg.frame_quality,
                                      prefix="scan", ffmpeg_bin=ff)
    except ffmpeg.FfmpegError:
        return base
    paths = paths[:cfg.safety_max_frames]
    for p in paths:
        try:
            with open(p, "rb") as fh:
                verdict = safety_client.analyze_image(fh.read())
        except (OSError, VisionCallError):
            continue
        base["scanned"] += 1
        base["max_severity"] = max(base["max_severity"], verdict.max_severity)
        if verdict.flag == "block":
            base["block"] += 1
        elif verdict.flag == "review":
            base["review"] += 1
    shutil.rmtree(scan_dir, ignore_errors=True)  # dense safety frames are never kept
    return base


def _make_poster(asset, out_dir, duration, cfg):
    dest = os.path.join(out_dir, "poster.webp")
    try:
        ffmpeg.extract_poster(asset, dest, at_seconds=(duration / 2.0 if duration > 0 else 0.0),
                              max_px=cfg.frame_max_px, ffmpeg_bin=cfg.ffmpeg_bin or ffmpeg.DEFAULT_FFMPEG)
    except ffmpeg.FfmpegError:
        return None
    return os.path.join(os.path.basename(out_dir), "poster.webp").replace(os.sep, "/")


def _safety_rollup(keyframes: list[dict], dense: dict) -> dict:
    block = sum(1 for k in keyframes if (k.get("safety") or {}).get("flag") == "block") + dense["block"]
    review = sum(1 for k in keyframes if (k.get("safety") or {}).get("flag") == "review") + dense["review"]
    max_sev = max([dense["max_severity"]] + [(k.get("safety") or {}).get("max_severity", 0)
                                             for k in keyframes] or [0])
    return {"block": block, "review": review, "max_severity": max_sev,
            "flag": "block" if block else ("review" if review else "ok")}


def _frames_block(generated_at, cfg, kind, *, mode, interval, keyframes, dense, rollup,
                  poster_rel) -> dict:
    return {
        "tool": FRAMES_TOOL_NAME, "tool_version": FRAMES_TOOL_VERSION,
        "generated_at_utc": generated_at, "source_kind": kind, "config": cfg.echo(),
        "vision_model": cfg.vision_model, "vision_dim": cfg.vision_dim,
        "keyframe_mode": mode,
        "keyframe_interval_seconds": round(interval, 3) if interval else None,
        "keyframe_count": len(keyframes),
        "embedded_count": sum(1 for k in keyframes if k.get("embedded")),
        "redacted_count": sum(1 for k in keyframes if k.get("redacted")),
        "safety_floor_scanned": dense.get("scanned", 0),
        "safety_rollup": rollup, "poster_blob_path": poster_rel,
        "image_occurrences": keyframes,  # S8 writes one image_occurrences row per entry
    }


def _record_error(asset, sc, rel, kind, generated_at, code, message) -> dict:
    sc["media_frames_error"] = {"code": code, "message": message[:300], "generated_at_utc": generated_at}
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("error", rel, kind, error_code=code, message=message[:300])


def _result(action, rel, kind, *, error_code=None, message=None, keyframes=0, embedded=0,
            redacted=0, safety_block=0, safety_review=0) -> dict:
    return {"action": action, "rel": rel, "kind": kind, "error_code": error_code, "message": message,
            "keyframes": keyframes, "embedded": embedded, "redacted": redacted,
            "safety_block": safety_block, "safety_review": safety_review}


def process_asset(asset, kind, root, cfg, generated_at, *, vision_client, safety_client, force):
    rel = common.rel_path(asset, root)
    sc, skip = _gate(asset, force=force)
    if skip is not None and sc is None:
        return _result(skip, rel, kind)
    if skip == "skipped_done":
        return _result(skip, rel, kind)
    if kind == "video":
        return _process_video(asset, kind, root, sc, cfg, generated_at, vision_client, safety_client)
    return _process_image(asset, kind, root, sc, cfg, generated_at, vision_client, safety_client)


def run(root: str, cfg: FramesConfig, generated_at: str, *, vision_client, safety_client,
        provenance: dict, force: bool, workers: int | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    assets = [(p, k) for p, k in common.find_media(root) if k in _VISUAL_KINDS]
    total = len(assets)
    workers = workers or cfg.concurrency

    actions = {"framed": 0, "skipped_done": 0, "no_sidecar": 0, "no_probe": 0, "not_visual": 0, "error": 0}
    agg = {"keyframes": 0, "embedded": 0, "redacted": 0, "safety_block": 0, "safety_review": 0}
    errors: list[dict] = []

    def consume(r: dict) -> None:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "framed":
            for k in agg:
                agg[k] += r[k]
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})

    def worker(item):
        asset, kind = item
        return process_asset(asset, kind, root, cfg, generated_at, vision_client=vision_client,
                             safety_client=safety_client, force=force)

    common.drive(assets, worker, workers=workers, on_result=consume, total=total,
                 label="visual assets", started=started)

    manifest = {
        "tool": FRAMES_TOOL_NAME, "tool_version": FRAMES_TOOL_VERSION,
        "vision_model": cfg.vision_model, "vision_dim": cfg.vision_dim,
        "vision": provenance.get("vision"), "content_safety": provenance.get("content_safety"),
        "generated_at_utc": generated_at, "root": root,
        "assets_seen": total,
        "assets_framed": actions["framed"],
        "assets_skipped_already_done": actions["skipped_done"],
        "assets_no_sidecar": actions["no_sidecar"],
        "assets_no_probe": actions["no_probe"],
        "assets_errored": actions["error"],
        "keyframes": agg["keyframes"], "keyframes_embedded": agg["embedded"],
        "keyframes_redacted": agg["redacted"],
        "safety_block": agg["safety_block"], "safety_review": agg["safety_review"],
        "errors": sorted(errors, key=lambda e: e["file"]),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    common.dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
