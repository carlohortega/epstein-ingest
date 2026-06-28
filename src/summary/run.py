"""Orchestration: scan one or more dataset roots, build the report, write the artifacts.

Default report location (Handoff §4/§10): a per-dataset ``dataset-summary.{json,md,html}`` beside each
dataset's data, plus a corpus ``corpus-summary.{json,md,html}`` at the common parent — so reports live with
the data. ``--out DIR`` instead writes everything (prefixed) into one directory. Reports are written with the
atomic ``dump_json`` / atomic text write; sidecars are never touched."""

from __future__ import annotations

import os

from .aggregate import Aggregator
from .config import SummaryConfig
from . import embed_store
from .report import build_report, to_markdown, to_html
from .scan import scan_dataset
from .util import dump_json
from .walk import discover_datasets
from . import DATASET_REPORT_STEM, CORPUS_REPORT_STEM


def _write_text(text: str, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _emit(report: dict, stem_path: str, formats: set[str]) -> list[str]:
    """Write the requested formats for one report at ``<stem_path>.<ext>``. Returns the paths written."""
    written = []
    if "json" in formats:
        p = stem_path + ".json"
        dump_json(report, p)
        written.append(p)
    if "md" in formats:
        p = stem_path + ".md"
        _write_text(to_markdown(report), p)
        written.append(p)
    if "html" in formats:
        p = stem_path + ".html"
        _write_text(to_html(report), p)
        written.append(p)
    return written


def run(roots, cfg: SummaryConfig, *, generated_at: str, formats: set[str],
        out_dir: str | None = None, safety_list: bool = False) -> dict:
    """Scan ``roots`` (each expanded to its ``DataSet-*`` children when applicable), build + write reports,
    and return the full corpus report dict."""
    dataset_roots: list[str] = []
    seen = set()
    for r in roots:
        for ds in discover_datasets(r):
            if ds not in seen:
                seen.add(ds)
                dataset_roots.append(ds)

    # Resolve the Stage-7 vector store PER DATASET. Stage 7 writes its store at <run_root>/.embeddings, and the
    # run_root varies: DS-12's embed was rooted at the dataset (-> DataSet-12/.embeddings) while DS-09's was
    # rooted at the flat IMAGES dir (-> DataSet-09/VOLxxxxx/IMAGES/.embeddings). So we check, in order: --store;
    # <dataset>/.embeddings; <dataset>/VOL*/.embeddings; <dataset>/VOL*/IMAGES/.embeddings; <corpus>/.embeddings.
    # (Globbing one/two levels is cheap and never descends the render trees.) Loads are cached per dir.
    import glob
    corpus_root = (os.path.dirname(dataset_roots[0]) if len(dataset_roots) == 1
                   else os.path.commonpath(dataset_roots)) if dataset_roots else os.path.abspath(".")
    _store_cache: dict[str, tuple] = {}

    def _store_candidates(ds_root):
        if cfg.store:
            return [os.path.abspath(cfg.store)]
        cands = [os.path.join(ds_root, ".embeddings")]
        cands += sorted(glob.glob(os.path.join(ds_root, "*", ".embeddings")))
        cands += sorted(glob.glob(os.path.join(ds_root, "*", "IMAGES", ".embeddings")))
        cands += sorted(glob.glob(os.path.join(ds_root, "*", "images", ".embeddings")))
        cands.append(os.path.join(corpus_root, ".embeddings"))
        return cands

    corpus_store = os.path.join(corpus_root, ".embeddings")

    def _resolve_store(ds_root):
        for d in _store_candidates(ds_root):
            if d in _store_cache:
                manifest, seen = _store_cache[d]
            else:
                manifest = embed_store.read_manifest(d)
                seen = embed_store.load_seen(d, manifest) if manifest is not None else None
                # Memoize ONLY the shared corpus-global store. Per-dataset stores (e.g. DS-09's 3.5M-sha
                # store) are each used by exactly one dataset, so retaining their seen-sets would pile up
                # in RAM across the run (a second OOM source) — let them be GC'd after their dataset.
                if d == corpus_store:
                    _store_cache[d] = (manifest, seen)
            if manifest is not None:
                return d, manifest, seen
        return None, None, None

    agg = Aggregator()
    scans: dict[str, dict] = {}
    embed_by_root: dict[str, dict] = {}
    for ds in dataset_roots:
        ds_abs = os.path.abspath(ds)
        sdir, manifest, ds_seen = _resolve_store(ds_abs)
        if manifest is not None:
            embed_by_root[ds_abs] = {
                "store_present": True, "store_root": sdir, "store_aware": bool(cfg.chunks),
                "embedding_model": manifest.get("embedding_model"), "dimensions": manifest.get("dimensions"),
                "embedded_unique": len(ds_seen),
            }
            print(f"  embed store for {os.path.basename(ds_abs)}: {sdir} ({len(ds_seen):,} content_shas, "
                  f"per-doc coverage {'on' if cfg.chunks else 'off — add --chunks'})", flush=True)
        else:
            embed_by_root[ds_abs] = {"store_present": False}
        print(f"  scanning {ds} ...", flush=True)
        scans[ds] = scan_dataset(ds, cfg, agg, embed_seen=ds_seen)
        sm = scans[ds]
        print(f"    {sm['docs_scanned']}/{sm['docs_total']} docs · {sm['cache_hits']} cache hits · "
              f"{sm['wall_clock_s']}s", flush=True)

    result = agg.finalize(scans, embed_by_root)
    full = build_report(result, cfg.echo(), generated_at)

    written: list[str] = []
    # per-dataset reports (each scoped to its own dataset; corpus == that dataset)
    by_root = {d.get("root"): d for d in result["datasets"]}
    for ds in dataset_roots:
        d = by_root.get(ds)
        if d is None:
            continue
        per = build_report({"datasets": [d], "corpus": d}, cfg.echo(), generated_at)
        if out_dir:
            stem = os.path.join(out_dir, f"{d['dataset']}-{DATASET_REPORT_STEM}")
        else:
            stem = os.path.join(ds, DATASET_REPORT_STEM)
        written += _emit(per, stem, formats)

    # corpus report
    if out_dir:
        corpus_stem = os.path.join(out_dir, CORPUS_REPORT_STEM)
    else:
        parent = os.path.dirname(dataset_roots[0]) if len(dataset_roots) == 1 \
            else os.path.commonpath(dataset_roots)
        corpus_stem = os.path.join(parent, CORPUS_REPORT_STEM)
    written += _emit(full, corpus_stem, formats)

    if safety_list:
        sl_path = (os.path.join(out_dir, "quarantine-list.json") if out_dir
                   else corpus_stem.replace(CORPUS_REPORT_STEM, "quarantine-list") + ".json")
        dump_json({"generated_at_utc": generated_at, "datasets": agg.safety_list()}, sl_path)
        written.append(sl_path)

    full["_written"] = written
    return full
