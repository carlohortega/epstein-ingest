"""Synthetic FROZEN sidecar corpus for the summary-scanner acceptance tests.

Two datasets exercise both real layouts and every progress/cost/safety case, plus the new S7 (embed) /
vision-embedding-coverage logic (Handoff §7):

  DataSet-77 (numbered: VOL00077/IMAGES/0001) —
    * text_done    — text-only, S2..S6 all present; chunks.json (2 children) fully in the embed store
    * vision_done  — vision, S2..S6 present; 2 has_visual assets BOTH embedded (embedding_ref set); 1 minor;
                     chunks.json (2 children) but only 1 in the store -> S7 NOT done for this doc
    * vision_gap   — vision, S2..S6 present BUT its has_visual asset has embedding_ref=null + 1 embed_error
                     -> the VISION-EMBEDDING GAP the dashboard must call out; chunks.json fully in store
    * text_mid     — S2 only, no S3 yet (in-progress; S3 projected-remaining > 0)
    * quar_doc     — ok, stage3.content_filtered=1 (quarantine candidate)
    * err_doc      — status=error (encrypted)

  DataSet-88 (flat: VOL00088/IMAGES, like DS-09) —
    * flat_done    — text-only fully done; chunks.json (2 children) fully in store; sibling render-dir decoys
    * no_sidecar   — a .pdf with NO .json (missing)
    * corrupt      — a .pdf with invalid-JSON .json (parse_error)

A corpus-global embed store (.embeddings/) is built with a subset of the children's content_shas so S7
store-aware coverage is exact and hand-checkable (see EXPECTED)."""

from __future__ import annotations

import json
import os
import struct

DS77 = "DataSet-77/VOL00077/IMAGES/0001"
DS88 = "DataSet-88/VOL00088/IMAGES"

# Deterministic fake content_shas (64 hex = 32 bytes). Prefixes vary so the store writes several shards.
def _sha(tag: str) -> str:
    h = "".join(f"{(ord(c) * 7 + i) % 16:x}" for i, c in enumerate((tag * 32)))
    return h[:64]

A1, A2 = _sha("textdoneA"), _sha("textdoneB")
V1, V2 = _sha("visiondoneA"), _sha("visiondoneB")
G1, G2 = _sha("visiongapA"), _sha("visiongapB")
F1, F2 = _sha("flatdoneA"), _sha("flatdoneB")

# What the embed store contains: text_done(both), vision_gap(both), flat_done(both), vision_done(ONLY V1).
STORE_SHAS = [A1, A2, G1, G2, F1, F2, V1]

EXPECTED = {
    "corpus_docs": 9,
    "ds77_docs_ok": 5, "ds77_docs_error": 1,
    "ds77_s3_applicable": 5, "ds77_s3_done": 4,     # text_mid has no S3
    "ds77_s4_applicable": 2, "ds77_s4_done": 2,     # vision_done + vision_gap
    "ds77_s5_done": 3, "ds77_s6_done": 3,
    "ds77_s7_applicable": 3, "ds77_s7_done": 2,     # text_done + vision_gap embedded; vision_done partial
    # vision-embedding coverage (ground truth = embedding_ref):
    "vision_has_visual": 3, "vision_embedded": 2, "vision_docs_gap": 1, "vision_embed_errors": 1,
    "total_cost_usd": 0.001 + 0.002 + 0.0009 + 0.001 + 0.0008 + 0.05,
    "quarantined": 2,        # quar_doc (content_filtered) + vision_done (minor asset)
}


def _base(rel, *, status="ok", pages=2, assets=None, kind="pdf", gen="2026-06-20T00:00:00Z"):
    sc = {
        "schema_version": "1.0",
        "status": status,
        "generator": {"tool": "text-first-extract", "generated_at_utc": gen},
        "document": {"source_kind": kind},
        "source": {"relative_path": rel, "page_count": pages, "bytes": 1000 * pages,
                   "filename": os.path.basename(rel)},
        "extraction_summary": {
            "total_text_chars": 500 * pages, "visible_chars": 480 * pages, "invisible_chars": 20 * pages,
            "scrubbed_chars": 0, "pages_text_sufficient": pages, "pages_need_vision": 0,
            "pages_redaction_flagged": 0, "pages_blank": 0, "routing_recommendation": "text",
        },
        "image_assets": assets or [],
    }
    if status == "error":
        sc["error"] = {"code": "encrypted"}
        sc["extraction_summary"] = {}
        sc["source"]["page_count"] = 0
    return sc


def _s2(sc):
    sc["stage2"] = {"counts": {"pages_rendered": 2, "artifacts": 0, "vision_workload": 0}}
    return sc


def _s3(sc, cost, *, content_filtered=0, gen="2026-06-20T01:00:00Z"):
    sc["stage3"] = {"generated_at_utc": gen,
                    "counts": {"pages_summarized": 2, "pages_errored": 0, "low_confidence": 0,
                               "safety_review": 0, "content_filtered": content_filtered},
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "calls": 3,
                              "estimated_cost_usd": cost}}
    return sc


