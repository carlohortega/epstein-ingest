"""Per-document Stage-6 building blocks: the sidecar gate (Handoff §6) and ``finalize_document`` which
chunks the doc (reusing sv-kb's chunker via ``chunk.py``), writes the sibling ``<name>.chunks.json``, and
extends the sidecar APPEND-ONLY with a new ``stage6`` block (§5) — never touching any Stage 1–5 field.

Transport-agnostic: the scheduler (walk.py) runs ``process_document_sync`` across a thread pool (one task
per document); the same function runs the workers<=1 path and the tests. No model client, no API calls —
chunking is deterministic + local. A bad doc / chunk error is isolated to its own task.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json, sidecar_path
from . import (STAGE6_TOOL_NAME, STAGE6_TOOL_VERSION, EMBEDDING_MODEL, BRIEF_DESCRIPTION,
               TEXT_SOURCE, IMAGE_PAGE_POLICY, FIDELITY_NOTES)
from .chunk import chunk_document, CHUNKER_PARAMS, IdentifierError
from .config import Stage6Config


def _result(action: str, rel: str, **kw) -> dict:
    base = {"action": action, "rel": rel, "error_code": None, "message": None,
            "pages_chunked": 0, "parents": 0, "children": 0, "skipped_pages": 0, "empty": False}
    base.update(kw)
    return base


def chunks_path(pdf_path: str) -> str:
    """Sibling artifact path: ``<stem>.chunks.json`` next to the PDF/sidecar (the §5/§6 staged file)."""
    return os.path.splitext(pdf_path)[0] + ".chunks.json"


def open_sidecar(pdf_path: str, root: str, *, force: bool):
    """Gate (Handoff §6). Returns (rel, sidecar_or_None, skip_result_or_None): when the third value is set
    the document is skipped/errored and recorded; otherwise proceed with the returned sidecar.

        status != ok                        -> skip (skipped_status)
        no stage1 pages                     -> skip (no_stage1)    # base extraction not present
        stage6 present and not --force      -> skip (skipped_done) # idempotency
    Stage 3/4/5 are NOT data dependencies (brief_description='' and the decided skip-no-text image policy
    mean Stage 6 reads only Stage-1 page text); running after Stage 5 is an ordering convention only."""
    rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
    sc = sidecar_path(pdf_path)
    try:
        with open(sc, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
    except FileNotFoundError:
        return rel, None, _result("no_sidecar", rel)
    except Exception as exc:
        return rel, None, _result("error", rel, error_code="bad_sidecar",
                                  message=f"{type(exc).__name__}: {exc}")
    if sidecar.get("status") != "ok":
        return rel, None, _result("skipped_status", rel)
    if not sidecar.get("pages"):
        return rel, None, _result("no_stage1", rel)
    if sidecar.get("stage6") and not force:
        return rel, None, _result("skipped_done", rel)
    return rel, sidecar, None


def _write_chunks_artifact(pdf_path: str, chunks_json: list[dict]) -> str | None:
    """Write the sibling ``<name>.chunks.json`` atomically (the live-contract artifact). For a doc with no
    chunkable content we write NO file (and clear a stale one from a prior run) and return ``None`` so the
    ``stage6.chunks_ref`` is null — avoids littering empty artifacts across image-only/blank docs (§6).
    Serialized exactly like sv-kb's ``consolidate.py`` (``json.dumps(...).encode("utf-8")``) so the bytes
    match what the live indexer-job would have produced."""
    target = chunks_path(pdf_path)
    if not chunks_json:
        if os.path.exists(target):
            os.remove(target)
        return None
    tmp = target + ".tmp"
    with open(tmp, "wb") as f:
        f.write(json.dumps(chunks_json).encode("utf-8"))
    os.replace(tmp, target)
    return os.path.basename(target)


def _write_stage6_block(sidecar: dict, cfg: Stage6Config, identifiers: dict, counts: dict,
                        chunks_ref: str | None, *, generated_at: str) -> None:
    """The per-doc ``stage6`` provenance block (Handoff §5). Carries the chunker source/params (read live
    from the reused chunker), the content_sha inputs (embedding_model / brief_description / identifiers),
    the decided fidelity calls, the sibling artifact ref, and this document's chunk counts."""
    sidecar["stage6"] = {
        "tool": STAGE6_TOOL_NAME,
        "tool_version": STAGE6_TOOL_VERSION,
        "chunker_source": "svkb_pipeline.chunking.chunk_pages",
        "chunker_params": CHUNKER_PARAMS,
        "embedding_model": EMBEDDING_MODEL,
        "brief_description": BRIEF_DESCRIPTION,
        "text_source": TEXT_SOURCE,
        "image_page_policy": IMAGE_PAGE_POLICY,
        "fidelity_notes": FIDELITY_NOTES,
        "identifiers": identifiers,
        "generated_at_utc": generated_at,
        "config": cfg.echo(),
        "chunks_ref": chunks_ref,
        "counts": counts,
    }


def finalize_document(rel: str, pdf_path: str, sc_path: str, sidecar: dict, cfg: Stage6Config, *,
                      generated_at: str) -> dict:
    """Chunk the document, write the sibling artifact + the append-only ``stage6`` block. Fault-isolated
    (Handoff §10.6): an IdentifierError (path can't yield case/dataset key) leaves NO ``stage6`` block so a
    re-run retries after the path is fixed, and is recorded. A doc with no chunkable content still
    finalizes with ``parents=0`` (and ``chunks_ref:null``) so it is marked done, not retried. Returns the
    manifest result dict."""
    try:
        chunks_json, identifiers, counts = chunk_document(pdf_path, sidecar)
    except IdentifierError as exc:
        return _result("error", rel, error_code="no_identifiers", message=str(exc)[:300])

    chunks_ref = _write_chunks_artifact(pdf_path, chunks_json)
    _write_stage6_block(sidecar, cfg, identifiers, counts, chunks_ref, generated_at=generated_at)
    dump_json(sidecar, sc_path)

    return _result("chunked", rel, parents=counts["parents"], children=counts["children"],
                   pages_chunked=counts["pages_chunked"], skipped_pages=counts["skipped_pages"],
                   empty=(counts["parents"] == 0))


def process_document_sync(pdf_path: str, root: str, cfg: Stage6Config, generated_at: str, *,
                          force: bool) -> dict:
    """One document, start to finish. Used for workers<=1, the scheduler's per-doc task, and the tests.
    Self-contained: opens, gates, chunks, and writes its own sidecar + sibling artifact."""
    rel, sidecar, skip = open_sidecar(pdf_path, root, force=force)
    if skip is not None:
        return skip
    return finalize_document(rel, pdf_path, sidecar_path(pdf_path), sidecar, cfg,
                             generated_at=generated_at)
