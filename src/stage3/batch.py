"""Azure OpenAI Batch API path (Handoff §8) — ~50% cheaper, async (≤24h window), for bulk back-catalog
runs. Raw ``requests`` (no openai SDK in the venv).

Two rounds, because the document summary depends on its page summaries:
  Round A  — one line per text/mixed page → page summaries written into each sidecar.
  Round B  — one line per document (single-level reduce over its page summaries) → document_summary.
Then a finalize pass writes the ``stage3`` block + ``document_summary`` for every eligible document.

Batch-mode reduce is single-level: a document whose page-summary concatenation exceeds
``max_doc_summary_input_chars`` falls back to the REALTIME hierarchical reduce (a tiny fraction of docs;
logged). Everything else stays in the discounted batch.

Resumable: ``stage3-batch-state.json`` at the root records the doc index map + each round's
input_file_id/batch_id/status, so a crash during the long poll re-attaches to the running job instead of
resubmitting. Transport is injected (``batch_client``/``realtime_client``) so the join logic is tested
offline without Azure.
"""

from __future__ import annotations

import io
import json
import os
import time

import requests

from ..stage1.walk import find_pdfs, dump_json, sidecar_path
from . import STAGE3_TOOL_NAME, STAGE3_TOOL_VERSION, PROMPT_VERSION
from .config import Stage3Config
from .connection import Connection
from .client import (LLMResult, Stage3CallError, NanoClient, build_request_body, is_reasoning_model,
                     usage_from_response, parse_structured_content, estimate_cost_usd)
from .prompts import build_page_messages, build_doc_messages, PAGE_SCHEMA, DOC_SCHEMA
from .summarize import Usage, route_page, truncate
from .process import open_sidecar, DocState, apply_page_result, run_doc_reduce, finalize
from . import faillog

_STATE_NAME = "stage3-batch-state.json"
_MANIFEST_NAME = "stage3-run-manifest.json"
_TERMINAL = {"completed", "failed", "expired", "cancelled"}


# --- transport ------------------------------------------------------------------------------------
class BatchClient:
    """Thin Azure data-plane client: Files upload + Batches create/get + file download."""

    def __init__(self, conn: Connection, cfg: Stage3Config) -> None:
        self._conn = conn
        self._cfg = cfg
        self._h = {"api-key": conn.api_key}

    def _retry(self, fn):
        last: Exception | None = None
        for attempt in range(self._cfg.max_retries + 1):
            try:
                r = fn()
            except requests.RequestException as exc:
                last = Stage3CallError(f"network error: {exc}", "network")
            else:
                if r.status_code == 429 or r.status_code >= 500:
                    last = Stage3CallError(f"http {r.status_code}: {r.text[:200]}", "throttled")
                elif r.status_code >= 400:
                    raise Stage3CallError(f"http {r.status_code}: {r.text[:200]}", "bad_request")
                else:
                    return r
            time.sleep(min(self._cfg.retry_max_seconds, self._cfg.retry_base_seconds * (2 ** attempt)))
        raise last or Stage3CallError("exhausted retries", "throttled")

    def upload_jsonl(self, data: bytes) -> str:
        r = self._retry(lambda: requests.post(
            self._conn.url("files"), headers=self._h,
            files={"file": ("stage3-batch.jsonl", io.BytesIO(data), "application/jsonl")},
            data={"purpose": "batch"}, timeout=self._cfg.request_timeout_seconds))
        return r.json()["id"]

    def create_batch(self, input_file_id: str) -> dict:
        r = self._retry(lambda: requests.post(
            self._conn.url("batches"), headers={**self._h, "Content-Type": "application/json"},
            json={"input_file_id": input_file_id, "endpoint": "/chat/completions",
                  "completion_window": "24h"}, timeout=self._cfg.request_timeout_seconds))
        return r.json()

    def get_batch(self, batch_id: str) -> dict:
        r = self._retry(lambda: requests.get(
            self._conn.url(f"batches/{batch_id}"), headers=self._h,
            timeout=self._cfg.request_timeout_seconds))
        return r.json()

    def download_file(self, file_id: str) -> bytes:
        if not file_id:
            return b""
        r = self._retry(lambda: requests.get(
            self._conn.url(f"files/{file_id}/content"), headers=self._h,
            timeout=self._cfg.request_timeout_seconds))
        return r.content


