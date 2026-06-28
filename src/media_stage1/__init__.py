"""Media Stage 1 — probe / register (HANDOFF-media-track §5).

Walks a folder for non-PDF media (audio/video/standalone images — the PDF ``IMAGES`` tree is excluded),
runs ``ffprobe`` per asset, and writes a sidecar ``<name>.media.json`` with typed ``media_metadata`` +
the raw ffprobe bag. 100% local, deterministic, no network.

Run:  python -m src.media_stage1 <ROOT> [flags]
"""
