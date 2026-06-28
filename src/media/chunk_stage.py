"""media_stage5 engine — chunk transcript + caption → ``<name>.chunks.json`` (HANDOFF-media-track §1/§5).

Reads the sidecar's ``media_transcribe`` windows and ``media_caption`` text and runs sv-kb's chunkers
(reused verbatim — see ``chunking.py``) to write the sibling ``<name>.chunks.json`` as the bare
``parents_to_dicts`` list S7/S8 consume. Combines:
  * transcript windows        → ``chunk_kind="transcript"``  (audio/video);
  * A/V caption + description  → ``chunk_kind="summary"``      (audio/video);
  * image caption + description→ ``chunk_kind="image_caption"`` (standalone image).

Local, deterministic, no API. Identifiers feed ``content_sha`` and are derived like Stage 6 (``--case-key``
/``--dataset-key`` override, else the ``VOLxxxxx`` / ``DataSet-NN`` path segments).
"""

from __future__ import annotations

import os
import re
import time

from . import CHUNK_TOOL_NAME, CHUNK_TOOL_VERSION
from . import common
from .config import ChunkConfig
from .chunking import transcript_parents, text_parents, serialize

_MANIFEST_NAME = "media-chunk-run-manifest.json"
_DATASET_RE = re.compile(r"(DataSet-\d+)", re.IGNORECASE)
_CASE_RE = re.compile(r"(VOL\d+)", re.IGNORECASE)


class IdentifierError(ValueError):
    """case_key/dataset_key neither overridden nor derivable — refuse to chunk (Stage 6 §1a.3 policy)."""


def derive_identifiers(asset: str, sidecar: dict, *, case_key: str, dataset_key: str) -> tuple[str, str, str]:
    norm = asset.replace("\\", "/")
    document_name = os.path.splitext(os.path.basename(norm))[0]   # stem, matching sv-kb's convention
    rel = ((sidecar.get("source") or {}).get("relative_path") or "").replace("\\", "/")
    if dataset_key:
        ds_val = dataset_key
    else:
        m = _DATASET_RE.search(norm)
        if not m:
            raise IdentifierError(f"no dataset_key override and no DataSet-NN segment: {asset}")
        ds_val = m.group(1)
    if case_key:
        case_val = case_key
    else:
        m = _CASE_RE.search(rel) or _CASE_RE.search(norm)
        if not m:
            raise IdentifierError(f"no case_key override and no VOLxxxxx segment: {asset}")
        case_val = m.group(1)
    return document_name, case_val, ds_val


def _caption_text(sc: dict) -> str:
    block = sc.get("media_caption") or {}
    parts = [(block.get("caption") or "").strip(), (block.get("description") or "").strip()]
    return "\n\n".join(p for p in parts if p)


def _gate(asset: str, *, force: bool):
    sc = common.load_json(common.sidecar_path(asset))
    if sc is None:
        return None, "no_sidecar"
    if sc.get("status") != "ok":
        return None, "not_ok"
    if not sc.get("media_transcribe") and not sc.get("media_caption"):
        return None, "nothing_to_chunk"
    if sc.get("media_chunk") and not force:
        return sc, "skipped_done"
    return sc, None


def process_asset(asset: str, kind: str, root: str, cfg: ChunkConfig, generated_at: str,
                  *, case_key: str, dataset_key: str, force: bool) -> dict:
    rel = common.rel_path(asset, root)
    sc, skip = _gate(asset, force=force)
    if skip is not None and sc is None:
        return _result(skip, rel, kind)
    if skip == "skipped_done":
        return _result(skip, rel, kind)

    try:
        document_name, case_val, ds_val = derive_identifiers(asset, sc, case_key=case_key,
                                                             dataset_key=dataset_key)
    except IdentifierError as exc:
        return _result("error", rel, kind, error_code="bad_identifiers", message=str(exc)[:300])

    ids = dict(case_key=case_val, dataset_key=ds_val, embedding_model=cfg.embedding_model)
    parent_lists = []

    block = sc.get("media_transcribe") or {}
    windows = block.get("windows") or []
    if windows:
        parent_lists.append(transcript_parents(windows, document_name,
                                               speaker_label=block.get("speaker_label"), **ids))

    caption_md = _caption_text(sc)
    if caption_md:
        caption_kind = "image_caption" if kind == "image" else "summary"
        parent_lists.append(text_parents(caption_md, caption_kind, document_name, **ids))

    chunks, counts = serialize(parent_lists)
    common.dump_json(chunks, common.chunks_path(asset))  # the bare parents_to_dicts list

    sc["media_chunk"] = {
        "tool": CHUNK_TOOL_NAME, "tool_version": CHUNK_TOOL_VERSION,
        "generated_at_utc": generated_at, "config": cfg.echo(),
        "embedding_model": cfg.embedding_model,
        "document_name": document_name, "case_key": case_val, "dataset_key": ds_val,
        "chunks_path": os.path.basename(common.chunks_path(asset)),
        "counts": counts,
    }
    common.dump_json(sc, common.sidecar_path(asset))
    return _result("chunked", rel, kind, parents=counts["parents"], children=counts["children"])


def _result(action, rel, kind, *, parents=0, children=0, error_code=None, message=None) -> dict:
    return {"action": action, "rel": rel, "kind": kind, "parents": parents, "children": children,
            "error_code": error_code, "message": message}


def run(root: str, cfg: ChunkConfig, generated_at: str, *, case_key: str = "", dataset_key: str = "",
        force: bool, workers: int | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    assets = common.find_media(root)  # all kinds: A/V (transcript+summary) and image (image_caption)
    total = len(assets)
    workers = workers or common.default_workers()

    actions = {"chunked": 0, "skipped_done": 0, "no_sidecar": 0, "not_ok": 0, "nothing_to_chunk": 0,
               "error": 0}
    agg = {"parents": 0, "children": 0}
    errors: list[dict] = []

    def consume(r: dict) -> None:
        actions[r["action"]] = actions.get(r["action"], 0) + 1
        if r["action"] == "chunked":
            agg["parents"] += r["parents"]
            agg["children"] += r["children"]
        elif r["action"] == "error":
            errors.append({"file": r["rel"], "code": r["error_code"], "message": r.get("message")})

    def worker(item):
        asset, kind = item
        return process_asset(asset, kind, root, cfg, generated_at, case_key=case_key,
                             dataset_key=dataset_key, force=force)

    common.drive(assets, worker, workers=workers, on_result=consume, total=total, label="assets",
                 started=started)

    manifest = {
        "tool": CHUNK_TOOL_NAME, "tool_version": CHUNK_TOOL_VERSION,
        "generated_at_utc": generated_at, "root": root,
        "assets_seen": total,
        "assets_chunked": actions["chunked"],
        "assets_skipped_already_done": actions["skipped_done"],
        "assets_no_sidecar": actions["no_sidecar"],
        "assets_nothing_to_chunk": actions["nothing_to_chunk"],
        "assets_errored": actions["error"],
        "parents": agg["parents"], "children": agg["children"],
        "errors": sorted(errors, key=lambda e: e["file"]),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    common.dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
