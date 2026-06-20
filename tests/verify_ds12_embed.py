"""One-off verification of the DS-12 Stage-7 canary embed run (run from WSL: data at /mnt/c/...).

Asserts the acceptance properties against the REAL run output (not fixtures): the run/store manifests are
stamped + consistent, the store is content_sha-keyed float32 at the locked dims, the store covers EXACTLY
the unique child content_sha set across every chunks.json (text/children only), a fidelity round-trip holds
for sampled children, and the source chunks.json were not mutated (read-only).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.embed.collect import _import_parents_from_dicts, find_chunks_files
from src.embed.store import EmbedStore

parents_from_dicts = _import_parents_from_dicts()

ROOT = "/mnt/c/Projects/Data/epstein/DataSet-12"
STORE = os.path.join(ROOT, ".embeddings")
MODEL = "text-embedding-3-small"
DIMS = 1536
_fail = []


def check(cond, msg):
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _fail.append(msg)


# -- manifests -------------------------------------------------------------------------------------------
run_m = json.load(open(os.path.join(ROOT, "embed-run-manifest.json"), encoding="utf-8"))
store_m = json.load(open(os.path.join(STORE, "embed-store-manifest.json"), encoding="utf-8"))
print("== run manifest ==")
check(run_m["embedding_model"] == MODEL and run_m["dimensions"] == DIMS and run_m["api_version"],
      f"run manifest stamps model/dims/api ({run_m['embedding_model']}@{run_m['dimensions']}, "
      f"api={run_m['api_version']})")
# embedded + cached == unique holds for BOTH a fresh run (3814+0) and the resume re-run (0+3814); this
# manifest reflects whichever ran last (here: the idempotency re-run, embedded=0 cached=3814).
check(run_m["embedded"] + run_m["cached"] == run_m["unique_content_sha"] and run_m["batch_errors"] == 0
      and run_m["files_errored"] == 0,
      f"embedded({run_m['embedded']}) + cached({run_m['cached']}) == unique "
      f"({run_m['unique_content_sha']}), 0 batch errors, 0 unreadable chunks.json")
check(run_m["children_seen"] == 3824 and run_m["files_seen"] == 152,
      f"saw all {run_m['children_seen']} children over {run_m['files_seen']} chunks.json")
print("== store manifest ==")
check(store_m["embedding_model"] == MODEL and store_m["dimensions"] == DIMS
      and store_m["record_bytes"] == 32 + 4 * DIMS and store_m["float_dtype"] == "float32",
      f"store manifest stamps model/dims + float32 geometry (record_bytes={store_m['record_bytes']})")

# -- collect the source truth ----------------------------------------------------------------------------
cfiles = find_chunks_files(ROOT, store_dirname=".embeddings")
all_shas, sample = set(), []
parent_count = 0
for cf in cfiles:
    for p in parents_from_dicts(json.load(open(cf, encoding="utf-8"))):
        parent_count += 1
        for c in p.children:
            all_shas.add(c.content_sha)
            if len(sample) < 50:
                sample.append((c.content_sha, c.context_prefix, c.text))

print("== store vs source chunks.json ==")
store = EmbedStore(STORE, model=MODEL, dimensions=DIMS, api_version=run_m["api_version"])
stored = dict(store.iter_vectors())
store.close()
check(set(stored) == all_shas,
      f"store holds EXACTLY the {len(all_shas)} unique child content_sha (no misses, no extras; "
      f"store={len(stored)})")
check(all(len(v) == DIMS for v in stored.values()), "every stored vector has the locked 1536 dims")

# -- fidelity round-trip: content_sha == sha256(model\nprefix\ntext) for sampled children ----------------
print("== fidelity: content_sha round-trip on sampled children ==")
ok = all(hashlib.sha256(f"{MODEL}\n{pref}\n{txt}".encode("utf-8")).hexdigest() == sha
         for sha, pref, txt in sample)
check(ok, f"all {len(sample)} sampled children round-trip from (model, context_prefix, text)")
check(all(sha in stored for sha, _, _ in sample), "every sampled child's content_sha is present in the store")

# -- read-only: chunks.json not mutated by the embed run -------------------------------------------------
print("== read-only: source chunks.json untouched ==")
store_mtime = os.path.getmtime(os.path.join(STORE, "embed-store-manifest.json"))
newest_chunk = max(os.path.getmtime(cf) for cf in cfiles)
check(newest_chunk < store_mtime,
      "every chunks.json predates the store (embed did not rewrite any source artifact)")

print("\n== store size ==")
shard_bytes = sum(os.path.getsize(os.path.join(STORE, f)) for f in os.listdir(STORE) if f.endswith(".f32v"))
nshards = sum(1 for f in os.listdir(STORE) if f.endswith(".f32v"))
print(f"  {nshards} shard files, {shard_bytes/1e6:.1f} MB "
      f"(expected ~{len(all_shas)*(32+4*DIMS)/1e6:.1f} MB for {len(all_shas)} records)")

print("\n" + "=" * 60)
print(f"FAILED: {len(_fail)}" if _fail else "ALL VERIFICATION CHECKS PASSED")
sys.exit(1 if _fail else 0)
