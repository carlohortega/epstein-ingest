"""Transcript + caption chunking — reuses sv-kb's ``chunk_transcript`` / ``chunk_markdown`` /
``parents_to_dicts`` VERBATIM (the fidelity core, like Stage 6 reuses ``chunk_pages``;
HANDOFF-media-track §1/§2). Do NOT fork the chunker — its ``content_sha``
(``sha256(model\\ncontext_prefix\\ntext)`` over a contextual prefix) is what lets S7 dedup media text
against PDF text.

Output is the bare ``parents_to_dicts(...)`` LIST that ``src/embed`` (S7) consumes via
``parents_from_dicts`` — combining, with continuous indices and the reserved sv-kb ``chunk_kind``s:
  * ``transcript``    — A/V transcript windows;
  * ``summary``       — A/V doc caption + description (media_stage4);
  * ``image_caption`` — standalone-image caption + description (media_stage4).

``transcript_markdown`` / ``transcript_vtt`` / ``content_sha`` are stdlib and kept here so
``media_stage2`` writes display artifacts without importing sv-kb.
"""

from __future__ import annotations

import hashlib
import os
import sys


def _import_svkb():
    """Import sv-kb's chunkers. Mirrors ``src/stage6/chunk._import_svkb`` (try the package, else add
    ``$SVKB_SHARED`` / the WSL path / the Windows UNC path to sys.path and retry)."""
    try:
        from svkb_pipeline.chunking import chunk_transcript, chunk_markdown, parents_to_dicts
        return chunk_transcript, chunk_markdown, parents_to_dicts
    except ModuleNotFoundError:
        pass
    for cand in (
        os.environ.get("SVKB_SHARED"),
        os.path.expanduser("~/repos/sv-kb/pipeline/shared"),
        r"\\wsl.localhost\Ubuntu-22.04\home\cortega\repos\sv-kb\pipeline\shared",
    ):
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    from svkb_pipeline.chunking import chunk_transcript, chunk_markdown, parents_to_dicts
    return chunk_transcript, chunk_markdown, parents_to_dicts


def content_sha(model: str, context_prefix: str, text: str) -> str:
    """sv-kb's recipe: ``sha256(model\\ncontext_prefix\\ntext)`` — kept for tests/verification."""
    return hashlib.sha256(f"{model}\n{context_prefix}\n{text}".encode("utf-8")).hexdigest()


def transcript_parents(windows: list[dict], document_name: str, *, case_key: str, dataset_key: str,
                       speaker_label: str | None, embedding_model: str):
    """sv-kb ``chunk_transcript`` over sidecar window dicts → list[ParentChunk] (chunk_kind=transcript)."""
    chunk_transcript, _chunk_markdown, _ = _import_svkb()
    tuples = [(float(w.get("start_s") or 0.0), float(w.get("end_s") or 0.0), w.get("text") or "")
              for w in windows]
    return chunk_transcript(tuples, document_name=document_name, case_key=case_key,
                            dataset_key=dataset_key, speaker_label=speaker_label,
                            embedding_model=embedding_model)


def text_parents(text: str, chunk_kind: str, document_name: str, *, case_key: str, dataset_key: str,
                 embedding_model: str):
    """sv-kb ``chunk_markdown`` over a caption/description blob, re-tagged with ``chunk_kind`` (the
    reserved ``summary`` / ``image_caption`` kinds) — the same pattern ``chunk_transcript`` uses."""
    _ct, chunk_markdown, _ = _import_svkb()
    text = (text or "").strip()
    if not text:
        return []
    parents = chunk_markdown(text, document_name=document_name, case_key=case_key,
                             dataset_key=dataset_key, embedding_model=embedding_model)
    for p in parents:
        p.chunk_kind = chunk_kind
        for c in p.children:
            c.chunk_kind = chunk_kind
    return parents


def serialize(parent_lists: list) -> tuple[list[dict], dict]:
    """Reindex parents continuously across the combined lists and serialize via ``parents_to_dicts``.
    Returns (chunks_list, counts) where counts breaks children down by ``chunk_kind``."""
    _ct, _cm, parents_to_dicts = _import_svkb()
    combined = [p for lst in parent_lists for p in lst]
    for i, p in enumerate(combined):
        p.chunk_index = i
    chunks = parents_to_dicts(combined)
    by_kind: dict[str, int] = {}
    children = 0
    for p in chunks:
        for c in (p.get("children") or []):
            children += 1
            by_kind[c["chunk_kind"]] = by_kind.get(c["chunk_kind"], 0) + 1
    return chunks, {"parents": len(chunks), "children": children, "by_kind": by_kind}


def build_chunks(windows: list[dict], document_name: str, *, case_key: str, dataset_key: str,
                 speaker_label: str | None = None,
                 embedding_model: str = "text-embedding-3-small") -> tuple[list[dict], dict]:
    """Transcript-only convenience (kept for tests): windows → bare chunks list + counts."""
    return serialize([transcript_parents(windows, document_name, case_key=case_key,
                                         dataset_key=dataset_key, speaker_label=speaker_label,
                                         embedding_model=embedding_model)])


def transcript_markdown(windows: list[dict]) -> str:
    """Render windows to the ``## [hh:mm:ss]`` heading + text markdown (the ``transcript.md`` display
    blob; same heading shape sv-kb's ``chunk_transcript`` builds internally)."""
    from .common import hms
    lines: list[str] = []
    for w in windows:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"## [{hms(float(w.get('start_s') or 0))}]")
        lines.append("")
        lines.append(text)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcript_vtt(windows: list[dict]) -> str:
    """Render windows to WebVTT (window-level cues)."""
    from .common import vtt_ts
    out = ["WEBVTT", ""]
    for w in windows:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        out.append(f"{vtt_ts(float(w.get('start_s') or 0))} --> {vtt_ts(float(w.get('end_s') or 0))}")
        out.append(text)
        out.append("")
    return "\n".join(out).rstrip() + "\n"
