"""Per-document Stage-4 building blocks: the sidecar gate, an ``AssetState`` that tracks which image
assets need processing, ``apply_outcome`` which merges one asset's result into the sidecar (and writes its
embedding sibling file), and ``finalize`` which writes the ``stage4`` block + manifest counts.

Transport-agnostic: the realtime scheduler (walk.py) drives an AssetState across a thread pool;
``process_document_sync`` runs one document start-to-finish (the workers<=1 path and the tests). Extends
the sidecar, never destructive; the api KEYS are never recorded.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json, sidecar_path
from . import STAGE4_TOOL_NAME, STAGE4_TOOL_VERSION, PROMPT_VERSION
from ..stage3.client import estimate_cost_usd
from .config import Stage4Config
from .assets import (Usage, AssetOutcome, process_asset, embedding_ref_for)
from .pregate import build_page_dark_lookup, SOURCE_PREGATE, SOURCE_VISION

# Per-asset verdict fields written onto each image_assets[] entry.
_VERDICT_KEYS = ("caption", "description", "content_class", "has_visual_content", "vision_confidence",
                 "safety")


def _result(action: str, rel: str, **kw) -> dict:
    base = {"action": action, "rel": rel, "error_code": None, "message": None,
            "assets": 0, "captioned": 0, "pregated_blank": 0, "has_visual_content_true": 0,
            "content_filtered": 0, "safety_review": 0, "embedded": 0, "embed_errors": 0,
            "content_safety_scanned": 0, "content_safety_review": 0, "errored": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0,
            "content_filter_entries": []}
    base.update(kw)
    return base


def open_sidecar(pdf_path: str, root: str, *, force: bool):
    """Gate. Returns (rel, sidecar_or_None, skip_result_or_None): when the third value is set the document
    is skipped/errored and recorded; otherwise proceed with the returned sidecar."""
    rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
    sc = sidecar_path(pdf_path)
    try:
        with open(sc, "r", encoding="utf-8") as f:
            sidecar = json.load(f)
    except FileNotFoundError:
        return rel, None, _result("no_sidecar", rel)
    except Exception as exc:
        return rel, None, _result("error", rel, error_code="bad_sidecar",
                                  message=f"{type(exc).__name__}: {exc}")
    if sidecar.get("status") != "ok":
        return rel, None, _result("skipped_status", rel)
    if not sidecar.get("stage2"):
        return rel, None, _result("no_stage2", rel)          # Stage 4 needs image_assets from Stage 2
    if sidecar.get("stage4") and not force:
        return rel, None, _result("skipped_done", rel)
    if not (sidecar.get("image_assets") or []):
        return rel, None, _result("no_assets", rel)          # text-only doc: nothing for vision
    return rel, sidecar, None


def write_embedding(base_dir: str, asset: dict, vector: list[float], model_version: str,
                    embedding_model: str) -> str:
    """Write an asset's image embedding to a sibling file and return its relative ``embedding_ref``."""
    ref = embedding_ref_for(asset)
    dest = os.path.join(base_dir, ref.replace("/", os.sep))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    dump_json({"embedding_model": embedding_model, "model_version": model_version,
               "dim": len(vector), "vector": vector}, dest)
    return ref


def apply_outcome(asset: dict, outcome: AssetOutcome, base_dir: str, sidecar: dict,
                  embedding_model: str) -> None:
    """Merge one asset's outcome into its image_assets[] entry (idempotent: a forced redo overwrites and
    clears stale fields). Demotes a kept-false image PAGE to page_kind=blank so the sidecar is internally
    consistent (Handoff §2 downstream rule)."""
    v = outcome.verdict or {}
    for k in _VERDICT_KEYS:
        if k in v:
            asset[k] = v[k]
    asset["prefilter_hint"] = outcome.prefilter_hint
    asset["verdict_source"] = outcome.source

    # content-filter marker
    if outcome.status == "content_filtered":
        asset["content_filtered"] = True
    else:
        asset.pop("content_filtered", None)

    # embedding ref
    asset.pop("embedding_error", None)
    if outcome.embedding is not None:
        asset["embedding_ref"] = write_embedding(base_dir, asset, outcome.embedding,
                                                  outcome.embedding_model or "", embedding_model)
    else:
        asset.setdefault("embedding_ref", None)
        if outcome.embedding_error:
            asset["embedding_error"] = outcome.embedding_error

    # content safety
    if outcome.content_safety is not None:
        asset["content_safety"] = outcome.content_safety

    # error marker (a step failed before producing a verdict)
    if outcome.status == "error":
        asset["stage4_error"] = {"code": outcome.error_code, "message": outcome.error_message}
    else:
        asset.pop("stage4_error", None)

    # page_kind demotion for a full-page image the gate rejected
    if (asset.get("kind") == "page" and v.get("has_visual_content") is False
            and outcome.status in ("vision", "pregated")):
        pno = asset.get("page_number")
        for p in (sidecar.get("pages") or []):
            if p.get("page_number") == pno and p.get("page_kind") == "image":
                p["page_kind"] = "blank"
                p.setdefault("flags", [])
                if "vision_blank" not in p["flags"]:
                    p["flags"].append("vision_blank")
                break


