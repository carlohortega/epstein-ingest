"""Per-document Stage-3 building blocks: the sidecar gate, a ``DocState`` that accumulates page results
and the (possibly hierarchical) document summary, and ``finalize`` which extends the sidecar in place.

These are transport-agnostic. The realtime scheduler (walk.py) drives a DocState across a thread pool;
the batch joiner (batch.py) drives the same DocState from retrieved results; ``process_document_sync``
runs one document start-to-finish (the workers<=1 path and the test path). Extends the sidecar, never
destructive (Handoff §7); the api KEY is never recorded.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json, sidecar_path
from . import STAGE3_TOOL_NAME, STAGE3_TOOL_VERSION, PROMPT_VERSION
from .config import Stage3Config
from .client import LLMResult, Stage3CallError, estimate_cost_usd
from .summarize import (Usage, route_page, normalize_page_output, make_summary_json,
                        is_low_confidence, summarize_page, summarize_document, safety_rollup)


def _result(action: str, rel: str, **kw) -> dict:
    base = {"action": action, "rel": rel, "error_code": None, "message": None,
            "pages_summarized": 0, "pages_skipped": 0, "pages_errored": 0, "low_confidence": 0,
            "safety_review": 0, "content_filtered": 0, "doc_summary_calls": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "calls": 0,
            "content_filter_entries": []}
    base.update(kw)
    return base


def open_sidecar(pdf_path: str, root: str, *, force: bool):
    """Gate (Handoff §2/§9). Returns (rel, sidecar_or_None, skip_result_or_None): when the third value is
    set the document is skipped/errored and recorded; otherwise proceed with the returned sidecar."""
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
        return rel, None, _result("no_stage2", rel)          # Stage 3 needs page_kind
    if sidecar.get("stage3") and not force:
        return rel, None, _result("skipped_done", rel)
    return rel, sidecar, None


def apply_page_result(page: dict, result, cfg: Stage3Config) -> str:
    """Write a page's summary/summary_json/text_safety from a model result (Handoff §7), or handle a
    failure. Shared by the realtime DocState and the batch joiner. Idempotent: a forced redo overwrites
    and clears any stale error/flag. Returns the status: "ok" | "filtered" | "error".

    A content-filter block is NOT a generic error: Azure refusing the page is itself a decisive safety
    signal, so the page is auto-escalated (text_safety.flag="review", content_filtered=true) rather than
    lost. No summary is written (the model produced none); it is logged for later handling."""
    def _clear_flag(name: str) -> None:
        if name in (page.get("flags") or []):
            page["flags"] = [f for f in page["flags"] if f != name]

    if isinstance(result, Stage3CallError):
        if result.code == "content_filter":
            page.pop("summary", None)
            page.pop("summary_json", None)
            page.pop("summary_error", None)
            page["text_safety"] = {"flag": "review", "categories": [], "content_filtered": True}
            page.setdefault("flags", [])
            if "content_filtered" not in page["flags"]:
                page["flags"].append("content_filtered")
            return "filtered"
        page["summary_error"] = {"code": result.code, "message": str(result)[:300]}
        return "error"
    page.pop("summary_error", None)
    _clear_flag("content_filtered")
    truncated = len((page.get("text") or {}).get("content") or "") > cfg.max_input_chars
    low_conf = is_low_confidence(page, truncated)
    summary, core, text_safety = normalize_page_output(result.data)
    page["summary"] = summary
    page["summary_json"] = make_summary_json(
        core, low_confidence=low_conf, truncated=truncated,
        prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens)
    page["text_safety"] = text_safety
    return "ok"


class DocState:
    """Holds one document's routing decision and accumulates page results + the doc summary."""

    def __init__(self, rel: str, sc_path: str, sidecar: dict, cfg: Stage3Config, *, force: bool) -> None:
        self.rel = rel
        self.sc_path = sc_path
        self.sidecar = sidecar
        self.cfg = cfg
        self.pages = sidecar.get("pages") or []
        self.to_call: list[int] = []          # page indices that need a model call
        self._summaries: dict[int, str] = {}  # idx -> summary text (existing + freshly produced)
        self.usage = Usage()                  # page-call usage
        self.skipped = 0
        self.summarized = 0
        self.errored = 0
        self.low_confidence = 0
        self.filtered_entries: list[dict] = []  # content-filter rejections, for content-filter-failures.json
        for idx, p in enumerate(self.pages):
            action, _reason = route_page(p, cfg)
            if action == "skip":
                self.skipped += 1
                continue
            if not force and isinstance(p.get("summary"), str):   # page-level idempotency (Handoff §9)
                self._summaries[idx] = p["summary"]
                continue
            self.to_call.append(idx)
        self.remaining = len(self.to_call)

    # -- per-page completion (called once per to_call entry) ------------------------------------------
    def record_page_result(self, idx: int, result: "LLMResult | Stage3CallError") -> None:
        self.remaining -= 1
        page = self.pages[idx]
        status = apply_page_result(page, result, self.cfg)      # fault isolation (Handoff §9)
        if status == "error":
            self.errored += 1
            return
        if status == "filtered":                               # blocked by Azure -> review + logged
            self.filtered_entries.append({
                "file": self.rel, "page_number": page.get("page_number"),
                "error_code": result.code, "error_message": str(result)[:300]})
            return
        self._summaries[idx] = page["summary"]
        self.usage.add(result)
        self.summarized += 1
        if page["summary_json"].get("low_confidence"):
            self.low_confidence += 1

    def ordered_summaries(self) -> list[str]:
        return [self._summaries[i] for i in sorted(self._summaries) if self._summaries[i].strip()]


