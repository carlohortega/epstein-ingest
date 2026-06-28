"""Media Stage 3 — frames / image vision + Content Safety (HANDOFF-media-track §3/§4).

Video keyframes (duration-adaptive, capped) and standalone images → 1024-d Azure AI Vision vectors
(``.emb.json``, content_hash-keyed) + Azure Content Safety verdicts (the CSAM net) + ``image_occurrences``
rows. High-risk imagery is redacted in place (not embedded, blob deleted). Appends a ``media_frames`` block.

Run:  python -m src.media_stage3 <ROOT> [flags]
"""
