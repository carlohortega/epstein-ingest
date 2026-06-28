"""Stage-8 (ingest) OFFLINE acceptance tests — no DB, no Azure, no PyMuPDF. Covers the safe core: per-dataset
IMAGES discovery (root + VOL shapes), the §4a safety/blank gate, the vector-store reader, the per-document
plan, the dry-run write-plan projection, and the read-only invariant. The live writers are gated (load.py
raises) and not exercised here.

Run:  python tests/run_ingest_tests.py     (project root + sv-kb shared on PYTHONPATH)
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ingest.config import IngestConfig                                  # noqa: E402
from src.ingest import discover, safety                                     # noqa: E402
from src.ingest.store_reader import VectorStore                             # noqa: E402
from src.ingest.plan import plan_document                                   # noqa: E402
from src.ingest.walk import dry_run                                         # noqa: E402
from src.ingest.emit import prepare                                         # noqa: E402
from tests.make_ingest_fixtures import build_ingest, DIMS, csha             # noqa: E402

_fail: list[str] = []
def check(c, m): print(f"  [{'PASS' if c else 'FAIL'}] {m}"); (None if c else _fail.append(m))


def _hashes(root):
    out = {}
    for dp, _d, files in os.walk(root):
        if os.sep + ".embeddings" in dp:
            continue
        for fn in files:
            if fn.endswith(".json") and not fn.startswith("ingest-"):
                p = os.path.join(dp, fn)
                out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def main() -> int:
    cfg = IngestConfig(dimensions=DIMS)
    root = tempfile.mkdtemp(prefix="tfe_s8_")
    facts = build_ingest(root)
    try:
        before = _hashes(root)

        print("\n== discovery: locate IMAGES per dataset (root + VOL shapes) ==")
        d77 = discover.find_images_dirs(os.path.join(root, "DataSet-77"))
        d66 = discover.find_images_dirs(os.path.join(root, "DataSet-66"))
        check(len(d77) == 1 and d77[0].endswith(os.path.join("VOL00077", "IMAGES")), "DS-77 IMAGES under VOL")
        check(len(d66) == 1 and d66[0].endswith(os.path.join("DataSet-66", "IMAGES")), "DS-66 IMAGES at root (no VOL)")
        check(len(discover.find_pdfs_for_dataset(os.path.join(root, "DataSet-77"))) == 3, "DS-77: 3 pdfs found")

        print("\n== §4a safety gate: CSAM(skip)/withhold/clean; high-confidence only; fail-safe ==")
        rev = safety.image_asset_verdict({"safety": {"flag": "review"}, "has_visual_content": True,
                                          "embedding_ref": "x"})
        check(not rev.withhold and not rev.csam, "bare flag=review is clean (not withheld, not csam)")
        check(safety.image_asset_verdict({"safety": {"minor_indicator": True}}).csam, "minor_indicator -> CSAM (doc skip)")
        check(safety.image_asset_verdict({"safety": {"explicit": True}}).tier == "sexual", "explicit -> withhold (sexual)")
        check(safety.image_asset_verdict({"safety": {"categories": ["violence"]}}).withhold, "violence -> withhold")
        check(safety.image_asset_verdict({"safety": {"categories": ["hate"]}}).withhold, "hate -> withhold (kept per 2026-06-25)")
        check(safety.image_asset_verdict({"safety": {"categories": ["self_harm"]}}).withhold, "self_harm -> withhold")
        check(safety.image_asset_verdict({"content_filtered": True}).withhold, "content_filter -> withhold")
        fs = safety.image_asset_verdict({"stage4_error": {"code": "throttled"}, "has_visual_content": True},
                                        withhold_on_missing=True)
        check(fs.withhold and fs.tier == "unvetted", "fail-safe: un-vetted image -> withhold")
        check(safety.page_is_blank({"page_kind": "blank"}) and not safety.page_is_blank({"text": {"content": "hi"}}),
              "page_is_blank detects blank, not text pages")

        print("\n== vector-store reader: present vs missing ==")
        store77 = VectorStore(os.path.join(root, "DataSet-77", ".embeddings"), DIMS)
        got = store77.get_many(facts["all_shas"])
        check(facts["missing_sha"] not in got, "the deliberately-missing sha is absent from the store")
        check(len(got) >= 3 and all(len(v) == DIMS for v in got.values()), "present shas returned at locked dims")

        print("\n== per-document plan: identifiers (case_key='epstein'), withhold, CSAM skip ==")
        d77 = os.path.join(root, "DataSet-77")
        p2 = plan_document(os.path.join(d77, "VOL00077", "IMAGES", "0001", "doc2.pdf"), d77, cfg, store77)
        check(p2.status == "plan" and p2.identifiers["case_key"] == "epstein"
              and p2.identifiers["dataset_key"] == "DataSet-77", "doc2 identifiers: case_key='epstein', DataSet-77")
        check(p2.children == 2 and p2.vectors_present == 1 and p2.vectors_missing == 1,
              f"doc2: 2 children, 1 vector missing (present={p2.vectors_present} missing={p2.vectors_missing})")
        check(p2.images_withheld == 6 and p2.csam_assets == 0 and p2.source_withheld,
              f"doc2: 6 assets withheld, source_withheld set (got {p2.images_withheld})")
        check({"sexual", "violence", "hate", "self_harm", "content_filter", "unvetted"} <= set(p2.withheld_tiers),
              f"doc2: all withhold tiers present ({p2.withheld_tiers})")
        check(p2.blank_pages == 1, "doc2: 1 blank page detected")
        p3 = plan_document(os.path.join(d77, "VOL00077", "IMAGES", "0001", "doc3.pdf"), d77, cfg, store77)
        check(p3.status == "skip_csam" and p3.csam_assets == 1, "doc3: CSAM-suspected -> skip_csam (NOT ingested)")
        p1 = plan_document(os.path.join(d77, "VOL00077", "IMAGES", "0001", "doc1.pdf"), d77, cfg, store77)
        check(p1.images_withheld == 0 and p1.images_embed == 1, "doc1: review-only asset embeds, not withheld")

        print("\n== dry-run: corpus write-plan, no DB/Storage, CSAM-skip + vectors_missing surfaced ==")
        out = os.path.join(os.path.dirname(root), "tfe_s8_plan.json")   # OUTSIDE the corpus tree
        m = dry_run([d77, os.path.join(root, "DataSet-66")], cfg, write_plan_to=out)
        cc = m["corpus"]; c = cc["totals"]
        check(cc["documents_to_ingest"] == 3, "3 docs to ingest (doc1,doc2,doc4; doc3 CSAM-skipped)")
        check(cc["csam_documents_NOT_ingested"] == 1 and cc["csam_assets"] == 1, "1 CSAM doc NOT ingested")
        check(c["vectors_missing"] == 1, "corpus surfaces the 1 missing vector (guard would raise)")
        check(c["images_withheld"] == 6 and c["images_embed"] == 2, "corpus withhold/embed image totals")
        check(cc["source_withheld_docs"] == 1, "1 doc has source_withheld (doc2)")
        check(c["blank_pages"] == 3, "3 blank pages (doc1/doc2/doc4)")
        check(len(cc["tenancy_pairs"]) == 2, "2 (case,dataset) pairs: epstein/DataSet-77, epstein/DataSet-66")
        check(os.path.exists(out), "dry-run plan written to --out (a local file, not DB/Storage)")

        print("\n== S8 prepare: emit the apply-package (offline, no creds) ==")
        pkg = os.path.join(os.path.dirname(root), "tfe_s8_apply")   # OUTSIDE the corpus tree
        pm = prepare([d77, os.path.join(root, "DataSet-66")], cfg, pkg)
        plan_lines = [json.loads(l) for l in open(os.path.join(pkg, "apply-plan.jsonl"), encoding="utf-8")]
        check(len(plan_lines) == 3, "apply-plan.jsonl: 3 ingestable docs (doc3 CSAM excluded)")
        check(all("doc3" not in p["rel"] for p in plan_lines), "doc3 (CSAM) absent from apply-plan")

        emb_rows = [l for l in open(os.path.join(pkg, "embeddings.copy"), encoding="utf-8") if l.strip()]
        check(len(emb_rows) == facts["present_text_vectors"],
              f"embeddings.copy: {facts['present_text_vectors']} present text vectors (got {len(emb_rows)})")
        sha0, dim0, lit0 = emb_rows[0].rstrip("\n").split("\t")
        check(dim0 == str(DIMS) and lit0.startswith("[") and lit0.endswith("]"),
              "embeddings.copy row is sha<TAB>dim<TAB>[vec] (pgvector literal)")

        img_rows = [l for l in open(os.path.join(pkg, "image_embeddings.copy"), encoding="utf-8") if l.strip()]
        check(len(img_rows) == 2, f"image_embeddings.copy: 2 image vectors (doc1,doc4; doc2 withheld) got {len(img_rows)}")
        check({l.split('\t')[0] for l in img_rows} == facts["image_hashes"], "image vectors keyed by PNG content_hash")

        blobs = [json.loads(l) for l in open(os.path.join(pkg, "blobs.manifest.jsonl"), encoding="utf-8")]
        redacted = [b for b in blobs if b["action"] == "placeholder:content-redacted"]
        blank = [b for b in blobs if b["action"] == "placeholder:page-blank"]
        check(len(redacted) == 6, f"6 withheld assets -> content-redacted placeholder (got {len(redacted)})")
        check(len(blank) == 3, f"3 blank pages -> page-blank placeholder (got {len(blank)})")
        check(any(b["artifact"] == "source.pdf" and b["action"] == "upload" for b in blobs),
              "canonical source.pdf upload queued")

        d2 = next(p for p in plan_lines if "doc2" in p["rel"])
        check(d2["source_withheld"] is True and not d2["image_occurrences"],
              "doc2: source_withheld set, NO image occurrences (vectors absent)")
        d1 = next(p for p in plan_lines if "doc1" in p["rel"])
        check(d1["asset_context"] and d1["asset_context"]["summary_text"] == "A short memo.",
              "asset_context.summary_text from document_summary_final")
        check(d1["metadata"]["source"]["sha256"] == "f" * 64 and d1["metadata"]["generator"]["tool"],
              "metadata bag carries source + generator provenance")
        check(d1["document"]["case_key"] == "epstein" and d1["document"]["source_kind"] == "pdf",
              "documents row: case_key='epstein', source_kind='pdf'")
        check(d1["image_occurrences"] and d1["image_occurrences"][0]["artifact_kind"] == "pdf_image",
              "doc1 image occurrence routed (pdf_image)")

        check(pm["corpus"]["documents_to_ingest"] == 3 and pm["corpus"]["csam_documents_NOT_ingested"] == 1,
              "prepare-manifest: 3 to ingest / 1 CSAM NOT ingested")
        check(pm["corpus"]["totals"]["vectors_missing"] == 1, "prepare-manifest surfaces 1 missing text vector")
        check(pm["image_vectors_written"] == 2 and pm["text_vectors_written"] == facts["present_text_vectors"],
              "prepare-manifest vector write counts")

        print("\n== read-only: staged sidecars/chunks unchanged ==")
        check(_hashes(root) == before, "all sidecars + chunks.json byte-identical after dry-run + prepare")

    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"FAILED: {len(_fail)}" if _fail else "ALL CHECKS PASSED")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
