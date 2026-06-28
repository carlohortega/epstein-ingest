"""Media-track acceptance tests (no pytest, no network, no ffmpeg).

ffmpeg/ffprobe and Azure are unavailable in CI, so we INJECT fakes (a fake transcribe client, fake AI
Vision + Content Safety clients) and MONKEYPATCH the ``src.media.ffmpeg`` subprocess wrappers to emit
synthetic segments/frames/posters. Everything else — the walk, sidecar blocks, idempotency/resume,
content_sha dedup recipe, chunker, redaction gate, manifests — runs exactly as in production.

Run:  python tests/run_media_tests.py     (project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.media import common, ffmpeg, probe, transcribe_stage, frames_stage, caption_stage, chunk_stage  # noqa: E402
from src.media.config import ProbeConfig, TranscribeConfig, FramesConfig, CaptionConfig, ChunkConfig  # noqa: E402
from src.media.chunking import build_chunks, content_sha, transcript_markdown, transcript_vtt  # noqa: E402
from src.media.transcribe import build_windows, TranscriptWindow, estimate_cost_usd  # noqa: E402
from src.media.vision import EmbedResult, verdict_from_analysis  # noqa: E402
from src.stage3.client import LLMResult  # noqa: E402

FIXED_TS = "2026-06-23T00:00:00Z"
PROV_T = {"deployment": "fake-transcribe", "api_version": "fake"}
PROV_F = {"vision": {"api_version": "fake"}, "content_safety": {"api_version": "fake"}}
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(asset: str) -> dict:
    return common.load_json(common.sidecar_path(asset))


# --- fakes ------------------------------------------------------------------------------------------
class FakeTranscribe:
    """Deterministic transcript: each segment file's name → its 'spoken' text."""
    def __init__(self) -> None:
        self.calls = 0

    def transcribe_file(self, path: str) -> str:
        self.calls += 1
        return f"transcript of {os.path.basename(path)}"


class FakeVision:
    def __init__(self) -> None:
        self.calls = 0

    def embed_image(self, image_bytes: bytes, content_hash: str) -> EmbedResult:
        self.calls += 1
        return EmbedResult(content_hash=content_hash, embedding_model="azure-ai-vision-multimodal",
                           model_version="fake-1", dim=1024, vector=[0.1] * 1024)


class FakeSafety:
    """Returns BLOCK for any frame whose bytes contain b'BAD' (CSAM stand-in), else OK."""
    def __init__(self, cfg: FramesConfig) -> None:
        self.cfg = cfg
        self.calls = 0

    def analyze_image(self, image_bytes: bytes):
        self.calls += 1
        sev = 6 if b"BAD" in image_bytes else 0
        return verdict_from_analysis([{"category": "Sexual", "severity": sev}], self.cfg)


class FakeCaption:
    """Deterministic gpt-5-mini stand-in returning token-bearing LLMResults."""
    def __init__(self) -> None:
        self.calls = 0

    def caption_image(self, image_b64: str, *, mime: str = "image/png") -> LLMResult:
        self.calls += 1
        return LLMResult({"caption": "A photograph.", "description": "A literal description of a photo.",
                          "content_class": "photo", "has_visual_content": True, "vision_confidence": 0.9,
                          "safety": {"flag": "ok", "categories": [], "minor_indicator": False,
                                     "explicit": False}}, 120, 25, 0)

    def caption_av(self, kind: str, transcript_text: str, keyframe_b64s: list[str]) -> LLMResult:
        self.calls += 1
        return LLMResult({"caption": f"A {kind} recording.",
                          "description": f"Describes the {kind} from {len(keyframe_b64s)} frames."}, 200, 30, 0)


