"""The content-filter failure log (``content-filter-failures.json``).

When Azure's content-management policy blocks a page (Handoff §6 / canary finding), Stage 3 records it as
a decisive ``review`` signal on the sidecar AND appends it here so an operator has a single, durable list
of everything that was rejected — file, page, timestamp, and the error — to formulate a policy/exemption
solution later. The log MERGES across runs (dedup on file+page), so re-runs and resumes accumulate rather
than overwrite, and a run with zero rejections leaves an existing log untouched.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json
from . import STAGE3_TOOL_NAME, STAGE3_TOOL_VERSION


def default_path(root: str) -> str:
    return os.path.join(root, "content-filter-failures.json")


def _load_existing(path: str) -> dict[tuple, dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[tuple, dict] = {}
    for e in (doc.get("failures") or []):
        out[(e.get("file"), e.get("page_number"))] = e
    return out


def merge_write(path: str, entries: list[dict], generated_at: str) -> int:
    """Merge ``entries`` (each: file, page_number, error_code, error_message) into the log at ``path``,
    stamping each with ``generated_at``. Returns the total failure count after the merge. A no-op write is
    still performed when there are new entries; if there are none and no file exists, nothing is written."""
    existing = _load_existing(path)
    if not entries and not existing:
        return 0
    for e in entries:
        key = (e.get("file"), e.get("page_number"))
        existing[key] = {
            "file": e.get("file"),
            "page_number": e.get("page_number"),
            "timestamp": generated_at,
            "error_code": e.get("error_code"),
            "error_message": e.get("error_message"),
        }
    failures = sorted(existing.values(), key=lambda x: (x.get("file") or "", x.get("page_number") or 0))
    dump_json({
        "tool": STAGE3_TOOL_NAME,
        "tool_version": STAGE3_TOOL_VERSION,
        "description": "Pages rejected by Azure OpenAI content-management policy during Stage 3. Each is "
                       "flagged text_safety.review on its sidecar; resolve via a content-filter "
                       "modification/exemption, then re-run --force on these files.",
        "updated_at_utc": generated_at,
        "count": len(failures),
        "failures": failures,
    }, path)
    return len(failures)
