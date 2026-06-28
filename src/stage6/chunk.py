"""Stage-6 domain logic, independent of transport so the scheduler (walk.py) and the offline tests share
it: importing sv-kb's chunker VERBATIM, deriving the content_sha identifiers from the path, assembling the
page-ordered ``(page_number, md)`` input exactly like sv-kb's ``consolidate.py``, and calling
``chunk_pages`` + ``parents_to_dicts``.

There is NO model client here (Stage 6 makes no API calls). The only external dependency is the sv-kb
chunker, which is pure stdlib (``hashlib``/``re``) and is reused unchanged — do not fork it (Handoff §1).
"""

from __future__ import annotations

import os
import re
import sys


def _import_svkb():
    """Import sv-kb's chunker. The launcher (run_stage6.py) puts ``~/repos/sv-kb/pipeline/shared`` on
    ``sys.path``; for ``python -m src.stage6`` and the tests we add it here too (WSL path, the Windows UNC
    path, or ``$SVKB_SHARED``), so the import resolves either way without forcing every caller to set it."""
    try:
        from svkb_pipeline.chunking import chunk_pages, parents_to_dicts  # noqa: F401
        import svkb_pipeline.chunking as chunking
        return chunking, chunk_pages, parents_to_dicts
    except ModuleNotFoundError:
        pass
    for cand in (
        os.environ.get("SVKB_SHARED"),
        os.path.expanduser("~/repos/sv-kb/pipeline/shared"),
        r"\\wsl.localhost\Ubuntu-22.04\home\cortega\repos\sv-kb\pipeline\shared",
    ):
        if cand and os.path.isdir(cand) and cand not in sys.path:
            sys.path.insert(0, cand)
    from svkb_pipeline.chunking import chunk_pages, parents_to_dicts
    import svkb_pipeline.chunking as chunking
    return chunking, chunk_pages, parents_to_dicts


_chunking, chunk_pages, parents_to_dicts = _import_svkb()

# Read live from the reused chunker so they can never drift from the values that actually shaped the output
# (forking the budgets is forbidden — Handoff §1/§3). Stamped into every stage6 block.
CHUNKER_PARAMS = {
    "parent_target": _chunking.PARENT_TARGET_TOKENS,
    "parent_max": _chunking.PARENT_MAX_TOKENS,
    "child_target": _chunking.CHILD_TARGET_TOKENS,
    "child_max": _chunking.CHILD_MAX_TOKENS,
}

# Identifiers feed content_sha (Handoff §1a.3), so they are derived from the path the same way the live
# IngestionEvent sets them: dataset_key = the ``DataSet-NN`` path segment, case_key = the ``VOLxxxxx``
# segment (also the first segment of the sidecar's ``source.relative_path``), document_name = the PDF stem.
_DATASET_RE = re.compile(r"(DataSet-\d+)", re.IGNORECASE)
_CASE_RE = re.compile(r"(VOL\d+)", re.IGNORECASE)


class IdentifierError(ValueError):
    """Raised when case_key/dataset_key cannot be derived from the path. We refuse to chunk rather than
    emit chunks with a wrong/blank key, which would silently make every content_sha distinguishable and
    break the shared embedding cache/dedup (Handoff §1a.3)."""


def derive_identifiers(pdf_path: str, sidecar: dict, *, case_key: str = "",
                       dataset_key: str = "") -> tuple[str, str, str]:
    """(document_name, case_key, dataset_key) for the content_sha. ``document_name`` = PDF stem. ``case_key``
    and ``dataset_key`` use the EXPLICIT override when given (the live sv-kb convention — e.g. case_key
    ``epstein``, which is a corpus constant, NOT the volume); otherwise they fall back to path derivation
    (legacy: dataset_key = the ``DataSet-NN`` segment, case_key = the ``VOLxxxxx`` segment). These feed
    content_sha, so a wrong value makes every chunk distinguishable — pass ``--case-key`` for real runs.
    Raises IdentifierError if a key is neither overridden nor derivable."""
    norm = pdf_path.replace("\\", "/")
    document_name = os.path.splitext(os.path.basename(norm))[0]
    rel = ((sidecar.get("source") or {}).get("relative_path") or "").replace("\\", "/")

    if dataset_key:
        ds_val = dataset_key
    else:
        m = _DATASET_RE.search(norm)
        if not m:
            raise IdentifierError(f"no dataset_key override and no DataSet-NN segment in path: {pdf_path}")
        ds_val = m.group(1)

    if case_key:
        case_val = case_key
    else:
        m = _CASE_RE.search(rel) or _CASE_RE.search(norm)
        if not m:
            raise IdentifierError(f"no case_key override and no VOLxxxxx segment in path: {pdf_path}")
        case_val = m.group(1)

    return document_name, case_val, ds_val


def build_pages(sidecar: dict) -> tuple[list[tuple[int, str]], int]:
    """Assemble the page-ordered ``(page_number, md)`` list fed to ``chunk_pages``, replicating sv-kb's
    ``consolidate.py`` text-layer path (Handoff §1a.1/§2): each page that HAS a text layer becomes
    ``(page_number, "## Transcription\n\n<raw page text>")``. Pages with no text (blank/redacted/scanned
    -> empty ``text.content``) are SKIPPED per the decided IMAGE_PAGE_POLICY (we never inject captions —
    sv-kb wouldn't). Returns (pages, skipped_page_count). Pages are emitted in ``page_number`` order."""
    from . import page_md

    pages: list[tuple[int, str]] = []
    skipped = 0
    rows = sorted((sidecar.get("pages") or []),
                  key=lambda p: (p.get("page_number") is None, p.get("page_number") or 0))
    for p in rows:
        content = ((p.get("text") or {}).get("content")) or ""
        if content.strip():
            pages.append((p.get("page_number"), page_md(content)))
        else:
            skipped += 1
    return pages, skipped


def chunk_document(pdf_path: str, sidecar: dict, *, case_key: str = "",
                   dataset_key: str = "") -> tuple[list[dict], dict, dict]:
    """Chunk one document. Returns (chunks_json, identifiers, counts) where:
      * chunks_json = ``parents_to_dicts(chunk_pages(...))`` — the exact bytes the live indexer consumes;
      * identifiers = {document_name, case_key, dataset_key};
      * counts = {pages_chunked, parents, children, skipped_pages}.
    ``case_key``/``dataset_key`` override the path derivation (see ``derive_identifiers``). Raises
    IdentifierError if a key can't be resolved (caller records it as an error, does not finalize)."""
    from . import EMBEDDING_MODEL, BRIEF_DESCRIPTION

    document_name, case_key, dataset_key = derive_identifiers(
        pdf_path, sidecar, case_key=case_key, dataset_key=dataset_key)
    pages, skipped = build_pages(sidecar)

    parents = chunk_pages(
        pages,
        document_name=document_name,
        case_key=case_key,
        dataset_key=dataset_key,
        brief_description=BRIEF_DESCRIPTION,        # "" — the sv-kb default (NOT the Stage-5 summary)
        embedding_model=EMBEDDING_MODEL,            # text-embedding-3-small — the sv-kb default
    )
    chunks_json = parents_to_dicts(parents)
    counts = {
        "pages_chunked": len(pages),
        "parents": len(chunks_json),
        "children": sum(len(c.get("children") or []) for c in chunks_json),
        "skipped_pages": skipped,
    }
    identifiers = {"document_name": document_name, "case_key": case_key, "dataset_key": dataset_key}
    return chunks_json, identifiers, counts
