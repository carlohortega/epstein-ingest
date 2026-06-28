"""Stage-8 S9 (apply) OFFLINE unit tests — the PURE logic of the live path, exercised with NO DB, NO Azure,
NO sv-kb on the path (every sv-kb/psycopg import in the S9 modules is lazy, inside the functions that actually
write). Covers: the guard provider (must raise on an un-cached chunk), deterministic ids, the content-safety
rollup, tenancy resolution policy A (fail-loud) vs --ensure-tenancy (upsert) via a fake cursor, the tenancy
preflight, and the blob-manifest grouping.

Run:  python tests/run_ingest_s9_tests.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ingest.event import make_ids                                       # noqa: E402
from src.ingest.load import GuardProvider                                   # noqa: E402
from src.ingest.register import _rollup                                     # noqa: E402
from src.ingest.tenancy import TenancyResolver, preflight, TenancyError     # noqa: E402
from src.ingest.blobs import group_manifest_by_document                     # noqa: E402

_fail: list = []
def check(c, m): print(f"  [{'PASS' if c else 'FAIL'}] {m}"); (None if c else _fail.append(m))


class FakeCur:
    """Returns canned fetchone() results in order; records executed SQL. Enough to drive the pure SQL flow."""
    def __init__(self, results): self.results = list(results); self.executed = []
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchone(self): return self.results.pop(0)


def main() -> int:
    print("\n== guard provider: never embeds (raises on an un-cached chunk) ==")
    gp = GuardProvider("text-embedding-3-small", 1536)
    check(gp.model == "text-embedding-3-small" and gp.dimensions == 1536 and gp.last_usage is None,
          "guard provider exposes the EmbeddingProvider surface (model/dimensions/last_usage)")
    check(gp.embed([]) == [], "embed([]) is a no-op (empty batch never triggers a call)")
    raised = False
    try:
        gp.embed(["uncached text"])
    except RuntimeError:
        raised = True
    check(raised, "embed([nonempty]) RAISES — a missing content_sha is loud, not a silent live re-embed")

    print("\n== deterministic per-document ids (stable re-apply) ==")
    a = make_ids("t", "epstein", "DataSet-77", "doc1")
    b = make_ids("t", "epstein", "DataSet-77", "doc1")
    c = make_ids("t", "epstein", "DataSet-77", "doc2")
    check(a == b and a[0] != a[1] and a != c, "make_ids is deterministic per doc, distinct ingestion/correlation")

    print("\n== content-safety rollup (element-wise max) ==")
    roll = _rollup([{"hate": 0, "sexual": 2, "violence": 6, "self_harm": 0, "csam_suspected": False},
                    {"hate": 4, "sexual": 0, "violence": 2, "self_harm": 0, "csam_suspected": False}])
    check(roll == {"hate": 4, "sexual": 2, "violence": 6, "self_harm": 0, "csam_suspected": False},
          "rollup takes the max per category across findings")
    check(_rollup([]) is None, "no findings -> no rollup (NULL content_safety)")

    print("\n== tenancy policy A: require pre-created, FAIL LOUD when missing ==")
    r = TenancyResolver(ensure=False)
    threw = False
    try:
        r.resolve(FakeCur([None]), "tid", "epstein", "DataSet-77")   # SELECT case -> None -> raise
    except TenancyError:
        threw = True
    check(threw, "missing case under policy A raises TenancyError (no silent fabrication)")

    print("\n== tenancy --ensure-tenancy: upsert on first sight ==")
    cid = "11111111-1111-1111-1111-111111111111"
    did = "22222222-2222-2222-2222-222222222222"
    cur = FakeCur([None, (cid,), None, (did,)])   # case: SELECT None, INSERT RETURNING; dataset: same
    got = TenancyResolver(ensure=True).resolve(cur, "tid", "epstein", "DataSet-77")
    check(got == (cid, did), "ensure mode resolves (case_id, dataset_id) by upserting both")
    check(any("INSERT INTO pipeline.cases" in s for s, _ in cur.executed)
          and any("INSERT INTO pipeline.datasets" in s for s, _ in cur.executed),
          "ensure mode issued the cases + datasets upserts")

    print("\n== tenancy caching: a second resolve of the same pair does NOT re-query ==")
    r2 = TenancyResolver(ensure=False)
    cur2 = FakeCur([(cid,), (did,)])              # one SELECT each, then cached
    r2.resolve(cur2, "tid", "epstein", "DataSet-77")
    n_before = len(cur2.executed)
    r2.resolve(cur2, "tid", "epstein", "DataSet-77")
    check(len(cur2.executed) == n_before, "cached (case,dataset) pair issues no further SQL")

    print("\n== tenancy preflight (read-only would-create vs found) ==")
    pf = preflight(FakeCur([None]), "tid", [("epstein", "DataSet-99")], ensure=False)
    check(pf["missing"] == [["epstein", "DataSet-99"]] and pf["would_fail"] == [["epstein", "DataSet-99"]],
          "preflight reports a missing pair as would_fail under policy A")
    pf2 = preflight(FakeCur([(cid,), (did,)]), "tid", [("epstein", "DataSet-77")], ensure=False)
    check(pf2["found"] == [["epstein", "DataSet-77"]] and not pf2["would_fail"], "preflight reports a found pair")

    print("\n== blob-manifest grouping by document ==")
    d = tempfile.mkdtemp(prefix="tfe_s9_")
    with open(os.path.join(d, "blobs.manifest.jsonl"), "w", encoding="utf-8") as f:
        for row in [{"case_key": "epstein", "dataset_key": "DataSet-77", "document_name": "doc1",
                     "artifact": "source.pdf", "action": "upload"},
                    {"case_key": "epstein", "dataset_key": "DataSet-77", "document_name": "doc1",
                     "artifact": "ocr/page-1.png", "action": "placeholder:page-blank"},
                    {"case_key": "epstein", "dataset_key": "DataSet-77", "document_name": "doc2",
                     "artifact": "source.pdf", "action": "upload"}]:
            f.write(json.dumps(row) + "\n")
    g = group_manifest_by_document(d)
    check(len(g) == 2 and len(g[("epstein", "DataSet-77", "doc1")]) == 2,
          "manifest groups 3 rows into 2 documents (doc1 has 2 blobs)")

    print("\n" + "=" * 56)
    print(f"FAILED: {len(_fail)}" if _fail else "ALL CHECKS PASSED")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
