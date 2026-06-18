"""Pretty-print key fields of a few sidecars + the manifest for a demo run."""
import json
import os
import sys

root = sys.argv[1]


def show(rel):
    path = os.path.join(root, os.path.splitext(rel)[0] + ".json")
    d = json.load(open(path))
    print(f"\n===== {rel} =====")
    print("status:", d["status"], "| reconstruction:", d["generator"]["reconstruction"])
    if d["status"] != "ok":
        print("error:", d["error"])
        return
    print("summary:", json.dumps(d["extraction_summary"]))
    p = d["pages"][0]
    print("page_class:", p["page_class"], "| needs_vision:", p["needs_vision"],
          "| reason:", p["needs_vision_reason"])
    print("metrics:", json.dumps(p["metrics"]))
    print("redaction:", json.dumps(p["redaction"]))
    print("text.format:", p["text"]["format"], "| char_count:", p["text"]["char_count"])
    print("content[:140]:", repr(p["text"]["content"][:140]))


for rel in ["VOL00012/IMAGES/ocr_layer.pdf", "VOL00012/IMAGES/redacted_leak.pdf",
            "figure_page.pdf", "encrypted.pdf"]:
    show(rel)

print("\n===== extract-run-manifest.json =====")
print(json.dumps(json.load(open(os.path.join(root, "extract-run-manifest.json"))), indent=2))
