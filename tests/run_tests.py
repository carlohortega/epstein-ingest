"""Self-contained acceptance tests (no pytest dependency). Mirrors Spec §12 against synthetic fixtures.

Run:  python tests/run_tests.py        (with the project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stage1.config import Config           # noqa: E402
from src.stage1.walk import run, sidecar_path   # noqa: E402
from tests.make_fixtures import build_all, SECRET, BATES_BLANK, PARTIAL_SECRET   # noqa: E402

FIXED_TS = "2026-06-15T00:00:00Z"
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    status = "PASS" if cond else "FAIL"
    print(f"  [{status}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, rel: str) -> dict:
    with open(sidecar_path(os.path.join(root, rel)), "r", encoding="utf-8") as f:
        return json.load(f)


def page0(doc: dict) -> dict:
    return doc["pages"][0]


def main() -> int:
    root = tempfile.mkdtemp(prefix="tfe_test_")
    try:
        build_all(root)
        cfg = Config()
        manifest = run(root, cfg, FIXED_TS, force=False, workers=1)

        print("\n== routing: born_digital ==")
        d = load(root, "text_born_digital.pdf")
        check(d["status"] == "ok", "born_digital status ok")
        check(page0(d)["needs_vision"] is False, "born_digital page needs_vision=False")
        check(page0(d)["metrics"]["text_layer_type"] in ("born_digital", "mixed"),
              f"born_digital layer type ({page0(d)['metrics']['text_layer_type']})")
        check("committee" in page0(d)["text"]["content"], "born_digital content present")

        print("\n== routing: ocr_layer ==")
        d = load(root, "VOL00012/IMAGES/ocr_layer.pdf")
        check(page0(d)["needs_vision"] is False, "ocr_layer page needs_vision=False")
        check(page0(d)["metrics"]["text_layer_type"] == "ocr_layer",
              f"ocr_layer detected ({page0(d)['metrics']['text_layer_type']})")
        check(page0(d)["text"]["format"] == "structured_text", "ocr_layer format=structured_text")
        check(page0(d)["text"]["invisible_chars"] > 0, "ocr_layer has invisible glyphs")

        print("\n== redaction safety (the #1 requirement) ==")
        d = load(root, "VOL00012/IMAGES/redacted_leak.pdf")
        p = page0(d)
        check(p["redaction"]["suspected"] is True, "redacted page suspected=True")
        check(p["needs_vision"] is True and p["needs_vision_reason"] == "redaction_suspected",
              "redacted page routed to vision (redaction_suspected)")
        check(p["redaction"]["scrubbed_char_count"] > 0, "redacted page scrubbed some chars")
        check(SECRET not in p["text"]["content"], "under-bar SECRET token NOT in emitted content")
        check("[REDACTED]" in p["text"]["content"], "under-bar text replaced with [REDACTED] marker")

        # glyph-level: a bar over only the middle token must mark it but keep the outer words
        d = load(root, "VOL00012/IMAGES/partial_redaction.pdf")
        p = page0(d)
        check(PARTIAL_SECRET not in p["text"]["content"], "partial-bar: under-bar token removed")
        check("PUBLIC" in p["text"]["content"] and "[REDACTED]" in p["text"]["content"],
              f"partial-bar: outer words survive + [REDACTED] inserted (content={p['text']['content']!r})")
        check(p["redaction"]["redaction_runs"] >= 1, "partial-bar: redaction_runs >= 1")
        check(p["redaction"]["suspected"] is True and p["needs_vision"] is True,
              "partial-bar: page flagged + routed to vision")

        # RASTER bar (burned into an image, not a vector rect) — positive detection
        d = load(root, "VOL00012/IMAGES/raster_redaction.pdf")
        p = page0(d)
        c = p["text"]["content"]
        check("BRAVO" not in c and "[REDACTED]" in c, f"raster-bar: BRAVO->[REDACTED] (content={c!r})")
        check("ALPHA" in c and "CHARLIE" in c, "raster-bar: outer words survive")
        check(p["redaction"]["dark_occluder_rects"] == 0 and p["redaction"]["suspected"] is True,
              "raster-bar: detected via render (no vector rect) and flagged")

        # DARK PHOTO (textured) — must NOT be treated as a redaction (the real-data false positive)
        d = load(root, "VOL00012/IMAGES/dark_photo.pdf")
        p = page0(d)
        c = p["text"]["content"]
        check(p["redaction"]["suspected"] is False, "dark-photo: NOT flagged as redaction")
        check("[REDACTED]" not in c, "dark-photo: no spurious [REDACTED] marker")
        check("ONE" in c and "THREE" in c, f"dark-photo: text-over-photo preserved (content={c!r})")

        print("\n== garbage rejection (microfilm quality gate) ==")
        d = load(root, "garbage_microfilm.pdf")
        p = page0(d)
        check(p["needs_vision"] is True, "garbage page needs_vision=True")
        check(p["needs_vision_reason"] == "quality_fail",
              f"garbage rejected by quality gate ({p['needs_vision_reason']})")
        check(p["metrics"]["quality"]["pass"] is False, "garbage fails quality metrics")

        print("\n== figure detection ==")
        d = load(root, "figure_page.pdf")
        p = page0(d)
        check(p["metrics"]["embedded_image_count"] >= 1, "figure page has an embedded image")
        check(p["needs_vision"] is True, "figure page needs_vision=True")
        check("image_safety_required" in p["flags"], "figure page flags image_safety_required")

        print("\n== blank / slip-sheet detection ==")
        d = load(root, "VOL00012/IMAGES/blank_slip.pdf")
        p = page0(d)
        check(p["needs_vision"] is False and p["needs_vision_reason"] == "blank",
              f"blank slip routed away from vision (reason={p['needs_vision_reason']})")
        check(p["metrics"]["ink_coverage"] is not None and p["metrics"]["ink_coverage"] < Config().blank_ink_max,
              f"blank slip ink_coverage below threshold ({p['metrics']['ink_coverage']})")
        check(p["metrics"]["bates_stamp"] == BATES_BLANK,
              f"blank slip Bates captured ({p['metrics']['bates_stamp']})")
        check("blank_page" in p["flags"] or "slip_sheet" in p["flags"], "blank slip flagged")

        d = load(root, "VOL00012/IMAGES/black_blank.pdf")
        p = page0(d)
        check(p["needs_vision"] is False and p["needs_vision_reason"] == "blank",
              f"black blank routed away from vision (reason={p['needs_vision_reason']})")
        check(p["metrics"]["dark_coverage"] is not None
              and p["metrics"]["dark_coverage"] > 1.0 - Config().blank_ink_max,
              f"black blank dark_coverage above threshold ({p['metrics']['dark_coverage']})")
        check("blank_black" in p["flags"], f"black blank flagged blank_black ({p['flags']})")

        d = load(root, "VOL00012/IMAGES/scanned_content.pdf")
        p = page0(d)
        check(p["needs_vision"] is True and p["needs_vision_reason"] == "low_text",
              f"dark scan with content NOT mistaken for blank (reason={p['needs_vision_reason']})")
        check(p["metrics"]["ink_coverage"] is not None and p["metrics"]["ink_coverage"] >= Config().blank_ink_max,
              f"dark scan ink_coverage above threshold ({p['metrics']['ink_coverage']})")

        print("\n== ink_coverage stays null for text-rich pages ==")
        d = load(root, "VOL00012/IMAGES/ocr_layer.pdf")
        check(page0(d)["metrics"]["ink_coverage"] is None, "ocr_layer page not rendered for blank-check")

        print("\n== robustness: encrypted + corrupt ==")
        d = load(root, "encrypted.pdf")
        check(d["status"] == "error" and d["error"]["code"] == "encrypted",
              f"encrypted -> status error/encrypted ({d['status']}/{d.get('error')})")
        d = load(root, "corrupt.pdf")
        check(d["status"] == "error" and d["error"]["code"] == "corrupt",
              f"corrupt -> status error/corrupt ({d['status']}/{d.get('error')})")

        print("\n== manifest ==")
        check(manifest["files_seen"] == 13, f"manifest files_seen=13 ({manifest['files_seen']})")
        check(manifest["files_errored"] == 2, f"manifest files_errored=2 ({manifest['files_errored']})")
        check(manifest["pages_redaction_flagged"] >= 1, "manifest counts a redaction-flagged page")
        check(manifest["pages_blank"] >= 2, "manifest counts both white and black blank pages")

        print("\n== determinism (byte-identical except timestamp) ==")
        before = {}
        for dirpath, _dn, files in os.walk(root):
            for fn in files:
                if fn.endswith(".json") and fn != "extract-run-manifest.json":
                    fp = os.path.join(dirpath, fn)
                    with open(fp, "rb") as f:
                        before[fp] = f.read()
        run(root, cfg, FIXED_TS, force=True, workers=1)
        identical = True
        for fp, data in before.items():
            with open(fp, "rb") as f:
                if f.read() != data:
                    identical = False
                    print(f"      changed: {fp}")
        check(identical, "re-run with same generated_at -> byte-identical sidecars")

        print("\n== idempotency / resume ==")
        m2 = run(root, cfg, FIXED_TS, force=False, workers=1)
        check(m2["files_skipped"] == 13 and m2["files_extracted"] == 0,
              f"unchanged files skipped on resume ({m2['files_skipped']} skipped, {m2['files_extracted']} extracted)")

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
