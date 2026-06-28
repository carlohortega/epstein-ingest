"""Media Stage 4 — caption + description (HANDOFF-media-track §2/§4).

Generates the doc-level caption + description (the media analog of the PDF document_summary / Stage-4
image caption) via gpt-5-mini: video from transcript + sampled keyframes, audio from transcript,
standalone image with Stage-4-identical fields. Captures prompt/completion/cached tokens + USD. Appends a
``media_caption`` block; the text is chunked by media_stage5.

Run:  python -m src.media_stage4 <ROOT> [flags]
"""
