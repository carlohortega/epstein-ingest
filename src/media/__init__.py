"""Media track — non-PDF asset processing (audio / video / standalone images).

A parallel set of *producer* stages that emit the **same downstream contract** as the PDF track
(sidecar JSON + ``<name>.chunks.json`` + 1024-d image vectors + blobs + ``media_metadata``) so the
shared ``embed`` (S7) and ``ingest`` (S8) tail consumes both tracks uniformly. See
``docs/HANDOFF-media-track.md``.

``src.media`` is the shared *engine* (stdlib-only core; the Azure clients import ``requests`` lazily so
the package imports without it). The four runnable stages are thin packages that drive this engine:

- :mod:`src.media_stage1` — probe / register     (``ffprobe`` + extension → sidecar + media_metadata)
- :mod:`src.media_stage2` — transcribe (A/V)      (ffmpeg segment → gpt-4o-mini-transcribe per window)
- :mod:`src.media_stage3` — frames / image vision (video keyframes + 1024-d image embeds + Content Safety)
- :mod:`src.media_stage4` — chunk                 (``chunk_transcript`` → ``<name>.chunks.json``)

This mirrors ``src/stage1..3``: each stage appends an own block to the asset's sidecar, is
idempotent/resumable, fault-isolated, and writes a per-stage run manifest.
"""

SCHEMA_VERSION = "1.0"

# Per-stage tool identity (mirrors stage1/2/3 TOOL_NAME/TOOL_VERSION provenance).
PROBE_TOOL_NAME = "media-probe"
PROBE_TOOL_VERSION = "1.0.0"
TRANSCRIBE_TOOL_NAME = "media-transcribe"
TRANSCRIBE_TOOL_VERSION = "1.0.0"
FRAMES_TOOL_NAME = "media-frames"
FRAMES_TOOL_VERSION = "1.0.0"
CAPTION_TOOL_NAME = "media-caption"
CAPTION_TOOL_VERSION = "1.0.0"
CHUNK_TOOL_NAME = "media-chunk"
CHUNK_TOOL_VERSION = "1.0.0"

# Mirrors svkb models.SOURCE_KINDS for the media track's subset.
SOURCE_KINDS = ("audio", "video", "image")

__all__ = [
    "SCHEMA_VERSION",
    "PROBE_TOOL_NAME", "PROBE_TOOL_VERSION",
    "TRANSCRIBE_TOOL_NAME", "TRANSCRIBE_TOOL_VERSION",
    "FRAMES_TOOL_NAME", "FRAMES_TOOL_VERSION",
    "CAPTION_TOOL_NAME", "CAPTION_TOOL_VERSION",
    "CHUNK_TOOL_NAME", "CHUNK_TOOL_VERSION",
    "SOURCE_KINDS",
]
