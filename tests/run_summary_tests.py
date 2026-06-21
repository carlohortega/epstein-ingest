"""Summary-scanner acceptance tests (no pytest, no network). Mirrors HANDOFF-dataset-summary §9 against a
synthetic FROZEN sidecar corpus (tests/make_summary_fixtures.py): read-only, correct progress (esp. S4
vision-gating + S7 store-aware coverage), cost == Σ block estimated_cost_usd, vision-embedding gap callout,
pruned render trees, and an incremental cache that re-parses only changed files.

Run:  python tests/run_summary_tests.py     (project root on PYTHONPATH)   Exit 0 = all passed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.summary.config import SummaryConfig                                  # noqa: E402
from src.summary.run import run                                               # noqa: E402
from src.summary.report import to_markdown, to_html                           # noqa: E402
from tests.make_summary_fixtures import build_summary, build_embed_store, EXPECTED  # noqa: E402

TS = "2026-06-25T00:00:00Z"
_failures: list[str] = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def _sidecar_hashes(root):
    skip = {".summary-cache.json", "dataset-summary.json", "corpus-summary.json", "quarantine-list.json"}
    out = {}
    for dp, _d, files in os.walk(root):
        for fn in files:
            if fn.endswith(".json") and fn not in skip:
                p = os.path.join(dp, fn)
                out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def _scan(root, out_dir, **over):
    cfg = SummaryConfig(workers=over.pop("workers", 1), use_cache=over.pop("use_cache", True),
                        rebuild_cache=over.pop("rebuild_cache", False), sample=over.pop("sample", 0),
                        chunks=over.pop("chunks", False), store=over.pop("store", ""))
    return run([root], cfg, generated_at=TS, formats=over.pop("formats", {"json", "md", "html"}),
               out_dir=out_dir, safety_list=over.pop("safety_list", False))


def _ds(report, name):
    return next(d for d in report["datasets"] if d["dataset"] == name)


def main():
    root = tempfile.mkdtemp(prefix="tfe_summary_")
    out_dir = tempfile.mkdtemp(prefix="tfe_summary_out_")
    build_summary(root)
    build_embed_store(root)                       # corpus-global S7 vector store at <root>/.embeddings
    try:
        # ---------- §9.1 read-only ----------
        print("\n== read-only: sidecars byte-identical after a scan ==")
        before = _sidecar_hashes(root)
        report = _scan(root, out_dir, chunks=True, safety_list=True)   # store auto-detected, store-aware S7
        after = _sidecar_hashes(root)
        check(before == after and len(before) > 0, "every sidecar/chunks/store file byte-identical after scan")

        corpus = report["corpus"]
        ds77, ds88 = _ds(report, "DataSet-77"), _ds(report, "DataSet-88")

        # ---------- §9.5 layouts + prune ----------
        print("\n== layouts + prune ==")
        check(ds77["totals"]["docs"] == 6, f"DS-77 sees 6 docs (got {ds77['totals']['docs']})")
        check(ds88["totals"]["docs"] == 3, f"DS-88 sees 3 docs, decoys pruned (got {ds88['totals']['docs']})")
        check(corpus["totals"]["docs"] == EXPECTED["corpus_docs"], "corpus = 9 docs")
        check(corpus["cost"]["total_usd"] < 1.0, "pruned decoy PDFs ($9.99) never entered the rollup")

        # ---------- §9.2 progress incl. S4 gating + S7 store-aware + S8 n/a ----------
        print("\n== progress: S4 gating, S7 store-aware, S8 n/a ==")
        s = ds77["stages"]
        check(s["stage4"]["applicable"] == EXPECTED["ds77_s4_applicable"]
              and s["stage4"]["done"] == EXPECTED["ds77_s4_done"], "S4 applies to the 2 vision docs (2/2)")
        check(ds88["stages"]["stage4"]["state"] == "n/a", "DS-88 has no vision docs -> S4 n/a")
        check(s["stage7"]["applicable"] == EXPECTED["ds77_s7_applicable"]
              and s["stage7"]["done"] == EXPECTED["ds77_s7_done"]
              and s["stage7"]["state"] == "in_progress",
              f"S7 store-aware: 2/3 docs fully embedded (got {s['stage7']['done']}/{s['stage7']['applicable']})")
        check(corpus["stages"]["stage7"]["done"] == 3 and corpus["stages"]["stage7"]["applicable"] == 4,
              "corpus S7 = 3/4 docs embedded (vision_done partial)")
        check(s["stage8"]["state"] == "n/a" and "DB" in (s["stage8"].get("note") or ""),
              "S8 ingest is n/a offline (requires DB cross-check)")

        # ---------- embed store section ----------
        print("\n== embed (S7) store section ==")
        em = corpus["embed"]
        check(em["store_present"] and em["store_aware"], "store present + store-aware (--chunks)")
        check(em["embedded_unique_content_sha"] == 7 and em["embedding_model"] == "text-embedding-3-small",
              f"store reports 7 content_shas + locked model (got {em['embedded_unique_content_sha']})")

        # ---------- per-dataset store resolution (the DS-12 real-world case) ----------
        print("\n== per-dataset store at <dataset>/.embeddings is detected ==")
        droot = tempfile.mkdtemp(prefix="tfe_summary_d_"); dout = tempfile.mkdtemp(prefix="tfe_summary_d_o_")
        build_summary(droot)
        build_embed_store(os.path.join(droot, "DataSet-77"))   # store at the DATASET root, not corpus root
        try:
            drep = _scan(droot, dout, chunks=True)
            d77, d88 = _ds(drep, "DataSet-77"), _ds(drep, "DataSet-88")
            check(d77["embed"]["store_present"] and "DataSet-77" in (d77["embed"].get("store_root") or ""),
                  "DataSet-77 detects its own <dataset>/.embeddings store")
            check(not d88["embed"]["store_present"],
                  "DataSet-88 (no store, no corpus-global) correctly reports no store")
        finally:
            shutil.rmtree(droot, ignore_errors=True); shutil.rmtree(dout, ignore_errors=True)

        # ---------- S7 readiness shown even with NO store (embed not run yet) ----------
        print("\n== S7 readiness shown pre-run (no store + --chunks) ==")
        nroot = tempfile.mkdtemp(prefix="tfe_summary_n_"); nout = tempfile.mkdtemp(prefix="tfe_summary_n_o_")
        build_summary(nroot)                          # NO embed store built
        try:
            nrep = _scan(nroot, nout, chunks=True)
            nem = nrep["corpus"]["embed"]
            check(not nem["store_present"] and nrep["corpus"]["chunks"].get("chunk_unique_sha", 0) > 0
                  and nem["projected_embed_usd"] is not None,
                  "no store: S7 not started but unique-chunk count + embed cost projection are computed")
            check("unique chunks ready" in to_markdown(nrep),
                  "Markdown shows S7 embed readiness even before any embed run")
        finally:
            shutil.rmtree(nroot, ignore_errors=True); shutil.rmtree(nout, ignore_errors=True)

        # ---------- vision-embedding coverage (THE callout) ----------
        print("\n== vision-embedding coverage gap callout ==")
        ve = corpus["vision_embedding"]
        check(ve["has_visual_assets"] == EXPECTED["vision_has_visual"]
              and ve["embedded_assets"] == EXPECTED["vision_embedded"]
              and ve["docs_with_gap"] == EXPECTED["vision_docs_gap"]
              and ve["embed_errors"] == EXPECTED["vision_embed_errors"],
              f"vision coverage 2/3 assets, 1 doc gap, 1 embed_error (got {ve})")
        check(ve["coverage_pct"] == 66.7, f"vision coverage pct = 66.7 (got {ve['coverage_pct']})")
        md = to_markdown(report)
        check("missing vision embeddings" in md and "Re-run Stage 4" in md,
              "Markdown prominently calls out the vision-embedding gap + remediation")
        hh = to_html(report)
        check("banner err" in hh and "vision embeddings" in hh, "HTML shows the danger banner + coverage tile")

        # ---------- cost ----------
        print("\n== cost == Σ block estimated_cost_usd ==")
        check(abs(corpus["cost"]["total_usd"] - round(EXPECTED["total_cost_usd"], 4)) < 1e-6,
              f"corpus cost {corpus['cost']['total_usd']} == hand-sum {round(EXPECTED['total_cost_usd'],4)}")

        # ---------- safety + source_kind ----------
        print("\n== safety + quarantine + source_kind ==")
        check(corpus["safety"]["docs_quarantined"] == EXPECTED["quarantined"], "2 docs quarantined")
        check(corpus["source_kinds"].get("pdf") == EXPECTED["corpus_docs"], "source_kinds = all pdf (9)")
        sl = json.load(open(os.path.join(out_dir, "quarantine-list.json")))
        gaps = sl["datasets"]["vision_embedding_gaps"]["DataSet-77"]
        check(any("vision_gap" in g["path"] for g in gaps), "safety-list enumerates the vision-embed gap doc")

        # ---------- health ----------
        print("\n== health ==")
        check(corpus["health"]["missing_sidecars"] == 1 and corpus["health"]["parse_errors"] == 1,
              "1 missing sidecar + 1 parse_error counted, scan did not crash")

        # ---------- reports written + well-formed ----------
        print("\n== reports JSON+MD+HTML ==")
        for ext in ("json", "md", "html"):
            check(os.path.exists(os.path.join(out_dir, f"corpus-summary.{ext}")), f"corpus-summary.{ext}")
        check(hh.startswith("<!doctype html") and "S7 embed" in hh and "</html>" in hh,
              "HTML self-contained + has the S7 column")

        # ---------- §9.6 incremental cache (default, no store) ----------
        print("\n== incremental cache (warm re-scan re-parses only changed) ==")
        croot = tempfile.mkdtemp(prefix="tfe_summary_c_")
        cout = tempfile.mkdtemp(prefix="tfe_summary_c_out_")
        build_summary(croot)                      # NO store -> clean cache accounting
        try:
            cold = _scan(croot, cout)["corpus"]["scan"]
            check(cold["cache_hits"] == 0 and cold["cache_misses"] == 8,
                  f"cold: 0 hits / 8 misses (got {cold['cache_hits']}/{cold['cache_misses']})")
            warm = _scan(croot, cout)["corpus"]["scan"]
            check(warm["cache_hits"] == 7 and warm["cache_misses"] == 1,
                  f"warm: 7 hits / 1 miss=corrupt (got {warm['cache_hits']}/{warm['cache_misses']})")
            tm = os.path.join(croot, "DataSet-77", "VOL00077", "IMAGES", "0001", "text_mid.json")
            sc = json.load(open(tm)); sc["stage3"] = {"usage": {"estimated_cost_usd": 0.0, "calls": 1},
                                                      "counts": {}}
            json.dump(sc, open(tm, "w"))
            ae = _scan(croot, cout)["corpus"]["scan"]
            check(ae["cache_hits"] == 6 and ae["cache_misses"] == 2,
                  f"after 1 edit: 6 hits / 2 misses (got {ae['cache_hits']}/{ae['cache_misses']})")
        finally:
            shutil.rmtree(croot, ignore_errors=True); shutil.rmtree(cout, ignore_errors=True)

        # ---------- §9.7 honest sampling ----------
        print("\n== honest sampling ==")
        sroot = tempfile.mkdtemp(prefix="tfe_summary_s_"); sout = tempfile.mkdtemp(prefix="tfe_summary_s_o_")
        build_summary(sroot)
        try:
            sr = _scan(sroot, sout, sample=1)["corpus"]["scan"]
            check(sr["mode"] == "sample" and sr["extrapolated"] and sr["sampled_pct"] < 100.0,
                  f"sample mode labeled + extrapolated (pct={sr['sampled_pct']})")
        finally:
            shutil.rmtree(sroot, ignore_errors=True); shutil.rmtree(sout, ignore_errors=True)

        # ---------- concurrency parity (process pool) ----------
        print("\n== concurrency parity ==")
        proot = tempfile.mkdtemp(prefix="tfe_summary_p_"); pout = tempfile.mkdtemp(prefix="tfe_summary_p_o_")
        build_summary(proot); build_embed_store(proot)
        try:
            par = _scan(proot, pout, workers=2, use_cache=False, chunks=True)["corpus"]
            check(par["totals"]["docs"] == EXPECTED["corpus_docs"]
                  and par["stages"]["stage7"]["done"] == 3
                  and abs(par["cost"]["total_usd"] - corpus["cost"]["total_usd"]) < 1e-6,
                  "workers=2 (process pool) matches workers=1 (docs + S7 + cost)")
        finally:
            shutil.rmtree(proot, ignore_errors=True); shutil.rmtree(pout, ignore_errors=True)

    finally:
        shutil.rmtree(root, ignore_errors=True); shutil.rmtree(out_dir, ignore_errors=True)

    print("\n" + "=" * 60)
    if _failures:
        print(f"FAILED: {len(_failures)} check(s)")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
