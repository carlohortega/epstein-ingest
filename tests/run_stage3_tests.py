"""Stage-3 acceptance tests (no pytest, no network). Mirrors HANDOFF-stage3 §10 against synthetic
fixtures, driving the stage with an INJECTED fake nano client (LLM output isn't byte-reproducible, so we
inject a deterministic stub instead of calling Azure — Handoff §9). Covers realtime + the Batch API join.

Run:  python tests/run_stage3_tests.py     (project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stage1.config import Config                              # noqa: E402
from src.stage1.walk import run as run_stage1, sidecar_path        # noqa: E402
from src.stage2.config import Stage2Config                  # noqa: E402
from src.stage2.walk import run as run_stage2              # noqa: E402
from src.stage3.config import Stage3Config                  # noqa: E402
from src.stage3.client import LLMResult, Stage3CallError, estimate_cost_usd  # noqa: E402
from src.stage3.connection import Connection               # noqa: E402
from src.stage3.walk import run as run_stage3, dry_run      # noqa: E402
from src.stage3.batch import run_batch                      # noqa: E402
from tests.make_fixtures import build_all, SECRET                          # noqa: E402
from tests.make_stage2_fixtures import build_stage2                        # noqa: E402
from tests.make_stage3_fixtures import build_stage3, SENSITIVE_WORD, FAIL_TOKEN, CFILTER_TOKEN  # noqa: E402

FIXED_TS = "2026-06-15T00:00:00Z"
PROV = {"deployment": "fake-nano", "api_version": "fake-version"}
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, rel: str) -> dict:
    with open(sidecar_path(os.path.join(root, rel)), "r", encoding="utf-8") as f:
        return json.load(f)


# --- deterministic fake nano client (Handoff §9) ---------------------------------------------------
class FakeNano:
    def __init__(self, fail_token: str | None = None, filter_token: str | None = None) -> None:
        self.calls = 0
        self.fail_token = fail_token
        self.filter_token = filter_token

    def complete_json(self, messages, schema, max_output_tokens):
        self.calls += 1
        user = messages[-1]["content"]
        if self.filter_token and schema["name"] == "page_summary" and self.filter_token in user:
            raise Stage3CallError("http 400: content management policy", "content_filter")
        if self.fail_token and schema["name"] == "page_summary" and self.fail_token in user:
            raise Stage3CallError("injected failure", "injected")
        if schema["name"] == "document_summary":
            data = {"summary": ("Document summary: " + user.replace("\n", " ").strip())[:200]}
        else:
            flag = "review" if SENSITIVE_WORD in user else "ok"
            data = {"summary": user.strip()[:160], "key_points": ["point"], "entities": [],
                    "topics": ["topic"],
                    "text_safety": {"flag": flag, "categories": (["violence"] if flag == "review" else [])}}
        return LLMResult(data, max(1, len(user) // 4), 20, 0)


class FakeBatchClient:
    """Simulates the Azure Batch API in-memory: completing a batch runs the fake nano over every input
    line so the join/finalize logic is exercised without Azure."""

    def __init__(self, nano: FakeNano) -> None:
        self.nano = nano
        self.files: dict[str, bytes] = {}
        self.batches: dict[str, dict] = {}
        self._n = 0

    def _fid(self) -> str:
        self._n += 1
        return f"file-{self._n}"

    def upload_jsonl(self, data: bytes) -> str:
        fid = self._fid()
        self.files[fid] = data
        return fid

    def create_batch(self, input_file_id: str) -> dict:
        out = []
        for raw in self.files[input_file_id].decode("utf-8").splitlines():
            if not raw.strip():
                continue
            ln = json.loads(raw)
            body, cid = ln["body"], ln["custom_id"]
            schema = body["response_format"]["json_schema"]
            try:
                r = self.nano.complete_json(body["messages"], schema, body.get("max_tokens", 512))
                out.append({"custom_id": cid, "error": None, "response": {"status_code": 200, "body": {
                    "choices": [{"message": {"content": json.dumps(r.data)}}],
                    "usage": {"prompt_tokens": r.prompt_tokens, "completion_tokens": r.completion_tokens}}}})
            except Stage3CallError as exc:
                out.append({"custom_id": cid, "error": str(exc),
                            "response": {"status_code": 500, "body": {}}})
        ofid = self._fid()
        self.files[ofid] = ("\n".join(json.dumps(o) for o in out) + "\n").encode("utf-8")
        bid = f"batch-{self._n}"
        self.batches[bid] = {"id": bid, "status": "completed", "output_file_id": ofid,
                             "error_file_id": None}
        return self.batches[bid]

    def get_batch(self, batch_id: str) -> dict:
        return self.batches[batch_id]

    def download_file(self, file_id: str) -> bytes:
        return self.files.get(file_id, b"") if file_id else b""


def _prep_corpus() -> str:
    root = tempfile.mkdtemp(prefix="tfe_s3_")
    build_all(root)
    build_stage2(root)
    build_stage3(root)
    run_stage1(root, Config(), FIXED_TS, force=False, workers=1)
    run_stage2(root, Stage2Config(), FIXED_TS, force=False, workers=1)
    return root


def main() -> int:
    cfg = Stage3Config()
    root = _prep_corpus()
    try:
        nano = FakeNano()
        m = run_stage3(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=False, workers=1,
                       progress_every=0)

        # -- §10.1 only text/mixed are summarized -----------------------------------------------------
        print("\n== routing: only text/mixed summarized ==")
        bd = load(root, "text_born_digital.pdf")["pages"][0]
        check(isinstance(bd.get("summary"), str) and bd.get("page_kind") == "text",
              "born-digital text page got a summary")
        fig = load(root, "figure_page.pdf")["pages"][0]
        check(isinstance(fig.get("summary"), str) and fig.get("page_kind") == "mixed",
              "mixed (figure) page got a summary")
        for rel, label in (("VOL00012/IMAGES/scanned_content.pdf", "image"),
                           ("VOL00012/IMAGES/blank_slip.pdf", "blank"),
                           ("fully_redacted.pdf", "redacted")):
            pg = load(root, rel)["pages"][0]
            check("summary" not in pg, f"{label} page got NO summary ({pg.get('page_kind')})")

        # -- §10.2 one bundled call -> valid summary + summary_json + text_safety ----------------------
        print("\n== bundled output shape + per-page token counts ==")
        sj = bd["summary_json"]
        check(all(k in sj for k in ("key_points", "entities", "topics", "low_confidence",
                                    "input_token_count", "output_token_count")),
              "summary_json carries key_points/entities/topics + provenance + token counts")
        check(isinstance(sj["input_token_count"], int) and sj["input_token_count"] > 0
              and isinstance(sj["output_token_count"], int) and sj["output_token_count"] > 0,
              "per-page input/output token counts recorded (>0)")
        ts = bd["text_safety"]
        check(ts["flag"] in ("ok", "review") and isinstance(ts["categories"], list),
              "text_safety has flag in {ok,review} + categories list (no 'block')")

        # -- §10.3 redaction respected ----------------------------------------------------------------
        print("\n== redaction respected ==")
        leak = load(root, "VOL00012/IMAGES/redacted_leak.pdf")["pages"][0]
        check(SECRET not in (leak.get("text", {}).get("content") or ""),
              "under-bar SECRET was already scrubbed from the page text (Stage 1)")
        check(isinstance(leak.get("summary"), str) and SECRET not in leak["summary"],
              "redacted page summarized WITHOUT reconstructing the redacted SECRET")

        # -- §10.4 provisional doc summary ------------------------------------------------------------
        print("\n== provisional document summary ==")
        ds = load(root, "text_born_digital.pdf")["document_summary"]
        check(ds is not None and ds.get("provisional") is True and ds.get("covered_pages", 0) >= 1,
              "doc with text pages -> provisional document_summary with covered_pages>=1")
        img_ds = load(root, "VOL00012/IMAGES/scanned_content.pdf")["document_summary"]
        check(img_ds is None, "all-image doc -> document_summary = null (left for Stage 5)")

        # -- §10.5 safety triage ----------------------------------------------------------------------
        print("\n== safety triage ==")
        sens = load(root, "sensitive_text.pdf")
        sp = sens["pages"][0]
        check(sp["text_safety"]["flag"] == "review" and "violence" in sp["text_safety"]["categories"],
              "sensitive page flagged review with a category hint")
        rollup = sens["document_summary"]["safety_rollup"]
        check(rollup["review"] >= 1 and rollup["pages_flagged"],
              "doc safety_rollup counts review pages + lists them")
        check(m["safety_review"] >= 1, "run manifest tallies safety_review")

        # -- §10.7 cost measured + provenance ---------------------------------------------------------
        print("\n== cost measured + provenance ==")
        u = m["usage"]
        check(u["prompt_tokens"] > 0 and u["completion_tokens"] > 0 and u["estimated_cost_usd"] > 0,
              f"manifest reports token totals + USD cost (${u['estimated_cost_usd']})")
        s3 = load(root, "text_born_digital.pdf")["stage3"]
        check(s3["deployment"] == "fake-nano" and s3["api_version"] == "fake-version"
              and s3["prompt_version"] and "endpoint" not in s3,
              "stage3 stamps deployment/api_version/prompt_version provenance; no endpoint/key")
        check(estimate_cost_usd(1_000_000, 1_000_000, 0, cfg, batch=True)
              == 0.5 * estimate_cost_usd(1_000_000, 1_000_000, 0, cfg, batch=False),
              "batch pricing is ~50% of realtime")
        check(os.path.exists(os.path.join(root, "stage3-run-manifest.json")), "run manifest written")

        # -- §10.6 idempotency + force ----------------------------------------------------------------
        print("\n== idempotency / resume / force ==")
        before = nano.calls
        m2 = run_stage3(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=False, workers=1,
                        progress_every=0)
        check(m2["pdfs_summarized"] == 0 and nano.calls == before,
              f"re-run skips all completed docs and makes no new calls ({m2['pdfs_skipped_already_done']} skipped)")
        m3 = run_stage3(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=True, workers=1,
                        progress_every=0)
        check(m3["pdfs_summarized"] >= 1 and nano.calls > before, "--force re-summarizes (new calls made)")

        # -- §10.6 fault isolation --------------------------------------------------------------------
        print("\n== fault isolation ==")
        froot = _prep_corpus()
        try:
            fm = run_stage3(froot, cfg, FIXED_TS, client=FakeNano(fail_token=FAIL_TOKEN), provenance=PROV,
                            force=False, workers=1, progress_every=0)
            fp = load(froot, "fail_page.pdf")["pages"][0]
            check("summary_error" in fp and "summary" not in fp,
                  "the failing page is marked summary_error (no summary)")
            check(fm["pages_errored"] >= 1 and fm["pdfs_summarized"] >= 1,
                  "one bad call is isolated; the run completes and other docs summarize")
        finally:
            shutil.rmtree(froot, ignore_errors=True)

        # -- content-filter handling: block -> review + logged (DataSet-12 canary finding) ------------
        print("\n== content-filter handling ==")
        croot = _prep_corpus()
        try:
            cm = run_stage3(croot, cfg, FIXED_TS, client=FakeNano(filter_token=CFILTER_TOKEN),
                            provenance=PROV, force=False, workers=1, progress_every=0)
            cp = load(croot, "filtered_page.pdf")["pages"][0]
            check(cp.get("text_safety", {}).get("flag") == "review"
                  and cp.get("text_safety", {}).get("content_filtered") is True
                  and "content_filtered" in cp.get("flags", []),
                  "content-filtered page -> text_safety.review + content_filtered marker")
            check("summary" not in cp and "summary_error" not in cp,
                  "content-filtered page has neither a summary nor a generic summary_error")
            check(cm["content_filtered"] >= 1 and cm["pages_errored"] == 0,
                  "manifest counts content_filtered separately, not as an error")
            log_path = os.path.join(croot, "content-filter-failures.json")
            check(os.path.exists(log_path), "content-filter-failures.json written")
            failures = json.load(open(log_path, encoding="utf-8"))["failures"]
            entry = next((e for e in failures if e["file"] == "filtered_page.pdf"), None)
            check(entry is not None and entry["error_code"] == "content_filter"
                  and entry["timestamp"] == FIXED_TS and entry["page_number"],
                  "failure log records file + page + timestamp + error_code")
        finally:
            shutil.rmtree(croot, ignore_errors=True)

        # -- §11 dry-run cost preview (no API) --------------------------------------------------------
        print("\n== dry-run cost preview ==")
        d = dry_run(root, cfg)
        check(d["eligible_pages"] >= 1 and d["estimated_cost_usd_realtime"] > 0
              and d["estimated_cost_usd_batch"] < d["estimated_cost_usd_realtime"],
              "dry-run projects eligible pages + realtime/batch cost from sidecars (no API)")

        # -- §8 Batch API path (join + finalize, offline) ---------------------------------------------
        print("\n== batch path ==")
        broot = _prep_corpus()
        try:
            bnano = FakeNano()
            conn = Connection("https://fake", "key", "fake-nano", "fake-version")
            bm = run_batch(broot, cfg, FIXED_TS, conn=conn, provenance=PROV, force=False,
                           batch_client=FakeBatchClient(bnano), realtime_client=bnano,
                           poll_interval=0, max_wait=10, log=lambda *_: None)
            bbd = load(broot, "text_born_digital.pdf")
            check(bbd["stage3"]["mode"] == "batch" and isinstance(bbd["pages"][0].get("summary"), str),
                  "batch mode summarized pages and stamped mode=batch")
            check(bbd["document_summary"] and bbd["document_summary"]["provisional"] is True,
                  "batch produced a provisional document_summary")
            bimg = load(broot, "VOL00012/IMAGES/scanned_content.pdf")["document_summary"]
            check(bimg is None, "batch: all-image doc -> document_summary null")
            bsens = load(broot, "sensitive_text.pdf")["document_summary"]["safety_rollup"]
            check(bm["usage"]["batch"] is True and bsens["review"] >= 1,
                  "batch manifest marks batch=True and safety triage carried through")
        finally:
            shutil.rmtree(broot, ignore_errors=True)

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
