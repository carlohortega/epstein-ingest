"""Stage-4 acceptance tests (no pytest, no network). Mirrors HANDOFF-stage4 §10 against synthetic
fixtures, driving the stage with an INJECTED fake vision client / embedder / content-safety scanner (model
output isn't byte-reproducible, so we inject deterministic stubs instead of calling Azure).

The fake vision client DECODES the image it is sent and decides blank-vs-content from its pixels — exactly
what the gate must get right — so the rendered PNG is the oracle (Handoff §10.2). Covers: the
has_visual_content gate (vision path + both pre-gate paths), page_kind demotion, embeddings only for kept
assets, content-safety on artifacts, content-filter handling, fault isolation, idempotent resume, the
concurrent scheduler, and the dry-run cost preview.

Run:  python tests/run_stage4_tests.py     (project root on PYTHONPATH)
Exit code 0 = all passed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image                                                      # noqa: E402

from src.stage1.walk import sidecar_path                                  # noqa: E402
from src.stage4.config import Stage4Config                                # noqa: E402
from src.stage4.client import LLMResult, VisionCallError                  # noqa: E402
from src.stage4.walk import run as run_stage4, dry_run                    # noqa: E402
from tests.make_stage4_fixtures import build_stage4                       # noqa: E402

FIXED_TS = "2026-06-19T00:00:00Z"
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, rel: str) -> dict:
    with open(sidecar_path(os.path.join(root, rel)), "r", encoding="utf-8") as f:
        return json.load(f)


def _dark_fraction(raw: bytes, level: int = 64) -> float:
    im = Image.open(io.BytesIO(raw)).convert("L")
    hist = im.histogram()
    total = sum(hist) or 1
    return sum(hist[: level + 1]) / total


# --- deterministic fake vision client --------------------------------------------------------------
class FakeVision:
    def __init__(self, raise_filter: bool = False, raise_fail: bool = False) -> None:
        self.calls = 0
        self._lock = threading.Lock()
        self.raise_filter = raise_filter
        self.raise_fail = raise_fail

    def complete_json(self, messages, schema, max_output_tokens, *, image_tokens=0):
        with self._lock:
            self.calls += 1
        if self.raise_filter:
            raise VisionCallError("http 400: content management policy", "content_filter")
        if self.raise_fail:
            raise VisionCallError("injected failure", "injected")
        # find the image content block and decide from its pixels
        url = ""
        for part in messages[-1]["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = part["image_url"]["url"]
        raw = base64.b64decode(url.split(",", 1)[1]) if "," in url else b""
        dark = _dark_fraction(raw)
        if dark < 0.02:
            data = {"caption": "Blank page.", "description": "This page appears blank.",
                    "content_class": "blank", "has_visual_content": False, "vision_confidence": 0.95,
                    "safety": {"flag": "ok", "categories": [], "minor_indicator": False,
                               "explicit": False}}
        else:
            data = {"caption": "A scanned document page with dark figures.",
                    "description": "The page shows several solid dark blocks and a filled shape.",
                    "content_class": "document_scan", "has_visual_content": True,
                    "vision_confidence": 0.9,
                    "safety": {"flag": "ok", "categories": [], "minor_indicator": False,
                               "explicit": False}}
        return LLMResult(data, max(1, image_tokens), 30, 0)


class FakeEmbedder:
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, image_bytes: bytes):
        self.calls += 1
        return [0.1, 0.2, 0.3, 0.4], "fake-model-version"


class FakeScanner:
    def __init__(self, flag: str = "ok", severity: int = 0) -> None:
        self.calls = 0
        self.flag = flag
        self.severity = severity

    def analyze(self, image_bytes: bytes) -> dict:
        self.calls += 1
        return {"status": "ok", "flag": self.flag, "max_severity": self.severity,
                "categories": [{"category": "Sexual", "severity": self.severity}]}


def prep() -> str:
    root = tempfile.mkdtemp(prefix="tfe_s4_")
    build_stage4(root)
    return root


PROV_FULL = {"deployment": "fake-vision", "api_version": "fake-ver",
             "embedding_model": "azure-ai-vision-multimodal", "content_safety": "azure-content-safety"}


def main() -> int:
    # ===== Run A: full pipeline, pre-gate ON, embeddings + content-safety ON =========================
    print("\n== Run A: full pipeline (pregate on, embeddings + content-safety on) ==")
    root = prep()
    vis, emb, scan = FakeVision(), FakeEmbedder(), FakeScanner()
    m = run_stage4(root, Stage4Config(), FIXED_TS, client=vis, embedder=emb, scanner=scan,
                   provenance=PROV_FULL, force=False, workers=1, progress_every=0)
    a = load(root, "alpha.pdf")
    assets = {x["path"]: x for x in a["image_assets"]}
    p1 = assets["alpha/images/page-0001.png"]
    p2 = assets["alpha/images/page-0002.png"]
    p3 = assets["alpha/images/page-0003.png"]
    p4 = assets["alpha/artifacts/page-0004-img-01.png"]

    # NOTE: manifest counts + the fake call counters are CORPUS-WIDE (alpha's 4 assets + beta's 1).
    check(m["counts"]["assets"] == 5, "corpus has 5 image assets (alpha 4 + beta 1)")
    check(vis.calls == 3, f"one vision call per NON-pregated asset (alpha 2 + beta 1 = 3, got {vis.calls})")

    # §10.2 the gate is correct against the PNG oracle
    check(p1["has_visual_content"] is True and p1["verdict_source"] == "vision" and p1.get("caption"),
          "content image PAGE -> has_visual_content true (vision) + caption")
    check(p2["has_visual_content"] is False and p2["verdict_source"] == "pregate"
          and p2["content_class"] == "blank" and p2["prefilter_hint"] == "likely_blank",
          "blank image PAGE -> METRIC pre-gate -> has_visual_content false, no vision call")
    check(p3["has_visual_content"] is False and p3["verdict_source"] == "pregate",
          "escalated near-white page -> PNG-measured pre-gate -> has_visual_content false")
    check(p4["has_visual_content"] is True and p4["verdict_source"] == "vision",
          "content ARTIFACT -> has_visual_content true (vision)")

    # §10.1/§5 embeddings only for kept assets; §2 page_kind demotion on a rejected image page
    check(bool(p1.get("embedding_ref")) and bool(p4.get("embedding_ref")), "kept assets got embedding_ref")
    check(p2.get("embedding_ref") is None and p3.get("embedding_ref") is None,
          "pre-gated blanks got NO embedding (embedding_ref null)")
    emb_file = os.path.join(root, p1["embedding_ref"].replace("/", os.sep))
    check(os.path.isfile(emb_file) and json.load(open(emb_file)).get("dim") == 4,
          "embedding vector written to its sibling file")
    pg2 = next(p for p in a["pages"] if p["page_number"] == 2)
    check(pg2["page_kind"] == "blank" and "vision_blank" in (pg2.get("flags") or []),
          "rejected full-page image -> page_kind demoted to blank (+vision_blank flag)")
    pg3 = next(p for p in a["pages"] if p["page_number"] == 3)
    check(pg3["page_kind"] == "text", "escalated text page is NOT demoted (page_kind stays text)")

    # §4 content safety on artifacts only (default)
    check("content_safety" in p4 and p4["content_safety"]["status"] == "ok",
          "Content Safety ran on the ARTIFACT")
    check("content_safety" not in p1, "Content Safety did NOT run on a full image page (artifacts-only)")
    # corpus-wide: scanner only fires on alpha's artifact (beta p1 is a page); embedder on the 3 kept assets.
    check(scan.calls == 1 and emb.calls == 3,
          f"scanner called once (artifact only); embedder called 3x (kept assets), got {scan.calls}/{emb.calls}")
    check(m["counts"]["has_visual_content_true"] == 3 and m["counts"]["pregated_blank"] == 2
          and m["counts"]["embedded"] == 3 and m["counts"]["content_safety_scanned"] == 1,
          "manifest counts (corpus): 3 kept / 2 pregated / 3 embedded / 1 CS-scanned")

    # safety shape: ok|review only, with minor_indicator + explicit
    s = p1["safety"]
    check(s["flag"] in ("ok", "review") and "minor_indicator" in s and "explicit" in s,
          "safety has flag in {ok,review} + minor_indicator + explicit (no 'block')")

    # §10.5 idempotent resume: a second run is a no-op (no new vision calls)
    print("\n== Run A': resume is a no-op ==")
    vis2 = FakeVision()
    m2 = run_stage4(root, Stage4Config(), FIXED_TS, client=vis2, embedder=FakeEmbedder(),
                    scanner=FakeScanner(), provenance=PROV_FULL, force=False, workers=1, progress_every=0)
    check(vis2.calls == 0 and m2["pdfs_skipped_already_done"] >= 1,
          "re-run skips already-done docs and makes 0 vision calls")

    # ===== Run B: pre-gate OFF -> vision adjudicates the blanks; demotion via VISION path ============
    print("\n== Run B: pregate OFF (vision sees every asset) ==")
    rootB = prep()
    visB = FakeVision()
    run_stage4(rootB, Stage4Config(pregate=False), FIXED_TS, client=visB, embedder=FakeEmbedder(),
               scanner=FakeScanner(), provenance=PROV_FULL, force=False, workers=1, progress_every=0)
    b = load(rootB, "alpha.pdf")
    bassets = {x["path"]: x for x in b["image_assets"]}
    check(visB.calls == 5, f"pregate off -> a vision call for every asset (alpha 4 + beta 1 = 5, got {visB.calls})")
    bp2 = bassets["alpha/images/page-0002.png"]
    check(bp2["has_visual_content"] is False and bp2["verdict_source"] == "vision",
          "blank image page rejected via the VISION path when pre-gate is off")
    bpg2 = next(p for p in b["pages"] if p["page_number"] == 2)
    check(bpg2["page_kind"] == "blank", "vision-rejected image page is demoted to blank")

    # ===== Run C: content-filter block is a first-class outcome =====================================
    print("\n== Run C: content-filter block handled, logged, no crash ==")
    rootC = prep()
    mC = run_stage4(rootC, Stage4Config(), FIXED_TS, client=FakeVision(raise_filter=True),
                    embedder=FakeEmbedder(), scanner=FakeScanner(), provenance=PROV_FULL, force=False,
                    workers=1, progress_every=0)
    beta = load(rootC, "beta.pdf")["image_assets"][0]
    check(beta.get("content_filtered") is True and beta["safety"]["flag"] == "review"
          and not beta.get("caption"), "blocked asset -> content_filtered + safety review + no caption")
    check(beta.get("embedding_ref") is None, "blocked asset got no embedding")
    check(mC["content_filter_failures"] >= 1 and os.path.isfile(mC["content_filter_log"]),
          "content-filter failure logged to content-filter-failures-stage4.json")

    # ===== Run D: fault isolation — a vision error skips the asset, run survives =====================
    print("\n== Run D: vision error is isolated (run completes) ==")
    rootD = prep()
    mD = run_stage4(rootD, Stage4Config(), FIXED_TS, client=FakeVision(raise_fail=True),
                    embedder=FakeEmbedder(), scanner=FakeScanner(), provenance=PROV_FULL, force=False,
                    workers=1, progress_every=0)
    betaD = load(rootD, "beta.pdf")["image_assets"][0]
    check(betaD.get("stage4_error", {}).get("code") == "injected" and mD["counts"]["errored"] >= 1,
          "a failed vision call records stage4_error and the run still finalizes")

    # ===== Run E: embeddings OFF (no embedder) ======================================================
    print("\n== Run E: embeddings off ==")
    rootE = prep()
    mE = run_stage4(rootE, Stage4Config(embeddings=False), FIXED_TS, client=FakeVision(), embedder=None,
                    scanner=FakeScanner(), provenance={**PROV_FULL, "embedding_model": None}, force=False,
                    workers=1, progress_every=0)
    eP1 = {x["path"]: x for x in load(rootE, "alpha.pdf")["image_assets"]}["alpha/images/page-0001.png"]
    check(mE["counts"]["embedded"] == 0 and eP1.get("embedding_ref") is None,
          "no embedder -> embedded=0 and embedding_ref null")

    # ===== Run F: content-safety OFF (no scanner) ===================================================
    print("\n== Run F: content-safety off ==")
    rootF = prep()
    mF = run_stage4(rootF, Stage4Config(), FIXED_TS, client=FakeVision(), embedder=FakeEmbedder(),
                    scanner=None, provenance={**PROV_FULL, "content_safety": "not_configured"},
                    force=False, workers=1, progress_every=0)
    fP4 = {x["path"]: x for x in load(rootF, "alpha.pdf")["image_assets"]}["alpha/artifacts/page-0004-img-01.png"]
    check(mF["counts"]["content_safety_scanned"] == 0 and "content_safety" not in fP4,
          "no scanner -> content-safety skipped, no content_safety field")

    # ===== Run G: concurrent scheduler (workers>1) finalizes correctly ==============================
    print("\n== Run G: concurrent scheduler ==")
    rootG = prep()
    visG = FakeVision()
    mG = run_stage4(rootG, Stage4Config(), FIXED_TS, client=visG, embedder=FakeEmbedder(),
                    scanner=FakeScanner(), provenance=PROV_FULL, force=False, workers=3, progress_every=0)
    check(mG["pdfs_processed"] == 2 and visG.calls == 3,
          f"scheduler processed both docs with 3 vision calls (alpha:2 + beta:1, got {visG.calls})")

    # ===== dry-run cost preview (no API) ============================================================
    print("\n== dry-run cost preview ==")
    rootH = prep()
    d = dry_run(rootH, Stage4Config())
    check(d["eligible_assets"] == 5 and d["pregated_blank_metric"] == 1 and d["vision_calls"] == 4,
          f"dry-run: 5 eligible, 1 metric-pregated, 4 vision (got {d})")
    check(d["estimated_cost_usd_realtime"] > 0 and d["estimated_cost_usd_batch"]
          < d["estimated_cost_usd_realtime"], "dry-run projects a positive realtime cost; batch is cheaper")

    print(f"\n{'='*60}\n{'ALL PASSED' if not _failures else f'{len(_failures)} FAILURE(S)'}")
    for f in _failures:
        print(f"  FAIL: {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
