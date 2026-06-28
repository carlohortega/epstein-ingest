"""Synthetic staged corpus for the Stage-8 (ingest) offline tests: two datasets exercising the root-IMAGES
(DS-1..8) and VOL-IMAGES (DS-9..12) shapes; sidecars covering §4a outcomes — clean(embed), withhold (sexual/
violence/hate/self-harm/content-filter/unvetted), and CSAM-suspected (minor → whole-doc skip) — plus a blank
page; chunks.json; a tiny embed store missing one sha (to exercise the vectors_missing guard warning); and —
for the apply-package emitter (S8) — the merged-sidecar later-stage blocks (source/document/generator/
document_summary_final/per-page summary) and real PNG + ``*.emb.json`` files for the clean embedded assets so
image vectors + content_hash can be routed."""

from __future__ import annotations

import hashlib
import json
import os
import struct

MODEL = "text-embedding-3-small"
DIMS = 8
IMG_DIMS = 4
IMG_MODEL = "azure-ai-vision-multimodal"


def csha(prefix: str, text: str) -> str:
    return hashlib.sha256(f"{MODEL}\n{prefix}\n{text}".encode("utf-8")).hexdigest()


def _page(n, text, kind="text", visual=None):
    p = {"page_number": n, "page_kind": kind, "page_class": "full_page_visual",
         "text": {"content": text}, "summary": (text[:20] or None),
         "summary_json": {"key_points": [f"kp{n}"], "entities": [{"name": "Acme", "type": "org"}],
                          "topics": [f"topic{n}"]} if text else {},
         "text_safety": {"flag": "ok", "categories": []}}
    if visual is not None:
        p["has_visual_content"] = visual
    return p


def _asset(idx, **kw):
    a = {"index": idx, "kind": "artifact", "path": f"art/artifacts/a{idx}.png", "page_number": 1,
         "has_visual_content": True, "embedding_ref": f"art/embeddings/a{idx}.png.emb.json"}
    a.update(kw)
    return a


def _sidecar(rel_path, assets):
    return {"status": "ok",
            "generator": {"tool": "text-first-extract", "tool_version": "1.2.3",
                          "config": {"dpi": 200}, "generated_at_utc": "2026-06-28T00:00:00Z"},
            "source": {"filename": os.path.basename(rel_path), "relative_path": rel_path,
                       "bytes": 4096, "sha256": "f" * 64, "page_count": 2,
                       "pdf_producer": "Acrobat", "pdf_metadata": {"author": "X"}},
            "document": {"source_kind": "pdf", "content_type": "application/pdf",
                         "bates_begin": "AB-0001", "bates_end": "AB-0002", "title": "Memo"},
            "document_summary_final": {"text": "A short memo.", "final": True, "source": "fused",
                                       "covered_text_pages": 1, "covered_vision_assets": 1,
                                       "safety": {"flag": "ok", "content_filtered": False}, "no_content": False},
            "stage4": {"model": "gpt-5-mini"}, "stage5": {"model": "gpt-5-mini"},
            "pages": [_page(1, "Alpha bravo charlie delta echo foxtrot."),
                      _page(2, "", kind="blank", visual=False)],
            "image_assets": assets}


def _chunks(prefix, texts):
    return [{"text": "parent", "heading_path": [], "chunk_index": 0, "token_count": 10, "page_number": 1,
             "chunk_kind": "text",
             "children": [{"text": t, "context_prefix": prefix, "content_sha": csha(prefix, t),
                           "chunk_index": i, "heading_path": [], "token_count": 5, "chunk_kind": "text"}
                          for i, t in enumerate(texts)]}]


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)


def _write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def _write_asset_files(base_dir, asset, png_bytes):
    """Write a clean embedded asset's PNG + its ``*.emb.json`` sibling (so record.py can hash + load it)."""
    _write_bytes(os.path.join(base_dir, asset["path"].replace("/", os.sep)), png_bytes)
    _write(os.path.join(base_dir, asset["embedding_ref"].replace("/", os.sep)),
           {"embedding_model": IMG_MODEL, "model_version": "2023-04-15", "dim": IMG_DIMS,
            "vector": [0.1, 0.2, 0.3, 0.4]})