# --- ffmpeg monkeypatches (synthetic media) ---------------------------------------------------------
def install_fakes(duration: float, n_frames: int, bad_frame_index: int | None = None, silence=None):
    def fake_probe(path, **_kw):
        return {"format": {"duration": str(duration), "bit_rate": "128000", "format_name": "mov"},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "44100"},
                            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                             "avg_frame_rate": "30000/1001"}]}

    def fake_segment(path, out_dir, *, window_seconds, **_kw):
        os.makedirs(out_dir, exist_ok=True)
        import math
        n = max(1, int(math.ceil(duration / window_seconds)))
        paths = []
        for i in range(n):
            p = os.path.join(out_dir, f"segment_{i:04d}.mp3")
            common.write_bytes(b"\x00audio", p)
            paths.append(p)
        return paths

    def fake_scene(path, **_kw):
        # n_frames evenly-spaced scene-change timestamps (the content-aware embed set)
        return [round((i + 1) * duration / (n_frames + 1), 3) for i in range(n_frames)]

    def fake_frames_at(path, out_dir, timestamps, *, prefix="frame", **_kw):
        os.makedirs(out_dir, exist_ok=True)
        out = []
        for i, ts in enumerate(timestamps):
            p = os.path.join(out_dir, f"{prefix}_{i + 1:04d}.webp")
            payload = b"BAD-frame" if (bad_frame_index is not None and i == bad_frame_index) else f"frame{i}".encode()
            common.write_bytes(payload, p)
            out.append((p, float(ts)))
        return out

    def fake_frames(path, out_dir, *, interval_seconds, prefix="frame", **_kw):
        # safety floor grid (SAFE frames only — never injects a block)
        os.makedirs(out_dir, exist_ok=True)
        n = min(5, max(1, int(duration / interval_seconds))) if interval_seconds > 0 else 1
        paths = []
        for i in range(n):
            p = os.path.join(out_dir, f"{prefix}_{i + 1:04d}.webp")
            common.write_bytes(f"scan{i}".encode(), p)
            paths.append(p)
        return paths

    def fake_poster(path, dest, **_kw):
        common.write_bytes(b"poster", dest)

    def fake_silence(path, **_kw):
        return silence if silence is not None else []  # default: no silence → transcribe all

    ffmpeg.probe_media = fake_probe
    ffmpeg.segment_audio = fake_segment
    ffmpeg.detect_scene_changes = fake_scene
    ffmpeg.extract_frames_at = fake_frames_at
    ffmpeg.extract_frames = fake_frames
    ffmpeg.extract_poster = fake_poster
    ffmpeg.detect_silence = fake_silence


def make_corpus(root: str):
    """A tiny corpus: a video, an audio file, a standalone image, plus a PDF IMAGES tree that MUST be
    excluded (an image under IMAGES/ should never be picked up by the media walk)."""
    common.write_bytes(b"v", os.path.join(root, "NATIVES", "clip.mp4"))
    common.write_bytes(b"a", os.path.join(root, "NATIVES", "talk.mp3"))
    common.write_bytes(b"i", os.path.join(root, "NATIVES", "photo.jpg"))
    common.write_bytes(b"x", os.path.join(root, "VOL00001", "IMAGES", "0001", "scan.jpg"))  # PDF track — excluded


# --- tests ------------------------------------------------------------------------------------------
def test_pure_chunker() -> None:
    print("test_pure_chunker")
    windows = [{"index": 0, "start_s": 0.0, "end_s": 120.0, "text": "Hello world. " * 80},
               {"index": 1, "start_s": 120.0, "end_s": 180.0, "text": "Second window of speech."},
               {"index": 2, "start_s": 180.0, "end_s": 200.0, "text": ""}]  # empty dropped
    chunks, counts = build_chunks(windows, "clip", case_key="epstein", dataset_key="DS-09")
    check(isinstance(chunks, list), "chunks.json is a bare parents_to_dicts list (S7/S8 contract)")
    check(counts["children"] >= 2, "long window chunked into multiple children")
    first_child = chunks[0]["children"][0]
    check(chunks[0]["chunk_kind"] == "transcript" and first_child["chunk_kind"] == "transcript",
          "parents + children tagged transcript")
    check(first_child["timestamp_start_ms"] == 0 and first_child["timestamp_end_ms"] == 120000,
          "window-level ms timestamps flow onto chunks")
    check(len(first_child["content_sha"]) == 64 and "context_prefix" in first_child,
          "children carry a 64-hex content_sha + context_prefix (S7 dedup key)")
    # content_sha recipe matches sv-kb exactly (the dedup invariant against PDF text)
    expect = content_sha("text-embedding-3-small", first_child["context_prefix"], first_child["text"])
    check(first_child["content_sha"] == expect, "content_sha = sha256(model\\ncontext_prefix\\ntext)")
    check("## [00:00:00]" in transcript_markdown(windows), "transcript markdown headings")
    check(transcript_vtt(windows).startswith("WEBVTT"), "vtt header")


