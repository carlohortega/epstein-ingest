"""Media Stage 5 — chunk transcript + caption → ``<name>.chunks.json`` (HANDOFF-media-track §1/§5).

Reads the ``media_transcribe`` windows and ``media_caption`` text and runs sv-kb's chunkers (verbatim) to
emit the bare ``parents_to_dicts`` list the shared embed (S7) + ingest (S8) tail consumes from the PDF
track — combining transcript (``transcript``), A/V caption (``summary``), and image caption
(``image_caption``) chunk kinds. Local, deterministic, no network.

Run:  python -m src.media_stage5 <ROOT> [flags]
"""
