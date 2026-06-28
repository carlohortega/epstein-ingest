"""media_stage4 engine — doc-level caption + description (HANDOFF-media-track §2/§4).

Generates the media analog of the PDF ``document_summary`` / Stage-4 image caption, via gpt-5-mini
(reused Stage-3 token-metered client):
  * **video**  → {caption, description} from the transcript + a few safety-cleared, embedded keyframes;
  * **audio**  → {caption, description} from the transcript;
  * **image**  → Stage-4-identical {caption, description, content_class, has_visual_content, safety}.

Keyframes are inputs here, not assets — they remain `image_occurrences` produced by media_stage3 (visual
search), and a small sample feeds the video caption. A standalone image scored high-risk (`block`) by
media_stage3 is NOT sent to the caption model (no objectionable bytes to the LLM); it is recorded
redacted with no caption. Captures prompt/completion/cached tokens + USD. Idempotent/resumable;
appends a ``media_caption`` block; a failure records ``media_caption_error`` (retried on resume).
"""

from __future__ import annotations

import base64
import os
import time

from . import CAPTION_TOOL_NAME, CAPTION_TOOL_VERSION
from . import common
from .config import CaptionConfig
from .caption import MediaCaptionClient, CaptionCallError, usd

_MANIFEST_NAME = "media-caption-run-manifest.json"


def _gate(asset: str, kind: str, *, force: bool):
    sc = common.load_json(common.sidecar_path(asset))
    if sc is None:
        return None, "no_sidecar"
    if sc.get("status") != "ok" or not sc.get("media_probe"):
        return None, "no_probe"
    if kind in ("audio", "video") and not sc.get("media_transcribe"):
        return None, "no_transcribe"
    if kind in ("video", "image") and not sc.get("media_frames"):
        return None, "no_frames"
    if sc.get("media_caption") and not force:
        return sc, "skipped_done"
    return sc, None


def _transcript_text(sc: dict) -> str:
    windows = (sc.get("media_transcribe") or {}).get("windows") or []
    return "\n".join((w.get("text") or "").strip() for w in windows if (w.get("text") or "").strip())


def _sample_keyframes(asset: str, sc: dict, max_n: int) -> list[str]:
    """Evenly sample up to ``max_n`` safety-cleared, embedded keyframes (base64 webp) for the vision call."""
    occ = [o for o in ((sc.get("media_frames") or {}).get("image_occurrences") or [])
           if o.get("embedded") and not o.get("redacted") and o.get("frame_path")]
    if not occ:
        return []
    if len(occ) > max_n:
        step = len(occ) / max_n
        occ = [occ[int(i * step)] for i in range(max_n)]
    out: list[str] = []
    base = os.path.dirname(asset)
    for o in occ:
        path = os.path.join(base, o["frame_path"].replace("/", os.sep))
        try:
            with open(path, "rb") as fh:
                out.append(base64.b64encode(fh.read()).decode("ascii"))
        except OSError:
            continue
    return out