def test_windowing() -> None:
    print("test_windowing")
    w = build_windows(250.0, 120)
    check(w == [(0, 0.0, 120.0), (1, 120.0, 240.0), (2, 240.0, 250.0)], "windows clip last to duration")
    check(build_windows(0.0, 120) == [(0, 0.0, 0.0)], "unknown duration → single window")
    cfg = TranscribeConfig()
    check(abs(estimate_cost_usd(600.0, cfg) - 10 * cfg.cost_per_audio_minute) < 1e-9, "cost ∝ minutes")
    check(ffmpeg.adaptive_interval(3600, base_seconds=10, max_frames=120) == 30.0,
          "adaptive interval caps frames on long video")
    check(ffmpeg.adaptive_interval(60, base_seconds=10, max_frames=120) == 10.0,
          "short video uses base interval")


def test_probe(root: str) -> None:
    print("test_probe")
    install_fakes(duration=250.0, n_frames=3)
    m = probe.run(root, ProbeConfig(), FIXED_TS, force=False, workers=1)
    check(m["assets_seen"] == 3, "walk excludes the PDF IMAGES tree (3 assets, not 4)")
    check(m["assets_probed"] == 3, "all probed")
    sc = load(os.path.join(root, "NATIVES", "clip.mp4"))
    check(sc["status"] == "ok" and sc["document"]["source_kind"] == "video", "video probed ok")
    check(sc["media_metadata"]["duration_seconds"] == 250.0, "typed duration")
    check(sc["media_metadata"]["video_width"] == 1920 and sc["media_metadata"]["audio_channels"] == 2,
          "typed video/audio fields")
    check(abs(sc["media_metadata"]["frame_rate"] - 29.97) < 0.01, "rational fps parsed")
    check(sc["media_probe"]["ffprobe"]["format"]["format_name"] == "mov", "raw ffprobe bag kept")
    # idempotency
    m2 = probe.run(root, ProbeConfig(), FIXED_TS, force=False, workers=1)
    check(m2["assets_skipped_already_done"] == 3, "resume skips already-probed assets")


def test_probe_no_ffprobe() -> None:
    print("test_probe_no_ffprobe")
    def boom(path, **_kw):
        raise ffmpeg.FfmpegError("ffprobe not found", "no_ffprobe")
    saved = ffmpeg.probe_media
    with tempfile.TemporaryDirectory() as sub:  # isolated so it doesn't contaminate the shared corpus
        common.write_bytes(b"v", os.path.join(sub, "NATIVES", "v.mp4"))
        ffmpeg.probe_media = boom
        m = probe.run(sub, ProbeConfig(), FIXED_TS, force=False, workers=1)
        sc = load(os.path.join(sub, "NATIVES", "v.mp4"))
        check(m["assets_errored"] == 1 and sc["status"] == "error" and sc["error"]["code"] == "no_ffprobe",
              "missing ffprobe → error sidecar")
        ffmpeg.probe_media = saved  # ffprobe now 'installed'
        m2 = probe.run(sub, ProbeConfig(), FIXED_TS, force=False, workers=1)
        check(m2["assets_probed"] == 1 and m2["assets_skipped_already_done"] == 0,
              "errored asset is re-probed on resume (not skipped), once ffprobe works")
        check(load(os.path.join(sub, "NATIVES", "v.mp4"))["status"] == "ok", "re-probe succeeds")


