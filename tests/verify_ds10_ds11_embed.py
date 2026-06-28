"""Verify the DS-10 + DS-11 Stage-7 re-embed (after the chunking fix). Run from WSL (data at /mnt/c).
Per dataset: run-manifest internal consistency, store record count == unique, model/dims stamped, and a
fidelity+membership spot-check proving the store is keyed to the CURRENT (corrected) chunks.json."""
from __future__ import annotations
import glob, hashlib, json, os, struct, sys

MODEL, DIMS, REC = "text-embedding-3-small", 1536, 32 + 4 * 1536
_fail = []
def check(c, m): print(f"  [{'PASS' if c else 'FAIL'}] {m}"); (None if c else _fail.append(m))

def shard_has(store, sha):
    """True if content_sha is present in its shard (scan 32-byte keys, seek past vectors)."""
    p = os.path.join(store, sha[:2] + ".f32v")
    if not os.path.exists(p): return False
    want = bytes.fromhex(sha)
    with open(p, "rb") as f:
        whole = os.path.getsize(p) // REC
        for _ in range(whole):
            if f.read(32) == want: return True
            f.seek(REC - 32, 1)
    return False

for ds, total_children in (("11", 1252915), ("10", 2468233)):
    root = f"/mnt/c/Projects/Data/epstein/DataSet-{ds}"
    store = f"{root}/.embeddings"
    print(f"\n===== DataSet-{ds} =====")
    m = json.load(open(f"{root}/embed-run-manifest.json", encoding="utf-8"))
    sm = json.load(open(f"{store}/embed-store-manifest.json", encoding="utf-8"))
    check(m["embedded"] + m["cached"] == m["unique_content_sha"],
          f"embedded({m['embedded']}) + cached({m['cached']}) == unique({m['unique_content_sha']})")
    check(m["batch_errors"] == 0 and m["files_errored"] == 0,
          f"0 batch_errors, 0 unreadable chunks.json (files_seen={m['files_seen']})")
    check(m["children_seen"] == total_children,
          f"children_seen({m['children_seen']}) == stage6 children({total_children})")
    check(m["embedding_model"] == MODEL and m["dimensions"] == DIMS and sm["embedding_model"] == MODEL
          and sm["dimensions"] == DIMS, "run+store manifests stamp text-embedding-3-small@1536")
    recs = sum(os.path.getsize(os.path.join(store, fn)) // REC
               for fn in os.listdir(store) if fn.endswith(".f32v"))
    check(recs == m["unique_content_sha"],
          f"store record count ({recs}) == unique_content_sha ({m['unique_content_sha']})")
    # fidelity + membership on a sample of CURRENT chunks
    files = sorted(glob.glob(f"{root}/VOL*/IMAGES/**/*.chunks.json", recursive=True))[:8]
    n = rt = mem = 0
    for fp in files:
        for par in json.load(open(fp, encoding="utf-8")):
            for c in par.get("children") or []:
                if n >= 24: break
                n += 1
                sha = hashlib.sha256((MODEL + "\n" + c["context_prefix"] + "\n" + c["text"]).encode()).hexdigest()
                if sha == c["content_sha"]: rt += 1
                if shard_has(store, c["content_sha"]): mem += 1
            if n >= 24: break
        if n >= 24: break
    check(rt == n, f"{n}/{n} sampled children round-trip (content_sha == sha256(model,prefix,text))")
    check(mem == n, f"{mem}/{n} sampled children's content_sha PRESENT in the store (keyed to corrected chunks)")
    print(f"  -> embedded {m['embedded']} unique, tokens {m['tokens']}, ~${m['estimated_cost_usd']:.4f}, "
          f"{m['wall_clock_seconds']:.0f}s")

print("\n" + "=" * 56)
print(f"FAILED: {len(_fail)}" if _fail else "ALL VERIFICATION CHECKS PASSED")
sys.exit(1 if _fail else 0)
