"""The Stage-4 content-filter failure log (``content-filter-failures.json``), asset-level.

When Azure's content policy blocks an IMAGE at the vision call (Handoff §5), Stage 4 records it as a
decisive ``review`` signal on the asset AND appends it here so an operator has a single durable list of
everything rejected — file, asset path, timestamp, error — to feed the CSAM/minors quarantine + any
mandatory-reporting process. The log MERGES across runs (dedup on file+asset_path), so re-runs accumulate
rather than overwrite, and a run with zero rejections leaves an existing log untouched.

Stage 4 writes its OWN log file by default so it never collides with the Stage-3 (page-level) log.
"""

from __future__ import annotations

import json
import os

from ..stage1.walk import dump_json
from . import STAGE4_TOOL_NAME, STAGE4_TOOL_VERSION


def default_path(root: str) -> str:
    return os.path.join(root, "content-filter-failures-stage4.json")


def _load_existing(path: str) -> dict[tuple, dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    out: dict[tuple, dict] = {}
    for e in (doc.get("failures") or []):
        out[(e.get("file"), e.get("asset_path"))] = e
    return out


def merge_write(path: str, entries: list[dict], generated_at: str) -> int:
    """Merge ``entries`` (each: file, asset_path, error_code, error_message) into the log at ``path``,
    stamping each with ``generated_at``. Returns the total failure count after the merge."""
    existing = _load_existing(path)
    if not entries and not existing:
        return 0
    for e in entries:
        key = (e.get("file"), e.get("asset_path"))
        existing[key] = {
            "file": e.get("file"),
            "asset_path": e.get("asset_path"),
            "timestamp": generated_at,
            "error_code": e.get("error_code"),
            "error_message": e.get("error_message"),
        }
    failures = sorted(existing.values(), key=lambda x: (x.get("file") or "", x.get("asset_path") or ""))
    dump_json({
        "tool": STAGE4_TOOL_NAME,
        "tool_version": STAGE4_TOOL_VERSION,
        "description": "Image assets rejected by Azure's content policy during the Stage-4 vision call. "
                       "Each is flagged safety.flag=review + content_filtered on its sidecar and belongs "
                       "to the CSAM/minors quarantine bucket. Minors-related content stays quarantined; "
                       "adult high-severity recovery needs the Azure Limited Access modified content "
                       "filter, then re-run --force on these files.",
        "updated_at_utc": generated_at,
        "count": len(failures),
        "failures": failures,
    }, path)
    return len(failures)