def test_transcribe(root: str) -> None:
    print("test_transcribe")
    install_fakes(duration=250.0, n_frames=3)
    fake = FakeTranscribe()
    cfg = TranscribeConfig(window_concurrency=1, concurrency=1)
    m = transcribe_stage.run(root, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=False, workers=1)
    check(m["assets_transcribed"] == 2, "only A/V transcribed (video + audio, not image)")
    sc = load(os.path.join(root, "NATIVES", "clip.mp4"))
    block = sc["media_transcribe"]
    check(block["window_count"] == 3, "3 windows for 250s @120s")
    check(block["windows"][0]["text"].startswith("transcript of segment_"), "windows carry text")
    check(sc["media_metadata"]["transcript_model"] == "gpt-4o-mini-transcribe", "transcript metadata set")
    ad = common.asset_dir(os.path.join(root, "NATIVES", "clip.mp4"))
    check(os.path.exists(os.path.join(ad, "transcript.md")), "transcript.md blob written")
    check(os.path.exists(os.path.join(ad, "transcript.vtt")), "transcript.vtt blob written")
    check(not os.path.isdir(os.path.join(ad, "segments")), "intermediate segments cleaned up")
    # idempotency + force
    before = fake.calls
    transcribe_stage.run(root, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=False, workers=1)
    check(fake.calls == before, "resume re-transcribes nothing")
    transcribe_stage.run(root, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=True, workers=1)
    check(fake.calls > before, "--force re-transcribes")


def _fake_av_probe(duration: float, audio: bool = True):
    streams = [{"codec_type": "video", "codec_name": "h264", "width": 640, "height": 480,
                "avg_frame_rate": "30/1"}]
    if audio:
        streams.insert(0, {"codec_type": "audio", "codec_name": "aac", "channels": 1,
                           "sample_rate": "16000"})
    return lambda path, **_kw: {"format": {"duration": str(duration)}, "streams": streams}


def _fake_segment(duration: float):
    import math
    def f(path, out_dir, *, window_seconds, **_kw):
        os.makedirs(out_dir, exist_ok=True)
        ps = []
        for i in range(max(1, int(math.ceil(duration / window_seconds)))):
            p = os.path.join(out_dir, f"segment_{i:04d}.mp3")
            common.write_bytes(b"a", p)
            ps.append(p)
        return ps
    return f


def test_audio_gating() -> None:
    """No-audio + silence gating on the transcription premium line (the surveillance-cost fix)."""
    print("test_audio_gating")
    cfg = TranscribeConfig(window_concurrency=1, concurrency=1)
    # (a) no audio stream → skipped, zero API calls
    with tempfile.TemporaryDirectory() as t:
        r = os.path.join(t, "DataSet-09")
        common.write_bytes(b"v", os.path.join(r, "NATIVES", "silent.mp4"))
        ffmpeg.probe_media = _fake_av_probe(100.0, audio=False)
        probe.run(r, ProbeConfig(), FIXED_TS, force=False, workers=1)
        fake = FakeTranscribe()
        m = transcribe_stage.run(r, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=False, workers=1)
        blk = load(os.path.join(r, "NATIVES", "silent.mp4"))["media_transcribe"]
        check(m["assets_no_audio_stream"] == 1 and blk["skipped_reason"] == "no_audio_stream"
              and blk["window_count"] == 0 and fake.calls == 0,
              "no-audio video → transcription skipped, zero API calls (block written, not error)")
    # (b) fully silent audio → skipped
    with tempfile.TemporaryDirectory() as t:
        r = os.path.join(t, "DataSet-09")
        common.write_bytes(b"a", os.path.join(r, "NATIVES", "quiet.m4a"))
        ffmpeg.probe_media = _fake_av_probe(250.0, audio=True)
        probe.run(r, ProbeConfig(), FIXED_TS, force=False, workers=1)
        ffmpeg.segment_audio = _fake_segment(250.0)
        ffmpeg.detect_silence = lambda path, **_kw: [(0.0, 250.0)]
        fake = FakeTranscribe()
        m = transcribe_stage.run(r, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=False, workers=1)
        check(m["assets_silent_audio"] == 1 and fake.calls == 0,
              "fully-silent audio → skipped, zero API calls")
    # (c) partial silence → only the sound window is transcribed
    with tempfile.TemporaryDirectory() as t:
        r = os.path.join(t, "DataSet-09")
        common.write_bytes(b"a", os.path.join(r, "NATIVES", "mixed.m4a"))
        ffmpeg.probe_media = _fake_av_probe(250.0, audio=True)
        probe.run(r, ProbeConfig(), FIXED_TS, force=False, workers=1)
        ffmpeg.segment_audio = _fake_segment(250.0)
        ffmpeg.detect_silence = lambda path, **_kw: [(0.0, 120.0), (240.0, None)]  # sound only in [120,240]
        fake = FakeTranscribe()
        transcribe_stage.run(r, cfg, FIXED_TS, client=fake, provenance=PROV_T, force=False, workers=1)
        blk = load(os.path.join(r, "NATIVES", "mixed.m4a"))["media_transcribe"]
        check(blk["windows_skipped_silent"] == 2 and fake.calls == 1,
              "partial silence → only the sound window transcribed (2 of 3 windows skipped)")
        u = blk["usage"]
        check(0 < u["transcribed_minutes"] < u["audio_minutes"],
              "billed minutes = transcribed (< total) when silence is skipped")


