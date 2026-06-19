"""Stage-5 acceptance tests (no pytest, no network). Mirrors HANDOFF-stage5 §10 against synthetic FROZEN
Stage-3/Stage-4 sidecars, driving the stage with an INJECTED fake nano client (LLM output isn't
byte-reproducible, so we inject a deterministic stub instead of calling Azure — Handoff §9).

Run:  python tests/run_stage5_tests.py     (project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stage3.client import LLMResult, Stage3CallError, estimate_cost_usd      # noqa: E402
from src.stage5.config import Stage5Config                                       # noqa: E402
from src.stage5 import NO_CONTENT_SENTINEL                                        # noqa: E402
from src.stage5.fuse import build_fusion_inputs, route_document, FUSED, IMAGE_ONLY, PROMOTED, NO_CONTENT  # noqa: E402
from src.stage5.walk import run as run_stage5, dry_run                           # noqa: E402
from tests.make_stage5_fixtures import build_stage5, FAIL_TOKEN, CFILTER_TOKEN   # noqa: E402

FIXED_TS = "2026-06-19T00:00:00Z"
PROV = {"deployment": "fake-nano", "api_version": "fake-version"}
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, stem: str) -> dict:
    with open(os.path.join(root, stem + ".json"), "r", encoding="utf-8") as f:
        return json.load(f)


class FakeNano:
    """Deterministic fake for the fusion reduce. Raises on the failure/filter tokens so §10.7 is exercised
    without Azure; otherwise echoes a summary derived from the joined items (the user content)."""

    def __init__(self, fail_token: str | None = None, filter_token: str | None = None) -> None:
        self.calls = 0
        self.fail_token = fail_token
        self.filter_token = filter_token

    def complete_json(self, messages, schema, max_output_tokens):
        self.calls += 1
        user = messages[-1]["content"]
        if self.filter_token and self.filter_token in user:
            raise Stage3CallError("http 400: content management policy", "content_filter")
        if self.fail_token and self.fail_token in user:
            raise Stage3CallError("injected failure", "injected")
        data = {"summary": ("Fused: " + user.replace("\n", " ").strip())[:240]}
        return LLMResult(data, max(1, len(user) // 4), 25, 0)


def main() -> int:
    cfg = Stage5Config()
    root = tempfile.mkdtemp(prefix="tfe_s5_")
    build_stage5(root)
    try:
        # snapshot the provisional document_summary before the run (append-only check, §10.1)
        prov_before = {stem: load(root, stem).get("document_summary")
                       for stem in ("text_only", "fused", "review_text")}

        nano = FakeNano(fail_token=FAIL_TOKEN, filter_token=CFILTER_TOKEN)
        m = run_stage5(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=False, workers=1,
                       progress_every=0)

        # -- §10.1 append-only -----------------------------------------------------------------------
        print("\n== append-only: provisional untouched, final added ==")
        for stem, before in prov_before.items():
            sc = load(root, stem)
            check(sc.get("document_summary") == before,
                  f"{stem}: provisional document_summary byte-unchanged")
            check(sc.get("document_summary_final", {}).get("final") is True and "stage5" in sc,
                  f"{stem}: new document_summary_final + stage5 block written")

        # -- §10.2 routing correct -------------------------------------------------------------------
        print("\n== routing ==")
        to = load(root, "text_only")["document_summary_final"]
        check(to["source"] == PROMOTED and to["text"] == "Provisional summary of a two-page internal memo."
              and to["input_token_count"] == 0 and to["output_token_count"] == 0
              and to["covered_text_pages"] == 2 and to["covered_vision_assets"] == 0,
              "text-only -> promoted (0 tokens, copied provisional text, covered pages carried)")

        fu = load(root, "fused")["document_summary_final"]
        check(fu["source"] == FUSED and fu["covered_text_pages"] == 2 and fu["covered_vision_assets"] == 1
              and fu["text"].startswith("Fused:") and fu["input_token_count"] > 0,
              "fused -> one reduce over page summaries + the kept artifact (false asset excluded)")

        io = load(root, "image_only")["document_summary_final"]
        check(io["source"] == IMAGE_ONLY and io["covered_text_pages"] == 0
              and io["covered_vision_assets"] == 1 and io["text"].startswith("Fused:"),
              "image-only -> first summary from captions only (0 text pages)")

        em = load(root, "empty")["document_summary_final"]
        check(em["source"] == NO_CONTENT and em["text"] == NO_CONTENT_SENTINEL
              and em["no_content"] is True and em["safety"]["flag"] == "ok"
              and em["safety"]["content_filtered"] is False and em["input_token_count"] == 0,
              "empty -> sentinel, no_content, safety ok (0 calls)")

        qu = load(root, "quarantined")["document_summary_final"]
        check(qu["source"] == NO_CONTENT and qu["text"] == NO_CONTENT_SENTINEL and qu["no_content"] is True
              and qu["safety"]["flag"] == "review" and qu["safety"]["content_filtered"] is True,
              "quarantined -> sentinel + safety review + content_filtered (§4)")

        # -- §10.3 gate on has_visual_content (pages AND artifacts), not page_kind -------------------
        print("\n== gate on has_visual_content (artifact routes a text doc to fused) ==")
        fused_sc = load(root, "fused")
        route, kept, has_text = route_document(fused_sc)
        check(route == FUSED and has_text and len(kept) == 1 and kept[0]["kind"] == "artifact",
              "a text doc carrying a has_visual_content artifact routes to FUSED (not promote)")
        items, ct, cv = build_fusion_inputs(fused_sc)
        # page-ordered: p1 text summary, p1 image, p2 text summary; the false p3 image excluded.
        check(ct == 2 and cv == 1 and len(items) == 3
              and "page 1 — text summary" in items[0] and "page 1 — image" in items[1]
              and "page 2 — text summary" in items[2],
              "fusion inputs are page-ordered, interleave text+image, exclude the false asset")
        check("Photograph of two people at a desk." in items[1]
              and "black-and-white photo" in items[1].lower(),
              "image item folds BOTH caption and description (§11 decision)")

        # -- §10.5 doc-level safety rollup folds text review -----------------------------------------
        print("\n== doc-level safety rollup ==")
        rt = load(root, "review_text")["document_summary_final"]
        check(rt["source"] == PROMOTED and rt["safety"]["flag"] == "review"
              and rt["safety"]["content_filtered"] is False,
              "promoted doc with a text_safety=review page rolls up to flag=review (no content_filter)")

        # -- §10.6 provenance stamp records consumed stage3/stage4 versions --------------------------
        print("\n== provenance / consumed stamp ==")
        s5 = load(root, "fused")["stage5"]
        check(s5["deployment"] == "fake-nano" and s5["api_version"] == "fake-version"
              and s5["prompt_version"] and "endpoint" not in s5,
              "stage5 stamps deployment/api_version/prompt_version; no endpoint/key")
        cons = s5["consumed"]
        check(cons["stage3_prompt_version"] == "stage3-2026-06-16.1"
              and cons["stage4_prompt_version"] == "stage4-2026-06-19.2"
              and cons["stage4_tool_version"] == "1.0.0",
              "consumed stamp records the frozen stage3/stage4 versions")
        s5_text = load(root, "text_only")["stage5"]
        check(s5_text["consumed"]["stage4_prompt_version"] is None,
              "text-only doc (no stage4) -> consumed stage4 fields are null")

        # -- §10.7 fault isolation: a bad reduce is isolated -----------------------------------------
        print("\n== fault isolation (generic reduce error) ==")
        fail_sc = load(root, "fail_reduce")
        check("stage5" not in fail_sc and "document_summary_final" not in fail_sc
              and fail_sc.get("document_summary_final_error", {}).get("code") == "injected",
              "a generic reduce error leaves NO stage5/final (retryable) + records the error marker")
        check(m["pdfs_errored"] == 1 and m["pdfs_finalized"] == 7,
              "one bad doc is isolated; the rest of the corpus finalizes and the run completes")

        # content-filter on the reduce -> quarantine + logged. The doc HAD content (it was not empty), so
        # source stays image_only and no_content=False, but it is quarantined via the safety rollup.
        print("\n== content-filter on the reduce -> quarantine + logged ==")
        fr = load(root, "filter_reduce")["document_summary_final"]
        check(fr["text"] == NO_CONTENT_SENTINEL and fr["source"] == IMAGE_ONLY
              and fr["no_content"] is False and fr["safety"]["flag"] == "review"
              and fr["safety"]["content_filtered"] is True,
              "a content-filtered reduce quarantines the doc (sentinel + review + content_filtered)")
        check(m["content_filter_failures"] >= 1 and os.path.exists(
              os.path.join(root, "content-filter-failures.json")),
              "the blocked reduce is recorded in content-filter-failures.json")

        # -- manifest counts -------------------------------------------------------------------------
        print("\n== manifest counts ==")
        c = m["counts"]
        # image_only=2 -> image_only.pdf + the quarantined filter_reduce (source stays image_only);
        # no_content=2 -> empty + quarantined; fail_reduce errored (not finalized, not counted).
        check(c["promoted"] == 2 and c["fused"] == 1 and c["image_only"] == 2 and c["no_content"] == 2,
              f"manifest route tally correct (promoted={c['promoted']} fused={c['fused']} "
              f"image_only={c['image_only']} no_content={c['no_content']})")
        check(c["quarantined"] == 2 and c["safety_review"] == 3,
              "manifest tallies quarantined + safety_review")
        check(m["usage"]["prompt_tokens"] > 0 and m["usage"]["estimated_cost_usd"] > 0,
              f"manifest reports token totals + USD cost (${m['usage']['estimated_cost_usd']})")
        check(os.path.exists(os.path.join(root, "stage5-run-manifest.json")), "run manifest written")

        # -- §10.7 idempotency / resume / force ------------------------------------------------------
        print("\n== idempotency / resume / force ==")
        before_calls = nano.calls
        m2 = run_stage5(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=False, workers=1,
                        progress_every=0)
        # the fail_reduce doc has no stage5 block, so a no-force re-run RETRIES it (and fails again); the
        # already-finalized docs are skipped and cost nothing.
        check(m2["pdfs_skipped_already_done"] >= 6 and m2["counts"]["calls"] == 0,
              f"re-run skips finalized docs and makes no new reduce calls "
              f"({m2['pdfs_skipped_already_done']} skipped)")
        m3 = run_stage5(root, cfg, FIXED_TS, client=nano, provenance=PROV, force=True, workers=1,
                        progress_every=0)
        check(m3["pdfs_finalized"] >= 6 and nano.calls > before_calls,
              "--force re-finalizes every doc (new reduce calls made)")

        # -- §10.8 dry-run breakdown + cost (no API) -------------------------------------------------
        print("\n== dry-run cost preview ==")
        droot = tempfile.mkdtemp(prefix="tfe_s5_dry_")
        build_stage5(droot)
        try:
            d = dry_run(droot, cfg)
            check(d["promoted"] == 2 and d["fused"] == 1 and d["image_only"] == 3
                  and d["no_content"] == 2,
                  "dry-run breakdown matches routing (fail/filter docs both project as image_only reduces)")
            check(d["reduce_calls"] == 4 and d["estimated_cost_usd_realtime"] > 0,
                  "dry-run counts reduce calls + projects realtime nano $ (no API)")
        finally:
            shutil.rmtree(droot, ignore_errors=True)

        # -- concurrency parity (thread pool path) ---------------------------------------------------
        print("\n== concurrency parity ==")
        croot = tempfile.mkdtemp(prefix="tfe_s5_conc_")
        build_stage5(croot)
        try:
            cm = run_stage5(croot, cfg, FIXED_TS,
                            client=FakeNano(fail_token=FAIL_TOKEN, filter_token=CFILTER_TOKEN),
                            provenance=PROV, force=False, workers=4, progress_every=0)
            check(cm["counts"] == m["counts"],
                  "workers=4 scheduler produces the same route tally as workers=1")
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