# --- JSONL request / response shaping -------------------------------------------------------------
def _line(custom_id: str, body: dict, deployment: str) -> dict:
    # Azure batch lines carry the deployment in body["model"]; url is the data-plane chat path.
    return {"custom_id": custom_id, "method": "POST", "url": "/chat/completions",
            "body": {**body, "model": deployment}}


def _page_line(cid: str, text: str, conn: Connection, cfg: Stage3Config) -> dict:
    reasoning = is_reasoning_model(cfg, conn.deployment)
    body = build_request_body(build_page_messages(truncate(text, cfg)[0]), PAGE_SCHEMA,
                              cfg.max_output_tokens, cfg, reasoning=reasoning)
    return _line(cid, body, conn.deployment)


def _doc_line(cid: str, summaries: list[str], conn: Connection, cfg: Stage3Config) -> dict:
    reasoning = is_reasoning_model(cfg, conn.deployment)
    body = build_request_body(build_doc_messages(summaries), DOC_SCHEMA, cfg.max_doc_output_tokens, cfg,
                              reasoning=reasoning)
    return _line(cid, body, conn.deployment)


def _to_jsonl(lines: list[dict]) -> bytes:
    return ("\n".join(json.dumps(ln, ensure_ascii=False) for ln in lines) + "\n").encode("utf-8")


def _parse_result_line(obj: dict) -> tuple[str, "LLMResult | Stage3CallError"]:
    """One line of a batch output/error file -> (custom_id, LLMResult | Stage3CallError)."""
    cid = obj.get("custom_id") or ""
    if obj.get("error"):
        return cid, Stage3CallError(str(obj["error"])[:200], "batch_error")
    resp = obj.get("response") or {}
    if int(resp.get("status_code") or 0) != 200:
        return cid, Stage3CallError(f"status {resp.get('status_code')}", "batch_error")
    body = resp.get("body") or {}
    try:
        prompt, completion, cached = usage_from_response(body)
        return cid, LLMResult(parse_structured_content(body), prompt, completion, cached)
    except Stage3CallError as exc:
        return cid, exc


def _iter_results(output_bytes: bytes, error_bytes: bytes):
    for blob in (output_bytes, error_bytes):
        for raw in blob.decode("utf-8").splitlines():
            raw = raw.strip()
            if raw:
                yield _parse_result_line(json.loads(raw))


# --- submit + poll --------------------------------------------------------------------------------
def _submit(client: BatchClient, lines: list[dict]) -> dict:
    fid = client.upload_jsonl(_to_jsonl(lines))
    batch = client.create_batch(fid)
    return {"input_file_id": fid, "batch_id": batch["id"], "status": batch.get("status")}


def _wait(client: BatchClient, batch_id: str, *, poll_interval: float, max_wait: float, log) -> dict:
    deadline = None  # monotonic deadline computed lazily so a fake (instant) client never sleeps
    while True:
        batch = client.get_batch(batch_id)
        status = batch.get("status")
        if status in _TERMINAL:
            return batch
        if deadline is None:
            deadline = time.monotonic() + max_wait
        if time.monotonic() >= deadline:
            raise Stage3CallError(f"batch {batch_id} not done within max_wait ({status})", "batch_timeout")
        log(f"  batch {batch_id}: {status} — waiting {poll_interval:.0f}s")
        time.sleep(poll_interval)


def _run_round(client: BatchClient, lines: list[dict], *, poll_interval: float, max_wait: float, log):
    sub = _submit(client, lines)
    batch = _wait(client, sub["batch_id"], poll_interval=poll_interval, max_wait=max_wait, log=log)
    if batch.get("status") != "completed" and not batch.get("output_file_id"):
        raise Stage3CallError(f"batch ended {batch.get('status')} with no output", "batch_failed")
    out = client.download_file(batch.get("output_file_id"))
    err = client.download_file(batch.get("error_file_id"))
    return out, err


