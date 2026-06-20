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

    # Resolve the shared (corpus-global) Stage-7 vector store ONCE: its seen-set is reused for every dataset's
    # S7 coverage. Store dir = --store or <corpus_root>/.embeddings. Loading the seen-set is opt-in cost — we
    # only do it when the store actually exists (no store => S7 simply reports not_started).
    corpus_root = (os.path.dirname(dataset_roots[0]) if len(dataset_roots) == 1
                   else os.path.commonpath(dataset_roots)) if dataset_roots else os.path.abspath(".")
    store_dir = os.path.abspath(cfg.store) if cfg.store else embed_store.default_store_dir(corpus_root)
    store_manifest = embed_store.read_manifest(store_dir)
    embed_seen = None
    embed_info = {"store_present": False}
    if store_manifest is not None:
        embed_seen = embed_store.load_seen(store_dir, store_manifest)
        embed_info = {
            "store_present": True,
            "store_root": store_dir,
            "store_aware": bool(cfg.chunks),     # exact per-doc coverage needs the per-doc child shas
            "embedding_model": store_manifest.get("embedding_model"),
            "dimensions": store_manifest.get("dimensions"),
            "embedded_unique": len(embed_seen),
        }
        print(f"  embed store: {store_dir} ({len(embed_seen):,} content_shas, "
              f"model={store_manifest.get('embedding_model')}, "
              f"per-doc coverage {'on' if cfg.chunks else 'off — add --chunks'})", flush=True)

    agg = Aggregator()
    scans: dict[str, dict] = {}
    for ds in dataset_roots:
        print(f"  scanning {ds} ...", flush=True)
        scans[ds] = scan_dataset(ds, cfg, agg, embed_seen=embed_seen)
        sm = scans[ds]
        print(f"    {sm['docs_scanned']}/{sm['docs_total']} docs · {sm['cache_hits']} cache hits · "
              f"{sm['wall_clock_s']}s", flush=True)

    result = agg.finalize(scans, embed_info)
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