def test_transcribe_dryrun(root: str) -> None:
    print("test_transcribe_dryrun")
    d = transcribe_stage.dry_run(root, TranscribeConfig())
    check(d["av_assets_probed"] == 2, "dry-run counts probed A/V")
    check(abs(d["audio_minutes"] - (250.0 + 250.0) / 60.0) < 0.01, "dry-run sums audio minutes from probes")


def test_frames(root: str) -> None:
    print("test_frames")
    install_fakes(duration=250.0, n_frames=4, bad_frame_index=2)  # 4 scene-change frames; #2 is high-risk
    cfg = FramesConfig(frame_concurrency=1, concurrency=1)
    vision, safety = FakeVision(), FakeSafety(cfg)
    m = frames_stage.run(root, cfg, FIXED_TS, vision_client=vision, safety_client=safety,
                         provenance=PROV_F, force=False, workers=1)
    check(m["assets_framed"] == 2, "video + standalone image framed (not audio)")
    sc = load(os.path.join(root, "NATIVES", "clip.mp4"))
    fb = sc["media_frames"]
    check(fb["keyframe_mode"] == "scene" and fb["keyframe_interval_seconds"] is None,
          "scene-change keyframe mode (content-aware, no fixed interval)")
    check(fb["keyframe_count"] == 4, "4 scene-change keyframes")
    check(fb["safety_floor_scanned"] >= 1, "coarse safety floor scanned as a backstop")
    check(fb["embedded_count"] == 3 and fb["redacted_count"] == 1, "blocked frame redacted, not embedded")
    occ = fb["image_occurrences"]
    blocked = next(o for o in occ if o["redacted"])
    check(blocked["safety"]["flag"] == "block" and blocked["emb_path"] is None, "blocked frame has no vector")
    check(blocked["frame_path"] is None, "blocked frame blob removed")
    kept = next(o for o in occ if o["embedded"])
    check(kept["artifact_kind"] == "keyframe" and kept["page_number"] == -1, "occurrence shape (keyframe, page -1)")
    emb_file = os.path.join(common.asset_dir(os.path.join(root, "NATIVES", "clip.mp4")),
                            "embeddings", f"{kept['content_hash']}.emb.json")
    embj = common.load_json(emb_file)
    check(embj and embj["dim"] == 1024 and embj["embedding_model"] == "azure-ai-vision-multimodal"
          and "model_version" in embj and "content_hash" not in embj,
          ".emb.json matches Stage-4 staged format (embedding_model/model_version/dim/vector)")
    check(fb["safety_rollup"]["block"] == 1, "safety rollup counts the block")
    # the blocked frame's bytes must be gone from disk
    fr_dir = os.path.join(common.asset_dir(os.path.join(root, "NATIVES", "clip.mp4")), "frames")
    check(len(os.listdir(fr_dir)) == 3, "only the 3 safe frames remain on disk")
    # standalone image
    si = load(os.path.join(root, "NATIVES", "photo.jpg"))
    iocc = si["media_frames"]["image_occurrences"]
    check(len(iocc) == 1 and iocc[0]["artifact_kind"] == "image" and iocc[0]["embedded"],
          "standalone image → single image vector, no transcript/chunks (§4 decision)")