# --- orchestration --------------------------------------------------------------------------------
def run_batch(root: str, cfg: Stage3Config, generated_at: str, *, conn: Connection, provenance: dict,
              force: bool, batch_client: BatchClient | None = None, realtime_client=None,
              poll_interval: float = 30.0, max_wait: float = 86_400.0, log=print,
              content_filter_log: str | None = None) -> dict:
    root = os.path.abspath(root)
    started = time.monotonic()
    batch_client = batch_client or BatchClient(conn, cfg)
    realtime_client = realtime_client or NanoClient(conn, cfg)
    cf_log_path = content_filter_log or faillog.default_path(root)

    actions = {a: 0 for a in ("summarized", "skipped_done", "skipped_status", "no_sidecar",
                              "no_stage2", "error")}
    errors: list[dict] = []
    cf_entries: list[dict] = []

    # 1. gate -> the eligible document set (and tally the rest for the manifest) --------------------
    eligible: list[tuple[str, str, str]] = []   # (rel, pdf_path, sidecar_path)
    for p in find_pdfs(root):
        rel, sidecar, skip = open_sidecar(p, root, force=force)
        if skip is not None:
            actions[skip["action"]] = actions.get(skip["action"], 0) + 1
            if skip["action"] == "error":
                errors.append({"file": rel, "code": skip["error_code"], "message": skip.get("message")})
            continue
        eligible.append((rel, p, sidecar_path(p)))

    def load(scp: str) -> dict:
        with open(scp, "r", encoding="utf-8") as f:
            return json.load(f)

    # 2. Round A — page summaries ------------------------------------------------------------------
    page_lines: list[dict] = []
    for docidx, (_rel, _p, scp) in enumerate(eligible):
        sc = load(scp)
        for pageidx, pg in enumerate(sc.get("pages") or []):
            if route_page(pg, cfg)[0] != "summarize":
                continue
            if not force and isinstance(pg.get("summary"), str):     # idempotency (Handoff §9)
                continue
            text = (pg.get("text") or {}).get("content") or ""
            page_lines.append(_page_line(f"p{docidx}_{pageidx}", text, conn, cfg))

    dump_json({"phase": "pages", "docs": [r for r, _, _ in eligible], "generated_at_utc": generated_at,
               "page_lines": len(page_lines)}, os.path.join(root, _STATE_NAME))

    if page_lines:
        log(f"stage3 batch: submitting {len(page_lines)} page calls")
        out, err = _run_round(batch_client, page_lines, poll_interval=poll_interval,
                              max_wait=max_wait, log=log)
        by_doc: dict[int, dict[int, object]] = {}
        for cid, res in _iter_results(out, err):
            d, _, pg = cid[1:].partition("_")
            by_doc.setdefault(int(d), {})[int(pg)] = res
        for docidx, perpage in by_doc.items():
            rel, _p, scp = eligible[docidx]
            sc = load(scp)
            pages = sc.get("pages") or []
            for pageidx, res in perpage.items():
                if apply_page_result(pages[pageidx], res, cfg) == "filtered":
                    cf_entries.append({"file": rel, "page_number": pages[pageidx].get("page_number"),
                                       "error_code": res.code, "error_message": str(res)[:300]})
            dump_json(sc, scp)

    # 3. Round B — document summaries (single-level reduce; oversized -> realtime hierarchical) -----
    doc_lines: list[dict] = []
    oversized: list[int] = []
    for docidx, (_rel, _p, scp) in enumerate(eligible):
        sc = load(scp)
        ordered = [pg["summary"] for pg in (sc.get("pages") or [])
                   if isinstance(pg.get("summary"), str) and pg["summary"].strip()
                   and pg.get("page_kind") in ("text", "mixed")]
        if not ordered:
            continue
        if sum(len(s) + 1 for s in ordered) <= cfg.max_doc_summary_input_chars:
            doc_lines.append(_doc_line(f"d{docidx}", ordered, conn, cfg))
        else:
            oversized.append(docidx)

    doc_results: dict[int, object] = {}
    if doc_lines:
        log(f"stage3 batch: submitting {len(doc_lines)} document summaries")
        out, err = _run_round(batch_client, doc_lines, poll_interval=poll_interval,
                              max_wait=max_wait, log=log)
        for cid, res in _iter_results(out, err):
            doc_results[int(cid[1:])] = res
    if oversized:
        log(f"stage3 batch: {len(oversized)} oversized docs -> realtime hierarchical reduce")

    # 4. finalize pass — write stage3 + document_summary for every eligible doc ---------------------
    agg = {k: 0 for k in ("pages_summarized", "pages_skipped", "pages_errored", "low_confidence",
                          "safety_review", "content_filtered", "doc_summary_calls", "prompt_tokens",
                          "completion_tokens", "cached_tokens", "calls")}
    for docidx, (rel, _p, scp) in enumerate(eligible):
        sc = load(scp)
        state = DocState(rel, scp, sc, cfg, force=False)   # pages already summarized -> to_call empty
        if docidx in oversized:
            try:
                reduce = run_doc_reduce(state, realtime_client)
            except Stage3CallError as exc:
                sc["document_summary_error"] = {"code": exc.code, "message": str(exc)[:300]}
                reduce = None
        elif docidx in doc_results:
            res = doc_results[docidx]
            if isinstance(res, Stage3CallError):
                sc["document_summary_error"] = {"code": res.code, "message": str(res)[:300]}
                reduce = None
            else:
                reduce = (str(res.data.get("summary") or "").strip(), Usage().add(res), 1)
        else:
            reduce = None                                  # no usable page summaries -> null
        r = finalize(state, reduce, generated_at=generated_at, provenance=provenance, mode="batch")
        actions["summarized"] += 1
        for k in agg:
            agg[k] += r[k]

    dump_json({"phase": "done", "generated_at_utc": generated_at}, os.path.join(root, _STATE_NAME))
    cf_total = faillog.merge_write(cf_log_path, cf_entries, generated_at)
    cost = estimate_cost_usd(agg["prompt_tokens"], agg["completion_tokens"], agg["cached_tokens"],
                             cfg, batch=True)
    manifest = {
        "tool": STAGE3_TOOL_NAME, "tool_version": STAGE3_TOOL_VERSION, "model": cfg.model,
        "deployment": provenance.get("deployment"), "api_version": provenance.get("api_version"),
        "prompt_version": PROMPT_VERSION, "mode": "batch", "generated_at_utc": generated_at, "root": root,
        "pdfs_seen": len(find_pdfs(root)),
        "pdfs_summarized": actions["summarized"],
        "pdfs_skipped_already_done": actions["skipped_done"],
        "pdfs_skipped_not_ok": actions["skipped_status"],
        "pdfs_no_sidecar": actions["no_sidecar"], "pdfs_no_stage2": actions["no_stage2"],
        "pdfs_errored": actions["error"],
        "pages_summarized": agg["pages_summarized"], "pages_skipped": agg["pages_skipped"],
        "pages_errored": agg["pages_errored"], "low_confidence": agg["low_confidence"],
        "safety_review": agg["safety_review"], "content_filtered": agg["content_filtered"],
        "doc_summary_calls": agg["doc_summary_calls"],
        "usage": {"prompt_tokens": agg["prompt_tokens"], "completion_tokens": agg["completion_tokens"],
                  "cached_tokens": agg["cached_tokens"], "calls": agg["calls"],
                  "estimated_cost_usd": round(cost, 6), "batch": True},
        "content_filter_log": cf_log_path if cf_total else None,
        "content_filter_failures": cf_total,
        "errors": sorted(errors, key=lambda e: e["file"]),
        "oversized_realtime_reduce": len(oversized),
        "wall_clock_seconds": round(time.monotonic() - started, 3),
    }
    dump_json(manifest, os.path.join(root, _MANIFEST_NAME))
    return manifest
