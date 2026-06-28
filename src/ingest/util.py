"""Local deterministic JSON writer (same bytes as ``stage1.walk.dump_json`` / ``embed.util.dump_json``),
kept local so ingest's offline core imports no PyMuPDF."""

from __future__ import annotations

import json
import os


def dump_json(obj: dict, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)
