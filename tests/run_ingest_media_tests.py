"""Stage-8 MEDIA fold-in OFFLINE tests — no DB, no Azure, no ffmpeg, no PyMuPDF. Builds a synthetic media
corpus (a clean video with a blocked keyframe, a clean audio, a clean image, a blocked image, a CSAM image)
under a NON-IMAGES dir, then runs S8 ``prepare`` and asserts the media apply-package: keyframe/image vectors
(``artifact_kind`` + ``timestamp_ms`` + ``frame_index``), media_metadata, A/V transcript chunks, the §4a
outcomes (block-keyframe → source_withheld video; blocked image → withheld doc + placeholder; CSAM image →
skip), and the read-only invariant.

Run:  python tests/run_ingest_media_tests.py
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ingest.config import IngestConfig                                  # noqa: E402
from src.ingest.emit import prepare                                         # noqa: E402
from tests.make_ingest_fixtures import csha, DIMS, MODEL, IMG_MODEL, IMG_DIMS, _store, _write, _write_bytes  # noqa: E402

_fail: list = []
def check(c, m): print(f"  [{'PASS' if c else 'FAIL'}] {m}"); (None if c else _fail.append(m))

H1 = "a" * 64      # vid1 keyframe 0 (embedded)
HI1 = "b" * 64     # img1 (embedded)
HI2 = "c" * 64     # img2 (blocked → no vector)


def _emb(path):
    _write(path, {"embedding_model": IMG_MODEL, "model_version": "v1", "dim": IMG_DIMS,
                  "vector": [0.1, 0.2, 0.3, 0.4]})


def _chunks(prefix, texts):
    return [{"text": "p", "heading_path": [], "chunk_index": 0, "token_count": 5, "page_number": None,
             "chunk_kind": "transcript",
             "children": [{"text": t, "context_prefix": prefix, "content_sha": csha(prefix, t),
                           "chunk_index": i, "heading_path": [], "token_count": 3, "chunk_kind": "transcript"}
                          for i, t in enumerate(texts)]}]


def _sidecar(kind, *, sha, transcribe=None, frames=None, caption=None, chunked=True, dn="", md=None):
    sc = {"status": "ok", "generator": {"tool": "media", "tool_version": "1", "generated_at_utc": "t"},
          "source": {"filename": f"{dn}", "relative_path": dn, "bytes": 100, "sha256": sha,
                     "content_type": {"audio": "audio/mp4", "video": "video/mp4", "image": "image/jpeg"}[kind]},
          "document": {"source_kind": kind}, "media_metadata": md or {},
          "media_probe": {"ffprobe": {"format": {"duration": "1"}}}}
    if transcribe is not None:
        sc["media_transcribe"] = transcribe
    if frames is not None:
        sc["media_frames"] = frames
    if caption is not None:
        sc["media_caption"] = caption
    if chunked:
        sc["media_chunk"] = {"document_name": dn, "case_key": "epstein", "dataset_key": "DataSet-55",
                             "embedding_model": MODEL, "counts": {}}
    return sc


def build_media_corpus(root: str) -> dict:
    base = os.path.join(root, "DataSet-55", "VOL00055", "NATIVES")
    os.makedirs(base, exist_ok=True)

    # vid1.mp4 — clean video; keyframe0 embedded, keyframe1 BLOCKED (redacted) → source_withheld video
    open(os.path.join(base, "vid1.mp4"), "wb").close()
    _write(os.path.join(base, "vid1.mp4.media.json"), _sidecar(
        "video", sha="1" * 64, dn="vid1",
        md={"duration_seconds": 600, "video_width": 640, "video_height": 480, "frame_rate": 30,
            "keyframe_count": 2, "transcript_word_count": 2, "poster_blob_path": "vid1.mp4.media/poster.webp"},
        transcribe={"windows": [{"index": 0, "start_s": 0, "end_s": 120, "text": "hello"}], "speaker_label": None},
        frames={"safety_rollup": {"flag": "block", "block": 1, "review": 0, "max_severity": 6},
                "image_occurrences": [
                    {"artifact_kind": "keyframe", "frame_index": 0, "timestamp_ms": 5000, "page_number": -1,
                     "content_hash": H1, "embedded": True, "redacted": False,
                     "emb_path": "vid1.mp4.media/embeddings/" + H1 + ".emb.json",
                     "frame_path": "vid1.mp4.media/frames/frame_0000.webp",
                     "safety": {"flag": "ok", "max_severity": 0, "categories": []}},
                    {"artifact_kind": "keyframe", "frame_index": 1, "timestamp_ms": 300000, "page_number": -1,
                     "content_hash": "d" * 64, "embedded": False, "redacted": True, "emb_path": None,
                     "frame_path": None,
                     "safety": {"flag": "block", "max_severity": 6,
                                "categories": [{"category": "Sexual", "severity": 6}]}}]},
        caption={"caption": "A video.", "description": "desc", "model": "gpt-5-mini"}))
    _write(os.path.join(base, "vid1.mp4.chunks.json"), _chunks("v1", ["hello", "world"]))
    _emb(os.path.join(base, "vid1.mp4.media", "embeddings", H1 + ".emb.json"))
    _write_bytes(os.path.join(base, "vid1.mp4.media", "frames", "frame_0000.webp"), b"WEBP0")
    _write_bytes(os.path.join(base, "vid1.mp4.media", "poster.webp"), b"POSTER")
    _write_bytes(os.path.join(base, "vid1.mp4.media", "transcript.md"), b"## [00:00:00]\n\nhello\n")

    # aud1.m4a — clean audio, transcript only (no frames) → media_metadata, no image vectors
    open(os.path.join(base, "aud1.m4a"), "wb").close()
    _write(os.path.join(base, "aud1.m4a.media.json"), _sidecar(
        "audio", sha="2" * 64, dn="aud1",
        md={"duration_seconds": 300, "audio_channels": 2, "sample_rate": 16000, "transcript_word_count": 2},
        transcribe={"windows": [{"index": 0, "start_s": 0, "end_s": 120, "text": "audio"}], "speaker_label": None},
        caption={"caption": "An audio.", "description": "d", "model": "gpt-5-mini"}))
    _write(os.path.join(base, "aud1.m4a.chunks.json"), _chunks("a1", ["audio", "words"]))
    _write_bytes(os.path.join(base, "aud1.m4a.media", "transcript.md"), b"## [00:00:00]\n\naudio\n")

    # img1.jpg — clean standalone image: embedded vector (artifact_kind=image) + image_caption chunks
    open(os.path.join(base, "img1.jpg"), "wb").close()
    _write(os.path.join(base, "img1.jpg.media.json"), _sidecar(
        "image", sha="3" * 64, dn="img1",
        frames={"safety_rollup": {"flag": "ok", "block": 0, "review": 0, "max_severity": 0},
                "image_occurrences": [
                    {"artifact_kind": "image", "frame_index": 0, "timestamp_ms": None, "page_number": -1,
                     "content_hash": HI1, "embedded": True, "redacted": False,
                     "emb_path": "img1.jpg.media/embeddings/" + HI1 + ".emb.json", "frame_path": None,
                     "safety": {"flag": "ok", "max_severity": 0, "categories": []}}]},
        caption={"caption": "a photo", "description": "d", "content_class": "photo",
                 "has_visual_content": True, "safety": {"flag": "ok"}, "model": "gpt-5-mini"}))
    _write(os.path.join(base, "img1.jpg.chunks.json"), _chunks("i1", ["photo"]))
    _emb(os.path.join(base, "img1.jpg.media", "embeddings", HI1 + ".emb.json"))

    # img2.jpg — BLOCKED standalone image: withhold whole doc (placeholder, no vector, source_withheld)
    open(os.path.join(base, "img2.jpg"), "wb").close()
    _write(os.path.join(base, "img2.jpg.media.json"), _sidecar(
        "image", sha="4" * 64, dn="img2", chunked=False,
        frames={"safety_rollup": {"flag": "block", "block": 1, "review": 0, "max_severity": 6},
                "image_occurrences": [
                    {"artifact_kind": "image", "frame_index": 0, "timestamp_ms": None, "page_number": -1,
                     "content_hash": HI2, "embedded": False, "redacted": True, "emb_path": None,
                     "frame_path": None, "safety": {"flag": "block", "max_severity": 6,
                                                    "categories": [{"category": "Sexual", "severity": 6}]}}]},
        caption={"caption": None, "description": None, "redacted": True, "safety": {}, "model": "gpt-5-mini"}))

    # img3.jpg — CSAM-suspected: caption.safety.minor_indicator → whole doc NOT ingested
    open(os.path.join(base, "img3.jpg"), "wb").close()
    _write(os.path.join(base, "img3.jpg.media.json"), _sidecar(
        "image", sha="5" * 64, dn="img3", chunked=False,
        frames={"safety_rollup": {"flag": "review", "block": 0, "review": 1, "max_severity": 2},
                "image_occurrences": [{"artifact_kind": "image", "frame_index": 0, "content_hash": "e" * 64,
                                       "embedded": False, "redacted": True, "safety": {"flag": "review"}}]},
        caption={"caption": None, "description": None, "safety": {"minor_indicator": True}, "model": "gpt-5-mini"}))

    # text store: all media transcript/caption shas
    shas = ({csha("v1", t) for t in ("hello", "world")} | {csha("a1", t) for t in ("audio", "words")}
            | {csha("i1", t) for t in ("photo",)})
    _store(os.path.join(root, "DataSet-55", ".embeddings"), {s: [float(i) for i in range(DIMS)] for s in shas})
    return {"present_text_vectors": len(shas)}


def _hashes(root):
    out = {}
    for dp, _d, files in os.walk(root):
        if os.sep + ".embeddings" in dp:
            continue
        for fn in files:
            if fn.endswith(".json") and not fn.startswith(("ingest-", "media-")):
                p = os.path.join(dp, fn)
                out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def main() -> int:
    cfg = IngestConfig(dimensions=DIMS)
    root = tempfile.mkdtemp(prefix="tfe_s8_media_")
    facts = build_media_corpus(root)
    pkg = os.path.join(os.path.dirname(root), "tfe_s8_media_pkg")
    shutil.rmtree(pkg, ignore_errors=True)
    try:
        before = _hashes(root)
        pm = prepare([os.path.join(root, "DataSet-55")], cfg, pkg)
        plans = {p["rel"].split("/")[-1]: p
                 for p in (json.loads(l) for l in open(os.path.join(pkg, "apply-plan.jsonl"), encoding="utf-8"))}

        print("\n== discovery + ingestable set (media under NON-IMAGES dir) ==")
        check(set(plans) == {"vid1.mp4", "aud1.m4a", "img1.jpg", "img2.jpg"},
              f"4 media docs ingestable; img3 (CSAM) excluded (got {sorted(plans)})")
        check(pm["corpus"]["csam_documents_NOT_ingested"] == 1, "1 CSAM media doc NOT ingested")

        print("\n== source_kind routing ==")
        check(plans["vid1.mp4"]["source_kind"] == "video" and plans["aud1.m4a"]["source_kind"] == "audio"
              and plans["img1.jpg"]["source_kind"] == "image", "source_kind set per media doc")

        print("\n== keyframe / image vectors (artifact_kind + timestamp + frame_index) ==")
        img_rows = [l for l in open(os.path.join(pkg, "image_embeddings.copy"), encoding="utf-8") if l.strip()]
        check(len(img_rows) == 2, f"2 image vectors: vid1 keyframe0 + img1 (blocked/redacted skipped) got {len(img_rows)}")
        check({l.split(chr(9))[0] for l in img_rows} == {H1, HI1}, "image vectors keyed by occurrence content_hash")
        vocc = plans["vid1.mp4"]["image_occurrences"]
        check(len(vocc) == 1 and vocc[0]["artifact_kind"] == "keyframe" and vocc[0]["timestamp_ms"] == 5000
              and vocc[0]["frame_index"] == 0, "vid1 keyframe occurrence: artifact_kind+timestamp_ms+frame_index")
        check(plans["img1.jpg"]["image_occurrences"][0]["artifact_kind"] == "image", "img1 occurrence artifact_kind=image")

        print("\n== media_metadata (A/V only) ==")
        check(plans["vid1.mp4"]["media_metadata"] and plans["vid1.mp4"]["media_metadata"]["duration_seconds"] == 600
              and plans["vid1.mp4"]["media_metadata"]["source_kind"] == "video", "vid1 media_metadata row populated")
        check(plans["aud1.m4a"]["media_metadata"]["audio_channels"] == 2, "aud1 media_metadata (audio)")
        check(plans["img1.jpg"]["media_metadata"] is None, "image doc has NO media_metadata row")

        print("\n== §4a: block-keyframe video + blocked image withhold ==")
        check(plans["vid1.mp4"]["source_withheld"] is True, "vid1: blocked keyframe -> source_withheld (transcript still ingests)")
        check(plans["img2.jpg"]["source_withheld"] is True, "img2: blocked image -> source_withheld")
        redacted = [b for b in plans["img2.jpg"]["blobs"] if b["action"] == "placeholder:content-redacted"]
        check(redacted and redacted[0]["artifact"].startswith("media"), "img2: media blob -> content-redacted placeholder")
        check(not plans["img2.jpg"]["image_occurrences"], "img2: no image vector/occurrence (withheld)")

        print("\n== transcript text chunks + blobs ==")
        emb_rows = [l for l in open(os.path.join(pkg, "embeddings.copy"), encoding="utf-8") if l.strip()]
        check(len(emb_rows) == facts["present_text_vectors"],
              f"{facts['present_text_vectors']} media text vectors (transcript+caption) got {len(emb_rows)}")
        arts = {b["artifact"] for b in plans["vid1.mp4"]["blobs"]}
        check({"media.mp4", "transcript.md", "poster.webp"} <= arts, f"vid1 blobs: media+transcript+poster ({arts})")
        check(any(b["artifact"].startswith("artifacts/frames/") for b in plans["vid1.mp4"]["blobs"]),
              "vid1: embedded keyframe webp queued as a frame blob")

        print("\n== read-only: media sidecars/chunks unchanged ==")
        check(_hashes(root) == before, "all media sidecars + chunks byte-identical after prepare")
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(pkg, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"FAILED: {len(_fail)}" if _fail else "ALL MEDIA FOLD-IN CHECKS PASSED")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
