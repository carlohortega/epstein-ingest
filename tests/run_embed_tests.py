"""Stage-7 (embed) acceptance tests (no pytest, no network). Mirrors HANDOFF-stage7-embed §9 against the
synthetic FROZEN chunks.json corpus (tests/make_embed_fixtures.py). Stage 7 calls a network endpoint, so we
inject a deterministic FAKE provider (no API): the assertions are about WHICH inputs get embedded (the
fidelity core), the content_sha-keyed store, global dedup, idempotency/resume/force, crash-safety, and the
read-only invariant — none of which depend on the actual vector values.

Run:  python tests/run_embed_tests.py     (project root on PYTHONPATH; see run.sh / the sv-kb venv)
Exit code 0 = all passed.  Needs sv-kb on sys.path (collect._import_parents_from_dicts resolves it).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.embed import DEFAULT_EMBEDDING_MODEL                                  # noqa: E402
from src.embed.config import EmbedConfig                                       # noqa: E402
from src.embed.store import EmbedStore, StoreModelMismatch                     # noqa: E402
from src.embed.walk import run as run_embed, dry_run, default_store_root       # noqa: E402
from tests.make_embed_fixtures import build_embed, DATASET                     # noqa: E402

FIXED_TS = "2026-06-20T00:00:00Z"
DIMS = 8                      # tiny vectors keep the store files small; dims don't affect content_sha
API_VER = "2024-10-21"
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


class FakeProvider:
    """Deterministic, offline EmbeddingProvider. Records every input it is asked to embed so the tests can
    assert exactly which strings were embedded (and how many times)."""

    def __init__(self, dims: int = DIMS) -> None:
        self.model = DEFAULT_EMBEDDING_MODEL
        self.dimensions = dims
        self.calls = 0
        self.inputs: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.inputs.extend(texts)
        return [[float((sum(map(ord, t)) + i) % 251) for i in range(self.dimensions)] for t in texts]


def _all_children(root: str) -> list[dict]:
    out = []
    for dp, _d, files in os.walk(root):
        for fn in files:
            if fn.endswith(".chunks.json"):
                for p in json.load(open(os.path.join(dp, fn), encoding="utf-8")):
                    out.extend(p.get("children") or [])
    return out


def _data_hashes(root: str) -> dict:
    """sha256 of every chunks.json + sentinel sidecar (excludes the store + run manifest) for the read-only
    invariant."""
    store = default_store_root(root)
    out = {}
    for dp, _d, files in os.walk(root):
        if dp == store or dp.startswith(store + os.sep):
            continue
        for fn in files:
            if fn == "embed-run-manifest.json":
                continue
            if fn.endswith(".json"):
                p = os.path.join(dp, fn)
                out[p] = hashlib.sha256(open(p, "rb").read()).hexdigest()
    return out


def main() -> int:
    cfg = EmbedConfig(dimensions=DIMS, batch_size=4)
    root = tempfile.mkdtemp(prefix="tfe_s7_")
    facts = build_embed(root)
    try:
        children = _all_children(root)
        shas = {c["content_sha"] for c in children}
        n_children = len(children)
        n_unique = len(shas)
        before = _data_hashes(root)

        # -- §9.2 content_sha integrity of the fixtures (the inputs ARE what the sha was built from) -------
        print("\n== content_sha = sha256(model\\ncontext_prefix\\ntext) for every fixture child ==")
        ok_sha = all(
            hashlib.sha256((DEFAULT_EMBEDDING_MODEL + "\n" + c["context_prefix"] + "\n" + c["text"])
                           .encode("utf-8")).hexdigest() == c["content_sha"]
            for c in children)
        check(ok_sha, "every fixture child's content_sha round-trips from (model, context_prefix, text)")
        check(n_children > n_unique, f"fixture has a duplicated content_sha across files "
                                     f"({n_children} children, {n_unique} unique)")

        # -- §9.7 dry-run: projection only, NO API calls, NO writes ----------------------------------------
        print("\n== dry-run: unique/token/$ projection, no API, no writes ==")
        d = dry_run(root, cfg)
        check(d["children_seen"] == n_children and d["unique_content_sha"] == n_unique,
              f"dry-run counts {d['children_seen']} children / {d['unique_content_sha']} unique")
        check(d["to_embed"] == n_unique and d["cached"] == 0, "dry-run: all unique are to-embed (no store yet)")
        check(d["estimated_cost_usd"] >= 0.0 and d["to_embed_tokens"] > 0, "dry-run projects tokens/$")
        check(not os.path.exists(default_store_root(root)), "dry-run wrote NO store")
        check(not os.path.exists(os.path.join(root, "embed-run-manifest.json")), "dry-run wrote NO manifest")

        # -- §9.1/§9.4 real run: text/children only + global dedup -----------------------------------------
        print("\n== run: embeds each unique child once (global dedup), text/children only ==")
        prov = FakeProvider()
        m = run_embed(root, cfg, FIXED_TS, provider=prov, api_version=API_VER, deployment="dep-small",
                      workers=1, progress_every=0)
        check(m["children_seen"] == n_children and m["unique_content_sha"] == n_unique,
              f"run saw {m['children_seen']} children, {m['unique_content_sha']} unique")
        check(m["embedded"] == n_unique and m["cached"] == 0, f"embedded all {n_unique} unique once")
        check(len(prov.inputs) == n_unique and len(set(prov.inputs)) == n_unique,
              f"provider asked to embed exactly {n_unique} distinct strings (shared sha embedded ONCE)")
        check(all(s.startswith("[Context:") for s in prov.inputs),
              "every embedded input is a CHILD embed input (context_prefix + text) — no parent text embedded")

        # -- §9.2 embed input == f"{context_prefix} {text}" -----------------------------------------------
        print("\n== embed input == f'{context_prefix} {text}' ==")
        check(facts["shared_embed_input"] in prov.inputs,
              "the shared child was embedded with input == f'{context_prefix} {text}'")
        sample = children[0]
        check(f"{sample['context_prefix']} {sample['text']}" in prov.inputs,
              "a sampled child's embed input is exactly context_prefix + ONE space + text")

        # -- §9.3 model/dims locked + stamped (store + manifest) -------------------------------------------
        print("\n== model/dims/api_version stamped in the store + run manifest ==")
        store_dir = default_store_root(root)
        sm = json.load(open(os.path.join(store_dir, "embed-store-manifest.json"), encoding="utf-8"))
        check(sm["embedding_model"] == DEFAULT_EMBEDDING_MODEL and sm["dimensions"] == DIMS
              and sm["api_version"] == API_VER, "store manifest records model/dimensions/api_version")
        check(m["embedding_model"] == DEFAULT_EMBEDDING_MODEL and m["dimensions"] == DIMS
              and m["api_version"] == API_VER, "run manifest records model/dimensions/api_version")
        check("vector" not in json.dumps(m) and m["estimated_cost_usd"] > 0, "manifest has $ + no raw vectors")

        # -- §9.4 store is content_sha-keyed float32; values round-trip ------------------------------------
        print("\n== store: content_sha-keyed float32, vectors round-trip ==")
        store = EmbedStore(store_dir, model=DEFAULT_EMBEDDING_MODEL, dimensions=DIMS, api_version=API_VER)
        stored = dict(store.iter_vectors())
        store.close()
        check(set(stored) == shas, f"store holds exactly the {n_unique} unique content_sha keys")
        check(all(len(v) == DIMS for v in stored.values()), "every stored vector has the locked dimensions")
        # the shared sha's stored vector == what the fake provider produced for that input
        want = [float((sum(map(ord, facts["shared_embed_input"])) + i) % 251) for i in range(DIMS)]
        check(stored[facts["shared_content_sha"]] == want, "stored vector matches the embedded value")

        # -- §9.5 idempotent / resumable: a re-run embeds nothing ------------------------------------------
        print("\n== idempotency / resume: re-run skips everything already in the store ==")
        prov2 = FakeProvider()
        m2 = run_embed(root, cfg, FIXED_TS, provider=prov2, api_version=API_VER, deployment="dep-small",
                       workers=1, progress_every=0)
        check(prov2.calls == 0 and m2["embedded"] == 0 and m2["cached"] == n_unique,
              f"re-run embeds 0 (cached={m2['cached']}), provider NEVER called")

        # -- §9.5 --force re-embeds every unique sha -------------------------------------------------------
        print("\n== --force re-embeds every unique content_sha ==")
        prov3 = FakeProvider()
        m3 = run_embed(root, cfg, FIXED_TS, provider=prov3, api_version=API_VER, deployment="dep-small",
                       force=True, workers=1, progress_every=0)
        check(m3["embedded"] == n_unique and prov3.calls > 0, "--force re-embeds all unique shas")
        store2 = EmbedStore(store_dir, model=DEFAULT_EMBEDDING_MODEL, dimensions=DIMS, api_version=API_VER)
        check(set(dict(store2.iter_vectors())) == shas, "store still keyed by exactly the unique shas after force")
        store2.close()

        # -- §9.3 store refuses a model/dims mismatch ------------------------------------------------------
        print("\n== store refuses a model/dims change ==")
        raised = False
        try:
            EmbedStore(store_dir, model=DEFAULT_EMBEDDING_MODEL, dimensions=DIMS + 1, api_version=API_VER)
        except StoreModelMismatch:
            raised = True
        check(raised, "opening the store with different dimensions raises StoreModelMismatch")

        # -- crash safety: a partial trailing record is ignored, its sha re-embeds ------------------------
        print("\n== crash-safety: partial trailing record ignored, re-embedded on resume ==")
        croot = tempfile.mkdtemp(prefix="tfe_s7_crash_")
        build_embed(croot)
        try:
            cp = FakeProvider()
            run_embed(croot, cfg, FIXED_TS, provider=cp, api_version=API_VER, workers=1, progress_every=0)
            cstore = default_store_root(croot)
            shard = next(os.path.join(cstore, fn) for fn in os.listdir(cstore) if fn.endswith(".f32v"))
            rec = 32 + 4 * DIMS
            with open(shard, "ab") as f:                       # simulate a crash mid-record
                f.write(b"\x01\x02\x03")
            seen = EmbedStore.read_seen(cstore)
            recovered = sum(os.path.getsize(os.path.join(cstore, fn)) // rec
                            for fn in os.listdir(cstore) if fn.endswith(".f32v"))
            check(len(seen) == recovered == n_unique,
                  "read_seen ignores the trailing partial bytes (all committed records preserved)")
            # the truncate-on-write recovery fires when a shard is next written: a --force resume re-opens
            # every shard, truncating the partial record before appending -> record-aligned again, no torn data.
            cp2 = FakeProvider()
            run_embed(croot, cfg, FIXED_TS, provider=cp2, api_version=API_VER, force=True, workers=1,
                      progress_every=0)
            check(os.path.getsize(shard) % rec == 0,
                  "a write to the shard truncated the partial trailing record (record-aligned again)")
        finally:
            shutil.rmtree(croot, ignore_errors=True)

        # -- §9.6 read-only to the data tree --------------------------------------------------------------
        print("\n== read-only: chunks.json + sidecars byte-unchanged ==")
        after = _data_hashes(root)
        check(after == before, "every chunks.json + sidecar is byte-identical after all runs")

        # -- §9.8 source-agnostic: the media chunks.json (no sibling .pdf) was embedded --------------------
        print("\n== source-agnostic: media chunks.json (no .pdf) collected + embedded ==")
        media_path = os.path.join(root, DATASET, "media", "media1.chunks.json")
        media_shas = {c["content_sha"]
                      for p in json.load(open(media_path, encoding="utf-8")) for c in p["children"]}
        check(media_shas and media_shas <= set(stored),
              "media1.chunks.json's children were embedded despite having no sibling PDF")

        # -- concurrency parity ----------------------------------------------------------------------------
        print("\n== concurrency parity (thread-pool path) ==")
        croot = tempfile.mkdtemp(prefix="tfe_s7_conc_")
        build_embed(croot)
        try:
            cprov = FakeProvider()
            cm = run_embed(croot, cfg, FIXED_TS, provider=cprov, api_version=API_VER, workers=4,
                           progress_every=0)
            check(cm["embedded"] == n_unique and cm["unique_content_sha"] == n_unique,
                  "workers=4 embeds the same unique-sha count as workers=1")
            cstore = EmbedStore(default_store_root(croot), model=DEFAULT_EMBEDDING_MODEL, dimensions=DIMS,
                                api_version=API_VER)
            check(set(dict(cstore.iter_vectors())) == shas, "workers=4 store keyed by the same unique shas")
            cstore.close()
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
