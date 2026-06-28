"""Fast verification of the Stage-7 re-embed for DS-09 (IMAGES-scoped), DS-10, DS-11.
Per dataset: run-manifest consistency, store record-count == unique, model/dims stamped, and a
fidelity+membership spot-check (sample discovered via lazy os.scandir, not a full recursive glob)."""
from __future__ import annotations
import hashlib, json, os, sys

MODEL, DIMS, REC = "text-embedding-3-small", 1536, 32 + 4 * 1536
_fail = []
def check(c, m): print(f"  [{'PASS' if c else 'FAIL'}] {m}"); (None if c else _fail.append(m))

# (label, store_dir, manifest, sample_root, stage6_children)
B = "/mnt/c/Projects/Data/epstein"
DATASETS = [
    ("DS-09", f"{B}/DataSet-09/VOL00009/IMAGES/.embeddings",
              f"{B}/DataSet-09/VOL00009/IMAGES/embed-run-manifest.json",
              f"{B}/DataSet-09/VOL00009/IMAGES", 3572277),
    ("DS-10", f"{B}/DataSet-10/.embeddings", f"{B}/DataSet-10/embed-run-manifest.json",
              f"{B}/DataSet-10/VOL00010/IMAGES", 2468233),
    ("DS-11", f"{B}/DataSet-11/.embeddings", f"{B}/DataSet-11/embed-run-manifest.json",
              f"{B}/DataSet-11/VOL00011/IMAGES", 1252915),
]

def sample_files(root, n=8):
    out, stack = [], [root]
    while stack and len(out) < n:
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_file(follow_symlinks=False) and e.name.endswith(".chunks.json"):
                            out.append(e.path)
                            if len(out) >= n:
                                break
                        elif e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        pass
        except OSError:
            pass
    return out

def shard_has(store, sha):
    p = os.path.join(store, sha[:2] + ".f32v")
    if not os.path.exists(p): return False
    want = bytes.fromhex(sha)
    with open(p, "rb") as f:
        for _ in range(os.path.getsize(p) // REC):
            if f.read(32) == want: return True
            f.seek(REC - 32, 1)
    return False

for label, store, manifest, sroot, children in DATASETS:
    print(f"\n===== {label} =====")
    if not os.path.exists(manifest):
        check(False, f"{label}: run-manifest missing ({manifest})"); continue
    m = json.load(open(manifest, encoding="utf-8"))
    sm = json.load(open(os.path.join(store, "embed-store-manifest.json"), encoding="utf-8"))
    check(m["embedded"] + m["cached"] == m["unique_content_sha"],
          f"embedded({m['embedded']}) + cached({m['cached']}) == unique({m['unique_content_sha']})")
    check(m["batch_errors"] == 0 and m["files_errored"] == 0, "0 batch_errors, 0 unreadable chunks.json")
    check(m["children_seen"] == children, f"children_seen({m['children_seen']}) == stage6({children})")
    check(m["embedding_model"] == MODEL and m["dimensions"] == DIMS and sm["embedding_model"] == MODEL
          and sm["dimensions"] == DIMS, "run+store manifests stamp text-embedding-3-small@1536")
    recs = sum(os.path.getsize(os.path.join(store, fn)) // REC
               for fn in os.listdir(store) if fn.endswith(".f32v"))
    check(recs == m["unique_content_sha"], f"store records ({recs}) == unique ({m['unique_content_sha']})")
    n = rt = mem = 0
    for fp in sample_files(sroot, 8):
        for par in json.load(open(fp, encoding="utf-8")):
            for c in par.get("children") or []:
                if n >= 24: break
                n += 1
                sha = hashlib.sha256((MODEL + "\n" + c["context_prefix"] + "\n" + c["text"]).encode()).hexdigest()
                rt += sha == c["content_sha"]; mem += shard_has(store, c["content_sha"])
            if n >= 24: break
        if n >= 24: break
    check(n and rt == n, f"{rt}/{n} sampled children round-trip (content_sha)")
    check(n and mem == n, f"{mem}/{n} sampled children present in store (keyed to corrected chunks)")
    print(f"  -> {m['embedded']} embedded, ~${m['estimated_cost_usd']:.4f}, {m['wall_clock_seconds']:.0f}s")

print("\n" + "=" * 56)
print(f"FAILED: {len(_fail)}" if _fail else "ALL VERIFICATION CHECKS PASSED")
sys.exit(1 if _fail else 0)
