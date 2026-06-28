"""The media-track apply record (offline, no DB/Storage) — the media analog of ``record.build_apply_record``.

Reads a media asset's ``<asset>.media.json`` sidecar (probe + transcribe + frames + caption + chunk blocks,
see HANDOFF-media-track) and produces the SAME tenant-agnostic ``ApplyRecord`` the PDF track produces, so
``emit.py`` / ``apply.py`` serialize and replay both tracks uniformly. The only media-specific shape is the
**keyframe** image vector / occurrence (``artifact_kind='keyframe'`` + ``timestamp_ms`` + ``frame_index``) and
the ``pipeline.media_metadata`` row.

§4a for media (the media track already redacts ``block`` keyframes/images at media_stage3 — bytes deleted,
vector absent — so this is a conformance + whole-doc layer):
  * **CSAM** (standalone-image caption ``minor_indicator``) → whole doc NOT ingested (``skip_csam``).
  * **Blocked standalone image** (rollup ``block`` / occurrence ``redacted`` / caption ``explicit``) → withhold
    the whole image doc: no vector, image blob → ``content-redacted`` placeholder, ``source_withheld=true``.
  * **A/V with a blocked keyframe** → ingest normally (transcript is the content); the blocked keyframe already
    has no vector/blob; set ``source_withheld=true`` (the raw media still contains the frame) + a finding.

Identifiers come from the authoritative ``media_chunk`` block (the exact ``document_name``/``case_key``/
``dataset_key`` the chunks' ``content_sha`` were baked with). Self-contained: imports no psycopg/azure/ffmpeg.
"""

from __future__ import annotations

import json
import os

from .config import IngestConfig
from .discover import media_sidecar_path, media_chunks_path, derive_identifiers, IdentifierError
from .record import (ApplyRecord, BlobRef, ImageVector, ACTION_UPLOAD, ACTION_PLACEHOLDER_REDACTED,
                     ordered_child_shas, _read_emb, _mime, _CS_COL)

_AV = ("audio", "video")
# pipeline.media_metadata typed columns we source from the sidecar's media_metadata (0002/0009).
_MEDIA_MD_KEYS = ("duration_seconds", "audio_channels", "sample_rate", "video_width", "video_height",
                  "frame_rate", "transcript_blob_path", "transcript_model", "transcript_language",
                  "transcript_word_count", "transcript_confidence", "speaker_count", "poster_blob_path",
                  "keyframe_count", "keyframe_interval_seconds", "container_format", "video_codec",
                  "audio_codec", "overall_bit_rate")


def _identifiers(asset: str, sc: dict, cfg: IngestConfig) -> dict:
    """Prefer the media_chunk block (authoritative — what the content_sha were keyed with); else derive."""
    mc = sc.get("media_chunk") or {}
    if mc.get("document_name") and mc.get("case_key") and mc.get("dataset_key"):
        return {"document_name": mc["document_name"], "case_key": mc["case_key"],
                "dataset_key": mc["dataset_key"]}
    document_name, case_key, dataset_key = derive_identifiers(asset, cfg.case_key)
    return {"document_name": document_name, "case_key": case_key, "dataset_key": dataset_key}


def _media_safety(sc: dict, kind: str) -> tuple[bool, bool, bool, str | None]:
    """(csam, withhold_doc, source_withheld, tier). The media track already removed ``block`` bytes/vectors."""
    frames = sc.get("media_frames") or {}
    rollup = frames.get("safety_rollup") or {}
    csafety = (sc.get("media_caption") or {}).get("safety") or {}   # image-only Stage-4-style verdict
    if csafety.get("minor_indicator") is True:
        return True, True, True, "csam"
    if kind == "image":
        occ0 = (frames.get("image_occurrences") or [{}])[0]
        blocked = bool(occ0.get("redacted") or rollup.get("flag") == "block"
                       or csafety.get("explicit") is True)
        return False, blocked, blocked, ("blocked_image" if blocked else None)
    blocked = rollup.get("flag") == "block" or int(rollup.get("block") or 0) > 0
    return False, False, blocked, ("blocked_keyframe" if blocked else None)


