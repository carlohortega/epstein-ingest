"""Per-document Stage-5 building blocks: the sidecar gate (Handoff §8), and ``finalize_document`` which
routes the doc (§2), runs at most one fusion reduce (§3), and extends the sidecar APPEND-ONLY with a new
``document_summary_final`` + a ``stage5`` provenance block (§5) — never touching Stage 3's provisional
``document_summary`` or any per-page/per-asset lane.

Transport-agnostic: the realtime scheduler (walk.py) runs ``process_document_sync`` across a thread pool
(one task per document — Stage 5's only call is the document reduce); ``process_document_sync`` also runs
the workers<=1 path and the tests. Extends the sidecar, never destructive; the api KEY is never recorded.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json, sidecar_path
from ..stage3.client import Stage3CallError, estimate_cost_usd
from ..stage3.summarize import Usage
from . import STAGE5_TOOL_NAME, STAGE5_TOOL_VERSION, PROMPT_VERSION, NO_CONTENT_SENTINEL
from .config import Stage5Config
from .fuse import (PROMOTED, FUSED, IMAGE_ONLY, NO_CONTENT,
                   route_document, build_fusion_inputs, summarize_fusion, safety_rollup)


def _result(action: str, rel: str, **kw) -> dict:
    base = {"action": action, "rel": rel, "error_code": None, "message": None,
            "source": None, "no_content": False, "content_filtered": False, "safety_review": False,
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0,
            "content_filter_entries": []}
    base.update(kw)
    return base


def open_sidecar(pdf_path: str, root: str, *, force: bool):
    """Gate (Handoff §8). Returns (rel, sidecar_or_None, skip_result_or_None): when the third value is set
    the document is skipped/errored and recorded; otherwise proceed with the returned sidecar.

        status != ok                        -> skip (skipped_status)
        no stage3 block                     -> skip (no_stage3)   # Stage 3 not done for this doc
        image_assets present and no stage4  -> skip (no_stage4)   # Stage 4 not done for a vision doc
        stage5 present and not --force      -> skip (skipped_done) # idempotency
    Text-only docs have NO stage4 block (Stage 4 skips no-image docs), so stage4 is required ONLY when
    ``image_assets`` is non-empty."""
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
    if not sidecar.get("stage3"):
        return rel, None, _result("no_stage3", rel)
    if (sidecar.get("image_assets") or []) and not sidecar.get("stage4"):
        return rel, None, _result("no_stage4", rel)
    if sidecar.get("stage5") and not force:
        return rel, None, _result("skipped_done", rel)
    return rel, sidecar, None


def _consumed(sidecar: dict) -> dict:
    """The provenance stamp for staleness detection (Handoff §7): the upstream prompt/tool versions Stage 5
    finalized over. Stage 4 fields are null for a text-only doc (no stage4 block)."""
    s3 = sidecar.get("stage3") or {}
    s4 = sidecar.get("stage4") or {}
    return {
        "stage3_prompt_version": s3.get("prompt_version"),
        "stage4_prompt_version": s4.get("prompt_version"),
        "stage4_tool_version": s4.get("tool_version"),
    }


def _write_stage5_block(sidecar: dict, cfg: Stage5Config, usage: Usage, *, generated_at: str,
                        provenance: dict, mode: str) -> None:
    """The per-doc ``stage5`` provenance block (Handoff §5). Carries provenance + the consumed stamp + this
    document's own usage; run-level route tallies live in the run manifest (mirrors Stage 3/4)."""
    sidecar["stage5"] = {
        "tool": STAGE5_TOOL_NAME,
        "tool_version": STAGE5_TOOL_VERSION,
        "model": cfg.model,
        "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "prompt_version": PROMPT_VERSION,
        "generated_at_utc": generated_at,
        "mode": mode,
        "config": cfg.echo(),
        "consumed": _consumed(sidecar),
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "cached_tokens": usage.cached_tokens,
            "calls": usage.calls,
            "estimated_cost_usd": round(estimate_cost_usd(
                usage.prompt_tokens, usage.completion_tokens, usage.cached_tokens, cfg, batch=False), 6),
            "batch": False,
        },
    }


