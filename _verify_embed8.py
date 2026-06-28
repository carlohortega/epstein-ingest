import json, os, struct, glob

DS = "/mnt/c/Projects/Data/epstein/DataSet-08"
store = os.path.join(DS, ".embeddings")
REC, SHA = 6176, 32

def present(sha_hex):
    path = os.path.join(store, sha_hex[:2] + ".f32v")
    if not os.path.exists(path):
        return False
    digest = bytes.fromhex(sha_hex)
    with open(path, "rb") as f:
        data = f.read()
    for i in range(len(data) // REC):
        if data[i*REC:i*REC+SHA] == digest:
            return True
    return False

shas, prefixes = [], set()
for cf in sorted(glob.glob(os.path.join(DS, "IMAGES", "*", "*.chunks.json")))[:40]:
    for p in json.load(open(cf)):
        for c in p["children"]:
            shas.append(c["content_sha"])
            prefixes.add(c["context_prefix"][:60])
sample = shas[:20]
found = sum(1 for s in sample if present(s))
total = sum(os.path.getsize(os.path.join(store, fn)) // REC
            for fn in os.listdir(store) if fn.endswith(".f32v"))
print(f"DS-08: sampled {len(sample)} content_sha -> present in store: {found}/{len(sample)}")
print("total vectors in DS-08 store:", total)
print("prefix sample:", sorted(prefixes)[0] if prefixes else "(none)")
