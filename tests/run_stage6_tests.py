"""Stage-6 acceptance tests (no pytest, no network). Mirrors HANDOFF-stage6 §10 against synthetic FROZEN
Stage-1.. sidecars. Stage 6 is deterministic + local (no LLM, no Azure), so unlike Stage 5 there is no
injected client — we assert the chunks are byte-identical to what sv-kb's chunk_pages would produce.

Run:  python tests/run_stage6_tests.py     (project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.  Needs sv-kb on sys.path (chunk._import_svkb resolves ~/repos/sv-kb/... or
$SVKB_SHARED).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stage6.config import Stage6Config                                       # noqa: E402
from src.stage6 import EMBEDDING_MODEL                                            # noqa: E402
from src.stage6.chunk import chunk_pages, parents_to_dicts                        # noqa: E402
from src.stage6.process import chunks_path                                        # noqa: E402
from src.stage6.walk import run as run_stage6, dry_run, find_pdfs_pruned          # noqa: E402
from svkb_pipeline.chunking import parents_from_dicts                             # noqa: E402
from tests.make_stage6_fixtures import build_stage6, BASE                         # noqa: E402

FIXED_TS = "2026-06-20T00:00:00Z"
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, rel: str) -> dict:
    with open(os.path.join(root, rel.replace("/", os.sep) + ".json"), "r", encoding="utf-8") as f:
        return json.load(f)


def load_chunks(root: str, rel: str) -> list:
    with open(os.path.join(root, rel.replace("/", os.sep) + ".chunks.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    cfg = Stage6Config()
    root = tempfile.mkdtemp(prefix="tfe_s6_")
    build_stage6(root)
    try:
        # snapshot the append-only invariants before the run (§10.3)
        before = {stem: (load(root, f"{BASE}/{stem}").get("stage5"),
                         load(root, f"{BASE}/{stem}").get("document_summary_final"))
                  for stem in ("two_text", "mixed_skip", "empty_doc")}

        m = run_stage6(root, cfg, FIXED_TS, force=False, workers=1, progress_every=0)

        # -- §10.1 reuse: round-trip through the live loader -----------------------------------------
        print("\n== reuse: chunks round-trip through parents_from_dicts (indexer can consume) ==")
        chunks = load_chunks(root, f"{BASE}/two_text")
        parents = parents_from_dicts(chunks)
        check(len(parents) == len(chunks) and all(p.children for p in parents),
              "two_text.chunks.json round-trips through parents_from_dicts")

        # -- §10.2 sv-kb fidelity: byte-identical to an independent chunk_pages --------------------
        print("\n== fidelity: chunks == independently-computed sv-kb output ==")
        sc = load(root, f"{BASE}/two_text")
        pages = [(p["page_number"], "## Transcription\n\n" + p["text"]["content"]) for p in sc["pages"]]
        ref = parents_to_dicts(chunk_pages(pages, document_name="two_text",
                                           case_key="VOL00077", dataset_key="DataSet-77"))
        check(ref == chunks, "our chunks.json is byte-identical to independent sv-kb chunk_pages output")
        check(chunks[0]["page_number"] == 1 and chunks[0]["chunk_kind"] == "text",
              "parent.page_number preserved + chunk_kind='text'")
        ids = sc["stage6"]["identifiers"]
        check(ids == {"document_name": "two_text", "case_key": "VOL00077", "dataset_key": "DataSet-77"},
              "identifiers derived from the path match the live convention")

        # -- §10.4 content_sha integrity -------------------------------------------------------------
        print("\n== content_sha = sha256(model\\ncontext_prefix\\ntext) ==")
        c = chunks[0]["children"][0]
        expect = hashlib.sha256(
            (EMBEDDING_MODEL + "\n" + c["context_prefix"] + "\n" + c["text"]).encode("utf-8")).hexdigest()
        check(expect == c["content_sha"], "child content_sha matches the documented formula")
        check("VOL00077/DataSet-77" in c["context_prefix"] and "Section: Transcription." in c["context_prefix"],
              "context_prefix carries the identifiers + the load-bearing Transcription section")
        check(sc["stage6"]["embedding_model"] == "text-embedding-3-small"
              and sc["stage6"]["brief_description"] == "",
              "embedding_model + brief_description are the sv-kb defaults (NOT the summary)")

        # -- §10.3 append-only -----------------------------------------------------------------------
        print("\n== append-only: stages 1..5 untouched, stage6 + sibling added ==")
        for stem, (s5, dsf) in before.items():
            sc2 = load(root, f"{BASE}/{stem}")
            check(sc2.get("stage5") == s5 and sc2.get("document_summary_final") == dsf,
                  f"{stem}: stage5 + document_summary_final byte-unchanged")
            check("stage6" in sc2, f"{stem}: stage6 block written")

        # -- §10.5 gate + skip-no-text + empty finalize + identifier error ---------------------------
        print("\n== gate / skip-no-text / empty finalize / identifier error ==")
        ms = load(root, f"{BASE}/mixed_skip")["stage6"]
        check(ms["counts"]["skipped_pages"] == 1 and ms["counts"]["parents"] >= 1
              and ms["chunks_ref"] == "mixed_skip.chunks.json"
              and os.path.exists(chunks_path(os.path.join(root, BASE, "mixed_skip.pdf"))),
              "mixed_skip: text-less page skipped, text page chunked, sibling written")
        emp = load(root, f"{BASE}/empty_doc")["stage6"]
        check(emp["counts"]["parents"] == 0 and emp["chunks_ref"] is None
              and not os.path.exists(chunks_path(os.path.join(root, BASE, "empty_doc.pdf"))),
              "empty_doc: parents=0, chunks_ref null, NO sibling file (finalized, not retried)")
        check("stage6" not in load(root, f"{BASE}/not_ok") and m["pdfs_skipped_not_ok"] == 1,
              "not_ok: status!=ok skipped (no stage6 block)")
        no_ids = load(root, "DataSet-77/loose/no_ids")
        check("stage6" not in no_ids and m["pdfs_errored"] == 1
              and any(e["code"] == "no_identifiers" for e in m["errors"]),
              "no_ids: IdentifierError -> error, NO stage6 block (retryable), recorded in manifest")

        # -- §10.7 no embedding / no API -------------------------------------------------------------
        print("\n== chunk-only: no vectors, no usage/cost in the manifest ==")
        check(all("embedding" not in ch for ch in chunks)
              and all("embedding" not in cc for ch in chunks for cc in ch["children"]),
              "chunks carry NO embedding vectors (embedding is Stage 7)")
        check("usage" not in m and "estimated_cost_usd" not in json.dumps(m),
              "manifest has no token usage / USD cost (Stage 6 makes no API calls)")

        # -- manifest counts -------------------------------------------------------------------------
        print("\n== manifest ==")
        cc = m["counts"]
        check(m["pdfs_seen"] == 5 and m["pdfs_chunked"] == 3 and m["pdfs_empty"] == 1,
              f"manifest: 5 seen, 3 chunked, 1 empty (seen={m['pdfs_seen']} chunked={m['pdfs_chunked']})")
        check(cc["parents"] == len(load_chunks(root, f"{BASE}/two_text"))
              + len(load_chunks(root, f"{BASE}/mixed_skip"))
              and cc["skipped_pages"] == 2,
              "manifest parent total == sum of written chunks; 2 text-less pages skipped (mixed+empty)")
        check(os.path.exists(os.path.join(root, "stage6-run-manifest.json")), "run manifest written")

        # -- §10.6 idempotency / resume / force ------------------------------------------------------
        print("\n== idempotency / resume / force ==")
        # capture deterministic chunk bytes before re-runs
        twb = open(chunks_path(os.path.join(root, BASE, "two_text.pdf")), "rb").read()
        m2 = run_stage6(root, cfg, FIXED_TS, force=False, workers=1, progress_every=0)
        # the no_ids doc has no stage6 block, so a no-force re-run RETRIES it (errors again); the 3 done
        # docs are skipped; not_ok stays skipped.
        check(m2["pdfs_skipped_already_done"] == 3 and m2["pdfs_chunked"] == 0 and m2["pdfs_errored"] == 1,
              f"re-run skips finalized docs ({m2['pdfs_skipped_already_done']}), retries the error doc")
        m3 = run_stage6(root, cfg, FIXED_TS, force=True, workers=1, progress_every=0)
        check(m3["pdfs_chunked"] == 3, "--force re-chunks every eligible doc")
        twa = open(chunks_path(os.path.join(root, BASE, "two_text.pdf")), "rb").read()
        check(twa == twb, "chunking is deterministic: --force reproduces byte-identical chunks.json")

        # -- §10.8 dry-run == real, no writes --------------------------------------------------------
        print("\n== dry-run: exact counts, no writes ==")
        droot = tempfile.mkdtemp(prefix="tfe_s6_dry_")
        build_stage6(droot)
        try:
            d = dry_run(droot, cfg)
            # eligible counts every status-ok doc with pages + no stage6 (incl. no_ids, which then errors).
            check(d["eligible_documents"] == 4 and d["empty_documents"] == 1
                  and d["identifier_errors"] == 1,
                  "dry-run: 4 eligible (incl. no_ids), 1 empty, 1 identifier-error")
            wrote_any = any(os.path.exists(p[:-4] + ".chunks.json") or "stage6" in json.load(open(p[:-4] + ".json"))
                            for p in find_pdfs_pruned(droot))
            check(not wrote_any, "dry-run wrote NO chunks.json and NO stage6 block")
            check(d["parents"] == cc["parents"], "dry-run parent count == real-run parent count")
        finally:
            shutil.rmtree(droot, ignore_errors=True)

        # -- concurrency parity ----------------------------------------------------------------------
        print("\n== concurrency parity (thread pool path) ==")
        croot = tempfile.mkdtemp(prefix="tfe_s6_conc_")
        build_stage6(croot)
        try:
            cm = run_stage6(croot, cfg, FIXED_TS, force=False, workers=4, progress_every=0)
            check(cm["counts"] == cc, "workers=4 produces the same counts as workers=1")
        finally:
            shutil.rmtree(croot, ignore_errors=True)

    finally:
        shutil.rmtree(root, ignore_errors=True)

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