def finalize_document(rel: str, sc_path: str, sidecar: dict, client, cfg: Stage5Config, *,
                      generated_at: str, provenance: dict, mode: str = "realtime") -> dict:
    """Route the document, run at most one fusion reduce, and write the append-only outputs. Fault-isolated
    (Handoff §10.7): a generic reduce failure leaves NO ``stage5`` block (so a re-run retries it) and is
    recorded; a content-filter block on the reduce (rare) is a decisive safety signal -> quarantine the doc
    + log. Returns the manifest result dict."""
    route, kept, _has_text = route_document(sidecar)
    rollup = safety_rollup(sidecar)
    usage = Usage()
    no_content = False
    cf_entries: list[dict] = []

    if route == PROMOTED:
        prov = sidecar.get("document_summary") or {}
        text = str(prov.get("text") or "")
        covered_text = int(prov.get("covered_pages") or 0)
        covered_vision = 0
    elif route == NO_CONTENT:
        text = NO_CONTENT_SENTINEL
        no_content = True
        covered_text = covered_vision = 0
    else:  # FUSED | IMAGE_ONLY -> one reduce over the page-ordered interleave
        items, covered_text, covered_vision = build_fusion_inputs(sidecar)
        if not items:
            # Defensive: the gate said kept_vision but nothing foldable survived (e.g. captions all empty).
            # Nothing to summarize -> sentinel rather than an empty/fabricated reduce.
            text = NO_CONTENT_SENTINEL
            no_content = True
            route = NO_CONTENT
            covered_text = covered_vision = 0
        else:
            try:
                text, usage, _depth = summarize_fusion(client, items, cfg)
            except Stage3CallError as exc:
                if exc.code != "content_filter":
                    # Generic API/parse error: do NOT finalize. Record a retryable marker (no stage5 block)
                    # and persist so a re-run without --force picks it up again.
                    sidecar["document_summary_final_error"] = {"code": exc.code, "message": str(exc)[:300]}
                    dump_json(sidecar, sc_path)
                    return _result("error", rel, error_code=exc.code, message=str(exc)[:300])
                # Content-filter block on the reduce is itself a decisive safety signal (mirrors Stage 3's
                # page handling): quarantine the doc with the sentinel + log it. Rare.
                text = NO_CONTENT_SENTINEL
                rollup = {"flag": "review", "content_filtered": True}
                cf_entries.append({"file": rel, "page_number": None, "error_code": exc.code,
                                   "error_message": str(exc)[:300]})

    sidecar.pop("document_summary_final_error", None)   # clear a stale marker on a forced/successful redo
    sidecar["document_summary_final"] = {
        "text": text,
        "final": True,
        "source": route,
        "covered_text_pages": covered_text,
        "covered_vision_assets": covered_vision,
        "safety": rollup,
        "no_content": no_content,
        "input_token_count": usage.prompt_tokens,    # 0 for promote / no-content (no call)
        "output_token_count": usage.completion_tokens,
    }
    _write_stage5_block(sidecar, cfg, usage, generated_at=generated_at, provenance=provenance, mode=mode)
    dump_json(sidecar, sc_path)

    return _result(
        "finalized", rel,
        source=route, no_content=no_content,
        content_filtered=bool(rollup.get("content_filtered")),
        safety_review=(rollup.get("flag") == "review"),
        calls=usage.calls, prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens, cached_tokens=usage.cached_tokens,
        content_filter_entries=cf_entries)


def process_document_sync(pdf_path: str, root: str, client, cfg: Stage5Config, generated_at: str,
                          provenance: dict, *, force: bool, mode: str = "realtime") -> dict:
    """One document, start to finish. Used for workers<=1, by the scheduler's per-doc task, and the tests.
    Self-contained: opens, routes, finalizes, and writes its own sidecar."""
    rel, sidecar, skip = open_sidecar(pdf_path, root, force=force)
    if skip is not None:
        return skip
    return finalize_document(rel, sidecar_path(pdf_path), sidecar, client, cfg,
                             generated_at=generated_at, provenance=provenance, mode=mode)