def _media_metadata_row(sc: dict, kind: str) -> dict | None:
    """pipeline.media_metadata row (A/V only — table CHECK is audio/video). Typed cols + the raw ffprobe bag."""
    if kind not in _AV:
        return None
    md = sc.get("media_metadata") or {}
    row = {k: md.get(k) for k in _MEDIA_MD_KEYS if md.get(k) is not None}
    row["source_kind"] = kind
    row["metadata"] = (sc.get("media_probe") or {}).get("ffprobe") or {}
    return row


def _findings(sc: dict, kind: str) -> list:
    out: list = []
    minor = bool(((sc.get("media_caption") or {}).get("safety") or {}).get("minor_indicator"))
    for occ in ((sc.get("media_frames") or {}).get("image_occurrences") or []):
        saf = occ.get("safety") or {}
        if saf.get("flag") not in ("block", "review"):
            continue
        cols = {"hate": 0, "sexual": 0, "violence": 0, "self_harm": 0}
        for c in (saf.get("categories") or []):
            col = _CS_COL.get(str(c.get("category") or "").lower())
            if col:
                try:
                    cols[col] = max(cols[col], int(c.get("severity") or 0))
                except (TypeError, ValueError):
                    pass
        ak = occ.get("artifact_kind") or "keyframe"
        modality = "keyframe" if ak == "keyframe" else "artifact_image"
        loc = (f"frame:{occ.get('frame_index')}" if ak == "keyframe"
               else f"artifact:{occ.get('frame_index') or 0}")
        out.append({"modality": modality, "locator": loc, **cols,
                    "csam_suspected": minor, "model": "azure-ai-vision-multimodal"})
    return out


def _metadata_bag(sc: dict, media_md: dict | None) -> dict:
    src = sc.get("source") or {}
    cap = sc.get("media_caption") or {}
    bag = {
        "source": {k: src.get(k) for k in ("sha256", "bytes", "content_type", "filename")
                   if src.get(k) is not None},
        "media": {k: v for k, v in (media_md or {}).items() if k not in ("metadata", "source_kind")},
        "caption": {k: cap.get(k) for k in ("caption", "description", "content_class") if cap.get(k)},
        "generator": {k: (sc.get("generator") or {}).get(k)
                      for k in ("tool", "tool_version", "generated_at_utc") if (sc.get("generator") or {}).get(k)},
    }
    return {k: v for k, v in bag.items() if v}


def _asset_context(sc: dict) -> dict | None:
    cap = sc.get("media_caption") or {}
    text = cap.get("caption")
    desc = cap.get("description")
    summary_text = "\n\n".join(p for p in (text, desc) if p) or None
    sj = {k: cap.get(k) for k in ("content_class", "has_visual_content") if cap.get(k) is not None}
    if not summary_text and not sj:
        return None
    return {"summary_text": summary_text, "summary_json": sj,
            "model": cap.get("model"), "version": 1}