def run_doc_reduce(state: DocState, client):
    """The provisional document summary (Handoff §5). Returns (text, usage, depth) or None when the doc
    has no usable page summaries (all image/blank/redacted/skipped) -> document_summary=null (Stage 5)."""
    ordered = state.ordered_summaries()
    if not ordered:
        return None
    return summarize_document(client, ordered, state.cfg)


def finalize(state: DocState, doc_reduce, *, generated_at: str, provenance: dict,
             mode: str) -> dict:
    """Write the per-page fields (already set), the top-level document_summary, and the stage3 block."""
    cfg = state.cfg
    rollup = safety_rollup(state.pages)

    if doc_reduce is None:
        state.sidecar["document_summary"] = None              # provisional null; Stage 5 finalizes
        reduce_usage, depth = Usage(), 0
    else:
        text, reduce_usage, depth = doc_reduce
        state.sidecar["document_summary"] = {
            "text": text,
            "provisional": True,
            "covered_pages": len(state.ordered_summaries()),
            "safety_rollup": rollup,
            "input_token_count": reduce_usage.prompt_tokens,
            "output_token_count": reduce_usage.completion_tokens,
            "reduce_depth": depth,
        }

    batch = mode == "batch"
    # Counts reflect the document's FINAL state (resume-stable; identical for realtime and batch), scanned
    # from the page fields rather than this-run counters. The doc-level token usage is summed from the
    # per-page input/output_token_count (the source of truth, Handoff §8) + the reduce, so it is the
    # document's total footprint regardless of how many pages were (re)done this run.
    counts = _scan_counts(state.pages, cfg, rollup, reduce_usage.calls)
    doc_prompt, doc_completion = _doc_token_totals(state.pages)
    doc_prompt += reduce_usage.prompt_tokens
    doc_completion += reduce_usage.completion_tokens
    state.sidecar["stage3"] = {
        "tool": STAGE3_TOOL_NAME,
        "tool_version": STAGE3_TOOL_VERSION,
        "model": cfg.model,
        "deployment": provenance.get("deployment"),
        "api_version": provenance.get("api_version"),
        "prompt_version": PROMPT_VERSION,
        "seed": cfg.seed,
        "generated_at_utc": generated_at,
        "mode": mode,
        "config": cfg.echo(),
        "counts": counts,
        "usage": {
            "prompt_tokens": doc_prompt,
            "completion_tokens": doc_completion,
            "calls": counts["pages_summarized"] + reduce_usage.calls,
            "estimated_cost_usd": round(
                estimate_cost_usd(doc_prompt, doc_completion, 0, cfg, batch=batch), 6),
            "batch": batch,
        },
    }
    dump_json(state.sidecar, state.sc_path)
    # The MANIFEST result carries THIS-RUN live spend (re-used pages on resume cost nothing this run), so
    # the run manifest is an accurate spend report; counts mirror the document's final state.
    run = Usage().merge(state.usage).merge(reduce_usage)
    return _result(
        "summarized", state.rel,
        pages_summarized=counts["pages_summarized"], pages_skipped=counts["pages_skipped"],
        pages_errored=counts["pages_errored"], low_confidence=counts["low_confidence"],
        safety_review=rollup["review"], content_filtered=counts["content_filtered"],
        doc_summary_calls=reduce_usage.calls,
        prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens,
        cached_tokens=run.cached_tokens, calls=run.calls,
        content_filter_entries=state.filtered_entries)


def _scan_counts(pages: list[dict], cfg: Stage3Config, rollup: dict, doc_summary_calls: int) -> dict:
    """Document-state page counts (Handoff §7), derived from the final sidecar so realtime and batch agree
    and a resumed/forced run reports the same thing a fresh run would."""
    summarized = skipped = errored = low_conf = filtered = 0
    for p in pages:
        if "content_filtered" in (p.get("flags") or []):       # blocked by Azure -> review, not error
            filtered += 1
            continue
        if p.get("summary_error"):
            errored += 1
            continue
        if isinstance(p.get("summary"), str):
            summarized += 1
            if (p.get("summary_json") or {}).get("low_confidence"):
                low_conf += 1
        elif route_page(p, cfg)[0] == "skip":
            skipped += 1
    return {"pages_summarized": summarized, "pages_skipped": skipped, "pages_errored": errored,
            "low_confidence": low_conf, "safety_review": rollup["review"],
            "content_filtered": filtered, "doc_summary_calls": doc_summary_calls}


def _doc_token_totals(pages: list[dict]) -> tuple[int, int]:
    """Sum the per-page token counts the stage recorded (Handoff §8: doc/run totals are summed from these)."""
    prompt = completion = 0
    for p in pages:
        sj = p.get("summary_json") or {}
        prompt += int(sj.get("input_token_count") or 0)
        completion += int(sj.get("output_token_count") or 0)
    return prompt, completion


def process_document_sync(pdf_path: str, root: str, client, cfg: Stage3Config, generated_at: str,
                          provenance: dict, *, force: bool, mode: str = "realtime") -> dict:
    """One document, start to finish, sequential. Used for workers<=1 and by the tests."""
    rel, sidecar, skip = open_sidecar(pdf_path, root, force=force)
    if skip is not None:
        return skip
    state = DocState(rel, sidecar_path(pdf_path), sidecar, cfg, force=force)
    for idx in state.to_call:
        try:
            res = summarize_page(client, (state.pages[idx].get("text") or {}).get("content") or "", cfg)
        except Stage3CallError as exc:
            res = exc
        state.record_page_result(idx, res)
    try:
        reduce = run_doc_reduce(state, client)
    except Stage3CallError as exc:
        # a failed doc-summary must not lose the page summaries already written; record null + a flag.
        state.sidecar["document_summary_error"] = {"code": exc.code, "message": str(exc)[:300]}
        reduce = None
    return finalize(state, reduce, generated_at=generated_at, provenance=provenance, mode=mode)