class AssetState:
    """Holds one document's image assets and which of them still need processing."""

    def __init__(self, rel: str, sc_path: str, sidecar: dict, cfg: Stage4Config, *, force: bool) -> None:
        self.rel = rel
        self.sc_path = sc_path
        self.sidecar = sidecar
        self.cfg = cfg
        self.base_dir = os.path.dirname(sc_path)
        self.assets = sidecar.get("image_assets") or []
        self.page_dark = build_page_dark_lookup(sidecar)
        # per-asset idempotency: an asset already carrying a verdict (has_visual_content) is done.
        self.to_call = [i for i, a in enumerate(self.assets)
                        if force or "has_visual_content" not in a]
        self.remaining = len(self.to_call)
        self.filtered_entries: list[dict] = []
        self.run_usage = Usage()              # THIS-RUN live spend (re-used assets on resume cost nothing)

    def record(self, outcome: AssetOutcome, embedding_model: str) -> None:
        self.remaining -= 1
        self.run_usage.merge(outcome.usage)
        asset = self.assets[outcome.index]
        apply_outcome(asset, outcome, self.base_dir, self.sidecar, embedding_model)
        if outcome.content_filter_entry is not None:
            entry = dict(outcome.content_filter_entry)
            entry["file"] = self.rel
            self.filtered_entries.append(entry)


def _scan_counts(assets: list[dict], cfg: Stage4Config) -> dict:
    """Document-state counts derived from the final sidecar so a resumed/forced run reports what a fresh
    run would."""
    c = {"assets": len(assets), "captioned": 0, "pregated_blank": 0, "has_visual_content_true": 0,
         "content_filtered": 0, "safety_review": 0, "embedded": 0, "embed_errors": 0,
         "content_safety_scanned": 0, "content_safety_review": 0, "errored": 0}
    for a in assets:
        if a.get("stage4_error"):
            c["errored"] += 1
        if a.get("content_filtered"):
            c["content_filtered"] += 1
        if a.get("verdict_source") == SOURCE_PREGATE:
            c["pregated_blank"] += 1
        elif a.get("verdict_source") == SOURCE_VISION and a.get("caption"):
            c["captioned"] += 1
        if a.get("has_visual_content") is True:
            c["has_visual_content_true"] += 1
        if (a.get("safety") or {}).get("flag") == "review":
            c["safety_review"] += 1
        if a.get("embedding_ref"):
            c["embedded"] += 1
        if a.get("embedding_error"):
            c["embed_errors"] += 1
        cs = a.get("content_safety")
        if isinstance(cs, dict) and cs.get("status") == "ok":
            c["content_safety_scanned"] += 1
            if cs.get("flag") == "review":
                c["content_safety_review"] += 1
    return c


def finalize(state: AssetState, run_usage: Usage, *, generated_at: str, provenance: dict,
             mode: str = "realtime") -> dict:
    """Write the ``stage4`` block (counts + usage + provenance) and persist the sidecar."""
    cfg = state.cfg
    counts = _scan_counts(state.assets, cfg)
    batch = mode == "batch"
    cost = estimate_cost_usd(run_usage.prompt_tokens, run_usage.completion_tokens,
                             run_usage.cached_tokens, cfg, batch=batch)
    state.sidecar["stage4"] = {
        "tool": STAGE4_TOOL_NAME,
        "tool_version": STAGE4_TOOL_VERSION,
        "vision_model": cfg.model,
        "vision_deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "embedding_model": provenance.get("embedding_model"),
        "content_safety": provenance.get("content_safety"),
        "prompt_version": PROMPT_VERSION,
        "generated_at_utc": generated_at,
        "mode": mode,
        "config": cfg.echo(),
        "counts": counts,
        "usage": {
            "prompt_tokens": run_usage.prompt_tokens,
            "completion_tokens": run_usage.completion_tokens,
            "cached_tokens": run_usage.cached_tokens,
            "calls": run_usage.calls,
            "estimated_cost_usd": round(cost, 6),
            "batch": batch,
        },
    }
    dump_json(state.sidecar, state.sc_path)
    return _result(
        "processed", state.rel,
        assets=counts["assets"], captioned=counts["captioned"], pregated_blank=counts["pregated_blank"],
        has_visual_content_true=counts["has_visual_content_true"],
        content_filtered=counts["content_filtered"], safety_review=counts["safety_review"],
        embedded=counts["embedded"], embed_errors=counts["embed_errors"],
        content_safety_scanned=counts["content_safety_scanned"],
        content_safety_review=counts["content_safety_review"], errored=counts["errored"],
        prompt_tokens=run_usage.prompt_tokens, completion_tokens=run_usage.completion_tokens,
        cached_tokens=run_usage.cached_tokens, calls=run_usage.calls,
        content_filter_entries=state.filtered_entries)


def process_document_sync(pdf_path: str, root: str, cfg: Stage4Config, generated_at: str,
                          provenance: dict, *, client, embedder, scanner, force: bool,
                          mode: str = "realtime") -> dict:
    """One document, start to finish, sequential. Used for workers<=1 and by the tests."""
    rel, sidecar, skip = open_sidecar(pdf_path, root, force=force)
    if skip is not None:
        return skip
    state = AssetState(rel, sidecar_path(pdf_path), sidecar, cfg, force=force)
    embedding_model = provenance.get("embedding_model")
    for idx in state.to_call:
        outcome = process_asset(idx, state.assets[idx], base_dir=state.base_dir,
                                page_dark=state.page_dark, cfg=cfg, client=client,
                                embedder=embedder, scanner=scanner)
        state.record(outcome, embedding_model)
    return finalize(state, state.run_usage, generated_at=generated_at, provenance=provenance, mode=mode)
