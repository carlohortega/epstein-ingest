"""Media Stage 2 — transcribe A/V (HANDOFF-media-track §2/§5).

ffmpeg-segments each audio/video asset's audio track and transcribes every window with Azure OpenAI
``gpt-4o-mini-transcribe``, writing transcript.md/.vtt blobs and a ``media_transcribe`` block (with the
windows Stage 4 chunks). The premium line: cost ∝ audio minutes — run ``--dry-run`` first to project it.

Run:  python -m src.media_stage2 <ROOT> [flags]
"""