def process_asset(asset: str, kind: str, root: str, cfg: CaptionConfig, generated_at: str,
                  *, client: MediaCaptionClient, provenance: dict, force: bool) -> dict:
    rel = common.rel_path(asset, root)
    sc, skip = _gate(asset, kind, force=force)
    if skip is not None and sc is None:
        return _result(skip, rel, kind)
    if skip == "skipped_done":
        return _result(skip, rel, kind)

    block = {"tool": CAPTION_TOOL_NAME, "tool_version": CAPTION_TOOL_VERSION,
             "model": cfg.model, "deployment": provenance.get("deployment"),
             "api_version": provenance.get("api_version"), "generated_at_utc": generated_at,
             "config": cfg.echo(), "source_kind": kind}

    try:
        if kind == "image":
            occ0 = ((sc.get("media_frames") or {}).get("image_occurrences") or [{}])[0]
            if occ0.get("redacted") or (occ0.get("safety") or {}).get("flag") == "block":
                block.update({"caption": None, "description": None, "redacted": True,
                              "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                                        "cached_tokens": 0, "estimated_cost_usd": 0.0}})
                sc.pop("media_caption_error", None)
                sc["media_caption"] = block
                common.dump_json(sc, common.sidecar_path(asset))
                return _result("redacted_skip", rel, kind)
            with open(asset, "rb") as fh:
                image_b64 = base64.b64encode(fh.read()).decode("ascii")
            import mimetypes
            res = client.caption_image(image_b64, mime=mimetypes.guess_type(asset)[0] or "image/png")
            data = res.data
            block.update({"caption": data.get("caption"), "description": data.get("description"),
                          "content_class": data.get("content_class"),
                          "has_visual_content": data.get("has_visual_content"),
                          "safety": data.get("safety"), "keyframes_used": 0})
        else:
            transcript = _transcript_text(sc)
            keyframes = _sample_keyframes(asset, sc, cfg.max_keyframes) if kind == "video" else []
            res = client.caption_av(kind, transcript, keyframes)
            data = res.data
            block.update({"caption": data.get("caption"), "description": data.get("description"),
                          "transcript_chars": len(transcript), "keyframes_used": len(keyframes)})
    except CaptionCallError as exc:
        return _record_error(asset, sc, rel, kind, generated_at, exc.code, str(exc))

    cost = usd(res, cfg)
    block["usage"] = {"prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens,
                      "cached_tokens": res.cached_tokens, "estimated_cost_usd": round(cost, 6)}
    sc.pop("media_caption_error", None)
    sc["media_caption"] = block
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("captioned", rel, kind, prompt=res.prompt_tokens, completion=res.completion_tokens,
                   cached=res.cached_tokens, cost=cost)


def _record_error(asset, sc, rel, kind, generated_at, code, message) -> dict:
    sc["media_caption_error"] = {"code": code, "message": message[:300], "generated_at_utc": generated_at}
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("error", rel, kind, error_code=code, message=message[:300])


def _result(action, rel, kind, *, error_code=None, message=None, prompt=0, completion=0, cached=0,
            cost=0.0) -> dict:
    return {"action": action, "rel": rel, "kind": kind, "error_code": error_code, "message": message,
            "prompt": prompt, "completion": completion, "cached": cached, "cost": cost}


def run(root: str, cfg: CaptionConfig, generated_at: str, *, client: MediaCaptionClient,
        provenance: dict, force: bool, workers: int | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    assets = common.find_media(root)  # audio + video + image all get a doc caption/description
    total = len(assets)
    workers = workers or cfg.concurrency

    actions = {"captioned": 0, "redacted_skip": 0, "skipped_done": 0, "no_sidecar": 0, "no_probe": 0,
               "no_transcribe": 0, "no_frames": 0, "error": 0}
    agg = {"prompt": 0, "completion": 0, "cached": 0, "cost": 0.0}
    errors: list[dict] = []

    def consume(r: dict) -> None:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "captioned":
            for k in agg:
                agg[k] += r[k] if k != "cost" else r["cost"]
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})

    def worker(item):
        asset, kind = item
        return process_asset(asset, kind, root, cfg, generated_at, client=client,
                             provenance=provenance, force=force)

    common.drive(assets, worker, workers=workers, on_result=consume, total=total, label="assets",
                 started=started)

    manifest = {
        "tool": CAPTION_TOOL_NAME, "tool_version": CAPTION_TOOL_VERSION,
        "model": cfg.model, "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"), "generated_at_utc": generated_at, "root": root,
        "assets_seen": total,
        "assets_captioned": actions["captioned"],
        "assets_redacted_skipped": actions["redacted_skip"],
        "assets_skipped_already_done": actions["skipped_done"],
        "assets_no_transcribe": actions["no_transcribe"],
        "assets_no_frames": actions["no_frames"],
        "assets_errored": actions["error"],
        "usage": {"prompt_tokens": agg["prompt"], "completion_tokens": agg["completion"],
                  "cached_tokens": agg["cached"], "estimated_cost_usd": round(agg["cost"], 6)},
        "errors": sorted(errors, key=lambda e: e["file"]),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    common.dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