def test_caption(root: str) -> None:
    print("test_caption")
    fake = FakeCaption()
    prov = {"deployment": "fake-gpt5", "api_version": "fake"}
    m = caption_stage.run(root, CaptionConfig(concurrency=1), FIXED_TS, client=fake, provenance=prov,
                          force=False, workers=1)
    check(m["assets_captioned"] == 3, "video + audio + image all get a doc caption+description")
    check(m["usage"]["prompt_tokens"] > 0 and m["usage"]["completion_tokens"] > 0,
          "caption stage captures input+output tokens (the media token cost line)")
    vcap = load(os.path.join(root, "NATIVES", "clip.mp4"))["media_caption"]
    check(bool(vcap["caption"]) and bool(vcap["description"]), "video caption + description stored")
    check(vcap["keyframes_used"] >= 1, "video caption fed by sampled keyframes (not all frames)")
    check(vcap["usage"]["estimated_cost_usd"] >= 0 and vcap["usage"]["prompt_tokens"] == 200,
          "per-asset token + USD recorded")
    icap = load(os.path.join(root, "NATIVES", "photo.jpg"))["media_caption"]
    check(icap["content_class"] == "photo" and icap.get("safety") is not None,
          "standalone image caption uses Stage-4-identical fields")
    before = fake.calls
    caption_stage.run(root, CaptionConfig(concurrency=1), FIXED_TS, client=fake, provenance=prov,
                      force=False, workers=1)
    check(fake.calls == before, "resume re-captions nothing")


def test_chunk(root: str) -> None:
    print("test_chunk")
    m = chunk_stage.run(root, ChunkConfig(), FIXED_TS, case_key="epstein", dataset_key="DS-09",
                        force=False, workers=1)
    check(m["assets_chunked"] == 3, "video + audio (transcript+summary) AND image (image_caption) chunked")
    asset = os.path.join(root, "NATIVES", "clip.mp4")
    chunks = common.load_json(common.chunks_path(asset))
    check(isinstance(chunks, list) and chunks, "chunks.json is a bare list (parents_to_dicts)")
    kinds = {c["chunk_kind"] for p in chunks for c in p["children"]}
    check("transcript" in kinds and "summary" in kinds,
          "video chunks combine transcript + summary (caption) kinds")
    child = chunks[0]["children"][0]
    check(len(child["content_sha"]) == 64, "children carry content_sha (embed.collect consumes unchanged)")
    sc = load(asset)
    total_children = sum(len(p.get("children") or []) for p in chunks)
    check(sc["media_chunk"]["counts"]["children"] == total_children, "media_chunk block records counts")
    check(sc["media_chunk"]["dataset_key"] == "DS-09" and sc["media_chunk"]["case_key"] == "epstein",
          "identifiers recorded on the sidecar block")
    # standalone image NOW produces image_caption chunks (caption text-searchable; the §4 model update)
    ichunks = common.load_json(common.chunks_path(os.path.join(root, "NATIVES", "photo.jpg")))
    check(isinstance(ichunks, list) and ichunks, "standalone image now has chunks.json")
    ikinds = {c["chunk_kind"] for p in ichunks for c in p["children"]}
    check(ikinds == {"image_caption"}, "image chunks are image_caption kind (no transcript)")


def test_additive_reprobe(root: str) -> None:
    """A forced re-probe (media_stage1 --force) must NOT clobber downstream blocks — each stage owns its
    own lane additively."""
    print("test_additive_reprobe")
    asset = os.path.join(root, "NATIVES", "clip.mp4")
    before = load(asset)
    check(all(before.get(k) for k in ("media_probe", "media_transcribe", "media_frames",
                                      "media_caption", "media_chunk")),
          "precondition: clip.mp4 has all five stage blocks")
    probe.run(root, ProbeConfig(), FIXED_TS, force=True, workers=1)
    after = load(asset)
    check(all(after.get(k) for k in ("media_transcribe", "media_frames", "media_caption", "media_chunk")),
          "forced re-probe preserves downstream stage blocks (additive)")
    check(after["media_metadata"].get("transcript_model") and after["media_metadata"].get("keyframe_count") is not None,
          "forced re-probe preserves downstream media_metadata keys (transcript_*/keyframe_*)")
    check(common.load_json(common.chunks_path(asset)), "chunks.json (own-lane sibling) untouched by re-probe")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "DataSet-09")
        make_corpus(root)
        test_pure_chunker()
        test_windowing()
        test_probe(root)
        test_probe_no_ffprobe()
        test_transcribe(root)
        test_transcribe_dryrun(root)
        test_audio_gating()
        test_frames(root)
        test_caption(root)
        test_chunk(root)
        test_additive_reprobe(root)
    print()
    if _failures:
        print(f"FAILED ({len(_failures)}):")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL MEDIA TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