def _s4(sc, cost, *, assets_count=1, embedded=1, embed_errors=0):
    sc["stage4"] = {"counts": {"assets": assets_count, "captioned": assets_count, "pregated_blank": 0,
                               "has_visual_content_true": assets_count, "content_filtered": 0,
                               "safety_review": 0, "content_safety_review": 0, "embedded": embedded,
                               "embed_errors": embed_errors, "errored": 0},
                    "usage": {"prompt_tokens": 5000, "completion_tokens": 800, "cached_tokens": 0,
                              "calls": 2, "estimated_cost_usd": cost}}
    return sc


def _s5(sc, source="promoted"):
    sc["stage5"] = {"usage": {"calls": 0, "estimated_cost_usd": 0.0}}
    sc["document_summary_final"] = {"source": source, "no_content": False, "safety": {"flag": "ok"}}
    return sc


def _s6(sc, parents=1, children=2):
    sc["stage6"] = {"counts": {"pages_chunked": 2, "parents": parents, "children": children,
                               "skipped_pages": 0}, "chunks_ref": "x.chunks.json"}
    return sc


def _asset(*, has_visual=True, embedding_ref=None, minor=False):
    return {"kind": "page", "has_visual_content": has_visual, "embedding_ref": embedding_ref,
            "safety": {"minor_indicator": minor, "explicit": False}}


def _write(root, rel, sidecar, *, chunk_shas=None):
    pdf = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    open(pdf, "wb").close()
    if sidecar is not None:
        with open(os.path.splitext(pdf)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump(sidecar, f, ensure_ascii=False, indent=2)
    if chunk_shas is not None:
        parents = [{"children": [{"content_sha": s} for s in chunk_shas]}]
        with open(os.path.splitext(pdf)[0] + ".chunks.json", "w", encoding="utf-8") as f:
            json.dump(parents, f)


def _write_raw(root, rel, raw_json):
    pdf = os.path.join(root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(pdf), exist_ok=True)
    open(pdf, "wb").close()
    with open(os.path.splitext(pdf)[0] + ".json", "w", encoding="utf-8") as f:
        f.write(raw_json)


def build_embed_store(corpus_root, shas=STORE_SHAS, dims=4):
    """Build a minimal Stage-7 vector store at <corpus_root>/.embeddings with the given content_shas, in the
    exact on-disk format src/embed/store.py writes (manifest + <xx>.f32v shards of digest+float32[dims])."""
    store = os.path.join(corpus_root, ".embeddings")
    os.makedirs(store, exist_ok=True)
    rec = 32 + 4 * dims
    with open(os.path.join(store, "embed-store-manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"embedding_model": "text-embedding-3-small", "dimensions": dims, "api_version": "2024-10-21",
                   "record_bytes": rec, "sha_bytes": 32, "float_dtype": "float32", "endianness": "little",
                   "created_at_utc": "2026-06-20T00:00:00Z"}, f)
    by_shard = {}
    for h in shas:
        by_shard.setdefault(h[:2], []).append(h)
    for pre, hs in by_shard.items():
        with open(os.path.join(store, pre + ".f32v"), "wb") as f:
            for h in hs:
                f.write(bytes.fromhex(h) + struct.pack(f"<{dims}f", *([0.0] * dims)))
    return store


def build_summary(root):
    # ---- DataSet-77 (numbered) ----
    _write(root, f"{DS77}/text_done.pdf",
           _s6(_s5(_s3(_s2(_base("VOL00077/IMAGES/0001/text_done.pdf")), 0.001))), chunk_shas=[A1, A2])
    _write(root, f"{DS77}/vision_done.pdf",
           _s6(_s5(_s4(_s3(_s2(_base("VOL00077/IMAGES/0001/vision_done.pdf",
                                     assets=[_asset(embedding_ref="e1", minor=True),
                                             _asset(embedding_ref="e2")])), 0.002),
                       0.05, assets_count=2, embedded=2), source="fused")), chunk_shas=[V1, V2])
    _write(root, f"{DS77}/vision_gap.pdf",
           _s6(_s5(_s4(_s3(_s2(_base("VOL00077/IMAGES/0001/vision_gap.pdf",
                                     assets=[_asset(embedding_ref=None)])), 0.0009),
                       0.0, assets_count=1, embedded=0, embed_errors=1), source="fused")), chunk_shas=[G1, G2])
    _write(root, f"{DS77}/text_mid.pdf", _s2(_base("VOL00077/IMAGES/0001/text_mid.pdf")))
    _write(root, f"{DS77}/quar_doc.pdf",
           _s3(_s2(_base("VOL00077/IMAGES/0001/quar_doc.pdf")), 0.001, content_filtered=1))
    _write(root, f"{DS77}/err_doc.pdf", _base("VOL00077/IMAGES/0001/err_doc.pdf", status="error"))

    # ---- DataSet-88 (flat, DS-09-like) ----
    _write(root, f"{DS88}/flat_done.pdf",
           _s6(_s5(_s3(_s2(_base("VOL00088/IMAGES/flat_done.pdf")), 0.0008))), chunk_shas=[F1, F2])
    _write(root, f"{DS88}/no_sidecar.pdf", None)
    _write_raw(root, f"{DS88}/corrupt.pdf", "{ this is : not json ]")
    # render-tree decoys the prune MUST skip
    _write(root, f"{DS88}/flat_done/decoy.pdf", _s3(_s2(_base("x")), 9.99))
    _write(root, f"{DS88}/flat_done/images/inner.pdf", _s3(_s2(_base("y")), 9.99))
