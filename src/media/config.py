"""Per-stage tunable knobs for the media track (HANDOFF-media-track §2/§3/§8) in one place.

Every field is echoed verbatim into the relevant sidecar block's ``config`` so a JSON is
self-describing and a run is reproducible from its own output (mirrors stage1 ``generator.config`` /
stage2 ``stage2.config`` / stage3 ``stage3.config``). NOT here: the Azure connection (endpoint /
deployment / api-version / KEY) — those live in ``connection.py`` and the KEY is never echoed.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class ProbeConfig:
    """media_stage1 — probe / register (local, deterministic, no API)."""
    ffprobe_bin: str = "ffprobe"             # provenance; real value resolves from FFPROBE_BIN/PATH
    probe_timeout_seconds: float = 120.0
    # large-run robustness (mirrors stage1 Config)
    heartbeat_seconds: int = 60

    def echo(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TranscribeConfig:
    """media_stage2 — transcribe A/V (the premium line; mirrors Stage 3's limiter/retry shape)."""
    # --- model / provenance (HANDOFF §2) ---
    model: str = "gpt-4o-mini-transcribe"    # informational; live deployment NAME comes from env
    language: str | None = None              # optional ISO-639-1 hint passed to the API ("en"); None = auto
    # --- windowing + ffmpeg segmentation (HANDOFF §2) ---
    window_seconds: int = 120                # one TranscriptWindow per 120s window; timestamps are window-level
    sample_rate: int = 16000                 # mono 16 kHz 64 kbps mp3 (-vn) per sv-kb transcribe.py
    channels: int = 1
    bitrate_kbps: int = 64
    ffmpeg_bin: str = "ffmpeg"               # provenance; real value resolves from FFMPEG_BIN/PATH
    segment_timeout_seconds: float = 3600.0  # ffmpeg segmentation of a long video can take a while
    # --- throughput / rate limiting (HANDOFF §8; mirrors Stage 3) ---
    concurrency: int = 4                     # bounded in-flight assets (each fans out window calls)
    window_concurrency: int = 4             # in-flight transcription calls per asset
    requests_per_minute: int = 0             # 0 = no client RPM cap (rely on server 429 + backoff)
    tokens_per_minute: int = 0               # unused for audio (kept for limiter symmetry)
    max_retries: int = 6
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    request_timeout_seconds: float = 300.0   # audio uploads are larger than a chat call
    # --- audio-aware gating (cut waste on long/silent surveillance footage) ---
    skip_no_audio: bool = True               # no audio stream (from probe) → skip transcription entirely
    skip_silent: bool = True                 # silencedetect: skip windows with no sound (and fully-silent files)
    silence_noise_db: float = -30.0          # silencedetect noise floor (dBFS); below this = silence
    silence_min_seconds: float = 2.0         # min silence run to count (silencedetect d=)
    silence_timeout_seconds: float = 3600.0  # the silencedetect decode pass on a long video
    # --- cost accounting (HANDOFF §8) — transcription scales with AUDIO MINUTES, not file count ---
    cost_per_audio_minute: float = 0.003     # gpt-4o-mini-transcribe ~list; used for dry-run projection
    poster_max_px: int = 480                 # audio waveform/poster longest side (best-effort)

    def echo(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FramesConfig:
    """media_stage3 — video keyframes + image vision + Content Safety (HANDOFF §3)."""
    # --- keyframe sampling: CONTENT-AWARE (scene-change) by default, with a per-video CAP + FLOOR ---
    keyframe_mode: str = "scene"             # "scene" = sample on visual change (boring static video → few
    #                                          frames); "interval" = legacy duration-adaptive time grid
    scene_threshold: float = 0.4             # ffmpeg scene score (0..1); higher = only bigger changes
    scene_downscale_px: int = 320            # detect scene changes on a downscaled decode (speed)
    keyframe_min_frames: int = 2             # FLOOR: a fully-static video still gets this many (≈poster set)
    keyframe_max_frames: int = 120           # CEILING: per-video cap on EMBEDDED keyframes
    keyframe_base_seconds: float = 10.0      # "interval" mode only: never finer than this
    frame_max_px: int = 480                  # longest side of each WebP frame
    frame_quality: int = 80                  # ffmpeg -q:v for WebP
    # --- safety / retrieval decoupling (HANDOFF §3): scan on change + a coarse floor, embed sparsely ---
    # In scene mode, Content-Safety = the scene-change frames (scanned during embed; a new face/object IS a
    # scene change) PLUS a COARSE floor grid as a backstop — so safety cost scales with content, not with
    # video length (a 10h static video no longer costs ~1800 CS scans). "interval" mode keeps the old dense
    # time grid.
    safety_floor_seconds: float = 120.0      # scene mode: coarse safety backstop grid between scene changes
    safety_scan_interval_seconds: float = 2.0  # interval mode: dense Content-Safety grid
    safety_max_frames: int = 1800            # cap on safety-scan frames (either mode)
    # --- image vision (1024-d, same staged format as Stage 4) ---
    vision_model: str = "azure-ai-vision-multimodal"
    vision_dim: int = 1024
    # --- Content Safety severity gates (HANDOFF §3 / Stage-8 redaction; 0/2/4/6 scale) ---
    safety_block_severity: int = 4           # >= this in any category => high-risk => block (redact at S8)
    safety_review_severity: int = 2          # >= this (and < block) => review
    # --- throughput / retry (mirrors Stage 3) ---
    concurrency: int = 4                     # bounded in-flight assets
    frame_concurrency: int = 8              # in-flight vision/safety calls per asset
    max_retries: int = 6
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    request_timeout_seconds: float = 120.0
    ffmpeg_bin: str = "ffmpeg"

    def echo(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CaptionConfig:
    """media_stage4 — doc-level caption + description (gpt-5-mini), the media analog of the PDF
    document_summary / Stage-4 image caption. Reuses Stage 3's token-metered chat client, so this is the
    media track's token+USD cost line (HANDOFF-media-track §2/§8)."""
    model: str = "gpt-5-mini"               # informational; live deployment NAME comes from env
    param_style: str = "auto"               # auto → reasoning shape for gpt-5/o-series (see Stage 3)
    reasoning_effort: str = "minimal"       # keep caption calls cheap/fast
    max_output_tokens: int = 700
    # --- inputs (cost levers) ---
    max_transcript_chars: int = 12_000      # transcript excerpt fed to the caption model
    max_keyframes: int = 8                  # # of (safe, embedded) keyframes sent to the vision model
    # --- throughput / retry (mirrors Stage 3) ---
    concurrency: int = 4
    max_retries: int = 6
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    request_timeout_seconds: float = 120.0
    # --- cost accounting (gpt-5-mini; approximate list, configurable) ---
    input_cost_per_million: float = 0.25
    cached_input_cost_per_million: float = 0.025
    output_cost_per_million: float = 2.0

    def echo(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ChunkConfig:
    """media_stage4 — chunk transcript → <name>.chunks.json (deterministic, local)."""
    embedding_model: str = "text-embedding-3-small"  # baked into content_sha so S7 dedups vs PDF text
    max_child_chars: int = 1200              # split a long window into children no larger than this
    max_parent_chars: int = 4000             # group children into parents up to this size
    chunk_overlap_chars: int = 0             # transcript windows are already coherent; no overlap by default

    def echo(self) -> dict:
        return asdict(self)
