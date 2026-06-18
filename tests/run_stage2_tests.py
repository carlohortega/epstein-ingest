"""Stage-2 acceptance tests (no pytest dependency). Mirrors HANDOFF-stage2 §9 against synthetic fixtures.

Runs Stage 1 to produce sidecars, then Stage 2 to render/classify/register, and asserts the page_kind
classification, artifact dedup, image_assets manifest, local-only output, determinism and idempotency.

Run:  python tests/run_stage2_tests.py     (project root on PYTHONPATH; see run.sh)
Exit code 0 = all passed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stage1.config import Config                       # noqa: E402
from src.stage1.walk import run as run_stage1, sidecar_path  # noqa: E402
from src.stage2.config import Stage2Config           # noqa: E402
from src.stage2.walk import run as run_stage2        # noqa: E402
from tests.make_fixtures import build_all                           # noqa: E402
from tests.make_stage2_fixtures import build_stage2, REDACT_BATES   # noqa: E402

FIXED_TS = "2026-06-15T00:00:00Z"
_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {msg}")
    if not cond:
        _failures.append(msg)


def load(root: str, rel: str) -> dict:
    with open(sidecar_path(os.path.join(root, rel)), "r", encoding="utf-8") as f:
        return json.load(f)


def kind_of(doc: dict, page_index: int = 0) -> str:
    return doc["pages"][page_index]["page_kind"]


def snapshot(root: str) -> dict:
    """All rendered PNGs + sidecar JSONs (not the run manifests) -> {path: bytes}, for determinism checks."""
    out = {}
    for dp, _dn, files in os.walk(root):
        for fn in files:
            if fn.endswith(".png") or (fn.endswith(".json") and "run-manifest" not in fn):
                fp = os.path.join(dp, fn)
                with open(fp, "rb") as f:
                    out[fp] = f.read()
    return out


def main() -> int:
    root = tempfile.mkdtemp(prefix="tfe_s2_")
    try:
        build_all(root)
        build_stage2(root)
        run_stage1(root, Config(), FIXED_TS, force=False, workers=1)
        s2cfg = Stage2Config()
        m = run_stage2(root, s2cfg, FIXED_TS, force=False, workers=1)

        # -- §9.2 page_kind correct on a sample ------------------------------------------------------
        print("\n== page_kind classification ==")
        check(kind_of(load(root, "text_born_digital.pdf")) == "text", "born-digital text page -> text")
        check(kind_of(load(root, "text_with_logo.pdf")) == "text", "text + small logo -> text (not image)")
        check(kind_of(load(root, "VOL00012/IMAGES/scanned_content.pdf")) == "image",
              "full-page scan, low text -> image")
        check(kind_of(load(root, "figure_page.pdf")) == "mixed", "figure-on-text page -> mixed")
        check(kind_of(load(root, "VOL00012/IMAGES/blank_slip.pdf")) == "blank", "white slip sheet -> blank")
        check(kind_of(load(root, "VOL00012/IMAGES/black_blank.pdf")) == "blank", "black scanner blank -> blank")

        # -- §9.3 redacted-with-text-layer is text/mixed, NOT image (the cost win) --------------------
        print("\n== redaction routing (the cost win) ==")
        leak = load(root, "VOL00012/IMAGES/redacted_leak.pdf")
        check(kind_of(leak) in ("text", "mixed"),
              f"redacted page WITH a real text layer -> {kind_of(leak)} (not image -> not vision)")
        fr = load(root, "fully_redacted.pdf")
        p = fr["pages"][0]
        check(p["page_kind"] == "redacted", f"page-covering bars + footer only -> redacted ({p['page_kind']})")
        check(REDACT_BATES in (p["text"]["content"]),
              "fully-redacted page keeps its Bates footer as the only readable text")
        check(all(a["page_number"] != 1 or a["kind"] != "page" for a in fr["image_assets"]),
              "redacted page is NOT registered as an image asset (no vision)")

        # -- quality_fail page kept in the text lane but flagged low_quality_text --------------------
        print("\n== quality_fail -> text + low_quality_text flag ==")
        gm = load(root, "garbage_microfilm.pdf")["pages"][0]
        check(gm["needs_vision_reason"] == "quality_fail" and gm["page_kind"] == "text",
              f"garbage-OCR page (quality_fail, >=min_chars) stays in text lane ({gm['page_kind']})")
        check("low_quality_text" in gm.get("flags", []),
              "quality_fail text page is flagged low_quality_text for Stage 6/7 to decide")
        check(m["text_quality_suspect"] >= 1, "manifest tallies text_quality_suspect")

        # -- §9.1 every page rendered once; image->images/, others->pages/ ----------------------------
        print("\n== render placement & per-page fields ==")
        scan = load(root, "VOL00012/IMAGES/scanned_content.pdf")
        sp = scan["pages"][0]
        check(sp["render_path"].endswith("scanned_content/images/page-0001.png"),
              f"image page rendered into images/ ({sp['render_path']})")
        check(sp["render_dpi"] == s2cfg.render_dpi, f"image page render_dpi recorded ({sp['render_dpi']})")
        blank = load(root, "VOL00012/IMAGES/blank_slip.pdf")["pages"][0]
        check("/pages/" in blank["render_path"], f"blank page rendered into pages/ ({blank['render_path']})")
        rl = load(root, "repeated_logo.pdf")
        check(len(rl["pages"]) == 3 and all("/pages/" in pg["render_path"] for pg in rl["pages"]),
              "every page of a 3-page text PDF rendered into pages/")

        # -- §9.4 artifacts deduped: one file, N occurrences -----------------------------------------
        print("\n== artifact extraction & dedup ==")
        arts = [a for a in rl["image_assets"] if a["kind"] == "artifact"]
        check(len(arts) == 1, f"repeated logo deduped to a single artifact file ({len(arts)})")
        if arts:
            check(len(arts[0]["occurrences"]) == 3,
                  f"the single artifact records all 3 occurrences ({len(arts[0]['occurrences'])})")
            art_dir = os.path.join(root, "repeated_logo", "artifacts")
            n_files = len([f for f in os.listdir(art_dir) if f.endswith(".png")]) if os.path.isdir(art_dir) else 0
            check(n_files == 1, f"exactly one PNG on disk in artifacts/ ({n_files})")
        check(rl["stage2"]["counts"]["artifacts"] == 1, "stage2.counts.artifacts == 1 (unique kept)")

        # -- §9.5 image_assets lists exactly image-pages + kept artifacts, all paths resolve ---------
        print("\n== image_assets manifest integrity ==")
        all_ok = True
        page_assets = artifact_assets = 0
        for dp, _dn, files in os.walk(root):
            for fn in files:
                if not fn.endswith(".json") or "run-manifest" in fn:
                    continue
                d = json.load(open(os.path.join(dp, fn), encoding="utf-8"))
                if d.get("status") != "ok" or "image_assets" not in d:
                    continue
                for a in d["image_assets"]:
                    page_assets += a["kind"] == "page"
                    artifact_assets += a["kind"] == "artifact"
                    resolved = os.path.exists(os.path.join(dp, a["path"]))
                    all_ok = all_ok and resolved and not os.path.isabs(a["path"])
                # blank/redacted pages must never appear as a page asset
                kinds = {pg["page_kind"] for pg in d["pages"]}
                listed_pages = {a["page_number"] for a in d["image_assets"] if a["kind"] == "page"}
                for pg in d["pages"]:
                    if pg["page_kind"] in ("blank", "redacted", "text", "mixed"):
                        all_ok = all_ok and pg["page_number"] not in listed_pages
        check(all_ok, "every image_assets path is relative and resolves; only image pages are page-assets")
        check(page_assets >= 1 and artifact_assets >= 1,
              f"manifest registered page-assets ({page_assets}) and artifact-assets ({artifact_assets})")

        # -- thumbnails: one per ok PDF --------------------------------------------------------------
        print("\n== thumbnails ==")
        thumb_ok = True
        for rel in ("text_born_digital.pdf", "fully_redacted.pdf", "repeated_logo.pdf"):
            d = load(root, rel)
            base_dir = os.path.dirname(sidecar_path(os.path.join(root, rel)))  # the PDF's own directory
            thumb_ok = thumb_ok and d["thumbnail_path"].endswith("/thumbnail.png") \
                and os.path.exists(os.path.join(base_dir, d["thumbnail_path"]))
        check(thumb_ok, "one thumbnail.png generated per PDF and present on disk")

        # -- §9.6 local only + manifest vision workload ----------------------------------------------
        print("\n== run manifest & local-only ==")
        check(m["image_pages"] >= 1 and m["redacted_pages"] >= 1 and m["blank_pages"] >= 2,
              f"manifest tallies kinds (image={m['image_pages']}, redacted={m['redacted_pages']}, "
              f"blank={m['blank_pages']})")
        check(m["vision_workload"] == m["image_pages"] + m["artifacts_kept"],
              f"vision_workload = image_pages + artifacts_kept ({m['vision_workload']})")
        check(os.path.exists(os.path.join(root, "stage2-run-manifest.json")), "stage2 run manifest written")

        # -- §9.6 determinism: re-run yields byte-identical renders + JSON ---------------------------
        print("\n== determinism ==")
        before = snapshot(root)
        run_stage2(root, s2cfg, FIXED_TS, force=True, workers=1)
        after = snapshot(root)
        identical = before.keys() == after.keys() and all(before[k] == after[k] for k in before)
        if not identical:
            for k in before:
                if k not in after or before[k] != after[k]:
                    print(f"      changed: {k}")
        check(identical, "force re-run reproduces byte-identical PNGs and sidecars")

        # -- §5 DPI clamp: large-format pages render under the pixel ceiling (OOM guard) --------------
        print("\n== render DPI clamp ==")
        import fitz
        from src.stage2.render import clamp_render_dpi
        small = fitz.open(); small_pg = small.new_page(width=612, height=792)      # US Letter
        big = fitz.open(); big_pg = big.new_page(width=34 * 72, height=44 * 72)     # E-size drawing
        check(clamp_render_dpi(small_pg, 200, s2cfg.max_render_pixels, s2cfg.min_render_dpi) == 200,
              "a normal page is never clamped (renders at full DPI)")
        clamped = clamp_render_dpi(big_pg, 200, s2cfg.max_render_pixels, s2cfg.min_render_dpi)
        big_px = (34 * clamped) * (44 * clamped)
        check(clamped < 200 and big_px <= s2cfg.max_render_pixels,
              f"a large-format page is clamped below the pixel ceiling (dpi {clamped})")
        small.close(); big.close()

        # -- §9.7 idempotency: re-run without --force skips completed PDFs ----------------------------
        print("\n== idempotency / resume ==")
        m3 = run_stage2(root, s2cfg, FIXED_TS, force=False, workers=1)
        check(m3["pdfs_rendered"] == 0 and m3["pdfs_skipped_already_done"] == m["pdfs_rendered"],
              f"resume skips all completed PDFs ({m3['pdfs_skipped_already_done']} skipped, "
              f"{m3['pdfs_rendered']} rendered)")

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