def build_media_record(asset: str, kind: str, root: str, cfg: IngestConfig, store) -> ApplyRecord:
    """Build the tenant-agnostic apply record for one staged media asset (or the skip reason)."""
    rel = os.path.relpath(asset, root).replace(os.sep, "/")
    r = ApplyRecord(rel)
    r.source_kind = kind
    r.staged_pdf = os.path.abspath(asset)
    r.staged_chunks = os.path.abspath(media_chunks_path(asset))
    try:
        with open(media_sidecar_path(asset), "r", encoding="utf-8") as f:
            sc = json.load(f)
    except FileNotFoundError:
        r.status = "no_sidecar"; return r
    except Exception as exc:
        r.status = "error"; r.error = f"bad_sidecar: {type(exc).__name__}"; return r
    if sc.get("status") != "ok":
        r.status = "skip_status"; return r

    try:
        r.identifiers = _identifiers(asset, sc, cfg)
    except IdentifierError as exc:
        r.status = "error"; r.error = f"no_identifiers: {str(exc)[:120]}"; return r

    csam, withhold_doc, source_withheld, tier = _media_safety(sc, kind)
    if csam:
        r.csam_assets = 1
        r.status = "skip_csam"                           # whole doc NOT ingested; preserve + report
        return r
    r.source_withheld = source_withheld
    if tier:
        r.withheld_tiers[tier] = r.withheld_tiers.get(tier, 0) + 1

    base_dir = os.path.dirname(asset)
    ext = os.path.splitext(asset)[1].lower()
    media_artifact = f"media{ext}" if ext else "media"
    content_type = (sc.get("source") or {}).get("content_type")

    # --- documents row + bag + media_metadata + asset_context ---
    r.media_metadata = _media_metadata_row(sc, kind)
    r.document = {
        "case_key": r.identifiers["case_key"], "dataset_key": r.identifiers["dataset_key"],
        "document_name": r.identifiers["document_name"], "source_kind": kind,
        "content_type": content_type, "filehash": (sc.get("source") or {}).get("sha256"),
        "page_count": None, "title": (sc.get("media_caption") or {}).get("caption"), "source": "upload",
        "has_transcript": bool(sc.get("media_transcribe")),       # apply → md_blob_path (transcript.md)
    }
    r.metadata = _metadata_bag(sc, r.media_metadata)
    r.asset_context = _asset_context(sc)
    r.safety_findings = _findings(sc, kind)

    # --- the source media file blob (placeholder if the whole image doc is withheld) ---
    if withhold_doc:
        r.blobs.append(BlobRef(media_artifact, ACTION_PLACEHOLDER_REDACTED))
        r.images_withheld += 1
    else:
        r.blobs.append(BlobRef(media_artifact, ACTION_UPLOAD, asset, content_type or _mime(asset)))

    # --- transcript.md + poster blobs (A/V) ---
    asset_out = asset + ".media"
    if sc.get("media_transcribe"):
        tpath = os.path.join(asset_out, "transcript.md")
        if os.path.exists(tpath):
            r.blobs.append(BlobRef("transcript.md", ACTION_UPLOAD, tpath, "text/markdown"))
    poster_rel = (sc.get("media_metadata") or {}).get("poster_blob_path")
    if poster_rel:
        ppath = os.path.join(base_dir, poster_rel.replace("/", os.sep))
        if os.path.exists(ppath):
            r.blobs.append(BlobRef("poster.webp", ACTION_UPLOAD, ppath, "image/webp"))

    # --- keyframe / image vectors + occurrences + frame blobs (embedded, non-redacted only) ---
    if not withhold_doc:
        for occ in ((sc.get("media_frames") or {}).get("image_occurrences") or []):
            if not occ.get("embedded") or occ.get("redacted") or not occ.get("content_hash"):
                continue
            ak = occ.get("artifact_kind") or "keyframe"
            fidx = int(occ.get("frame_index") or 0)
            ts = int(occ["timestamp_ms"]) if isinstance(occ.get("timestamp_ms"), int) else -1
            suffix = f"artifacts/frames/frame_{fidx:04d}.webp" if ak == "keyframe" else media_artifact
            emb = _read_emb(os.path.join(base_dir, str(occ.get("emb_path") or "").replace("/", os.sep))) \
                if occ.get("emb_path") else None
            if emb:
                r.image_vectors.append(ImageVector(
                    content_hash=occ["content_hash"], dim=emb["dim"], embedding_model=emb["model"],
                    vector=emb["vector"], artifact_kind=ak, page_number=-1, artifact=suffix,
                    timestamp_ms=ts, frame_index=fidx))
                r.images_embed += 1
            if ak == "keyframe" and occ.get("frame_path"):
                fpath = os.path.join(base_dir, occ["frame_path"].replace("/", os.sep))
                if os.path.exists(fpath):
                    r.blobs.append(BlobRef(suffix, ACTION_UPLOAD, fpath, "image/webp"))

    # --- child content_sha (transcript + caption chunks) + vector presence ---
    shas = ordered_child_shas(media_chunks_path(asset))
    r.content_shas = shas
    if shas and store.exists():
        present = store.get_many(set(shas))
        r.vectors_present = sum(1 for s in shas if s in present)
        r.missing_shas = [s for s in shas if s not in present]
    else:
        r.missing_shas = list(shas)
    r.vectors_missing = len(r.missing_shas)
    return r