def _store(store_dir, vectors):
    os.makedirs(store_dir, exist_ok=True)
    with open(os.path.join(store_dir, "embed-store-manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"embedding_model": MODEL, "dimensions": DIMS, "record_bytes": 32 + 4 * DIMS}, f)
    handles = {}
    for sha, vec in vectors.items():
        h = handles.setdefault(sha[:2], open(os.path.join(store_dir, sha[:2] + ".f32v"), "ab"))
        h.write(bytes.fromhex(sha) + struct.pack(f"<{DIMS}f", *vec))
    for h in handles.values():
        h.close()


def build_ingest(root: str) -> dict:
    base77 = os.path.join(root, "DataSet-77", "VOL00077", "IMAGES", "0001")
    base66 = os.path.join(root, "DataSet-66", "IMAGES", "0001")
    os.makedirs(base77, exist_ok=True)
    os.makedirs(base66, exist_ok=True)

    # doc1 (clean): one review-only asset -> embeds (image vector), not withheld
    open(os.path.join(base77, "doc1.pdf"), "wb").close()
    a1 = _asset(0, safety={"flag": "review"})
    _write(os.path.join(base77, "doc1.json"), _sidecar("VOL00077/IMAGES/0001/doc1.pdf", [a1]))
    _write(os.path.join(base77, "doc1.chunks.json"), _chunks("p1", ["one", "two", "three"]))
    _write_asset_files(base77, a1, b"PNG-doc1-a0")

    # doc2 (WITHHELD): 6 assets — sexual, violence, hate, self_harm, content_filter, unvetted (NO minor)
    open(os.path.join(base77, "doc2.pdf"), "wb").close()
    _write(os.path.join(base77, "doc2.json"),
           _sidecar("VOL00077/IMAGES/0001/doc2.pdf", [
               _asset(0, safety={"explicit": True}),
               _asset(1, safety={"categories": ["violence"]},
                      content_safety={"categories": [{"category": "violence", "severity": 6}]}),
               _asset(2, safety={"categories": ["hate"]}),
               _asset(3, safety={"categories": ["self_harm"]}),
               _asset(4, content_filtered=True, safety={"flag": "review"}),
               _asset(5, safety=None, stage4_error={"code": "throttled"}),
           ]))
    _write(os.path.join(base77, "doc2.chunks.json"), _chunks("p2", ["aa", "bb"]))

    # doc3 (CSAM-suspected): minor_indicator -> WHOLE DOC not ingested (skip_csam)
    open(os.path.join(base77, "doc3.pdf"), "wb").close()
    _write(os.path.join(base77, "doc3.json"),
           _sidecar("VOL00077/IMAGES/0001/doc3.pdf", [_asset(0, safety={"minor_indicator": True})]))
    _write(os.path.join(base77, "doc3.chunks.json"), _chunks("p3", ["cc", "dd"]))

    # doc4 (DS-66, root-IMAGES, clean) -> embeds an image vector too
    open(os.path.join(base66, "doc4.pdf"), "wb").close()
    a4 = _asset(0, safety={"flag": "ok"})
    _write(os.path.join(base66, "doc4.json"), _sidecar("VOL00066/IMAGES/0001/doc4.pdf", [a4]))
    _write(os.path.join(base66, "doc4.chunks.json"), _chunks("p4", ["x", "y"]))
    _write_asset_files(base66, a4, b"PNG-doc4-a0")

    # stores: all shas except doc2's "bb" (doc3 skipped -> its shas don't matter)
    missing = csha("p2", "bb")
    shas77 = {csha("p1", t) for t in ("one", "two", "three")} | {csha("p2", t) for t in ("aa", "bb")}
    shas66 = {csha("p4", t) for t in ("x", "y")}
    _store(os.path.join(root, "DataSet-77", ".embeddings"), {s: [float(i) for i in range(DIMS)]
                                                             for s in (shas77 - {missing})})
    _store(os.path.join(root, "DataSet-66", ".embeddings"), {s: [1.0] * DIMS for s in shas66})

    img_hashes = {hashlib.sha256(b"PNG-doc1-a0").hexdigest(), hashlib.sha256(b"PNG-doc4-a0").hexdigest()}
    # present text vectors across the corpus: doc1(3) + doc2(aa only, bb missing) + doc4(2) = 6
    return {"all_shas": shas77, "missing_sha": missing,
            "present_text_vectors": 6, "image_hashes": img_hashes}
