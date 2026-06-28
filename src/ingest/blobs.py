"""Blob upload (Handoff §2 step 4, §4a) — drive **azcopy** from the package's ``blobs.manifest.jsonl``.

Each manifest row is one file to place under the tenant's processed prefix. The §4a ``action`` decides the
source bytes:
  * ``upload``                    → the staged file (source PDF, page/artifact PNG) is copied as-is.
  * ``placeholder:content-redacted`` → a WITHHELD unit: the staged bytes are NEVER uploaded; the shared
                                    ``content-redacted`` placeholder is written at the unit's path instead
                                    (the real image never exists for retrieval/serving). Vector already absent.
  * ``placeholder:page-blank``    → a blank PDF page → the gray ``page-blank`` placeholder.

The tenant-scoped destination path is resolved HERE (the package is tenant-agnostic) via sv-kb's
``TenantPathBuilder`` so every blob is rooted at ``tenants/{tenant_id}/`` by construction. Also seeds the
shared placeholders under ``_system/redaction/`` once. Lazy sv-kb import; azcopy is shelled out.
"""

from __future__ import annotations

import json
import os
import subprocess

from .safety import _ASSETS_DIR

PROCESSED_CONTAINER = "sv-kb-processed"
_PLACEHOLDER_LOCAL = {
    "placeholder:content-redacted": os.path.join(_ASSETS_DIR, "content-redacted.png"),
    "placeholder:page-blank": os.path.join(_ASSETS_DIR, "page-blank.png"),
}
_SYSTEM_REDACTION_PREFIX = "_system/redaction"


class BlobUploader:
    """Resolves tenant blob URLs and runs azcopy. Source of account/SAS is the environment (launcher loads
    ``AZURE_STORAGE_ACCOUNT`` + optional ``AZURE_STORAGE_SAS``; without a SAS, azcopy must already be
    ``azcopy login``'d with a managed identity)."""

    def __init__(self, tenant_id: str, *, account: str | None = None, sas: str | None = None,
                 azcopy: str = "azcopy", dry: bool = False) -> None:
        from svkb_pipeline.blob_paths import TenantPathBuilder   # lazy: sv-kb on path only at apply
        self.builder = TenantPathBuilder(tenant_id)
        self.account = account or os.environ.get("AZURE_STORAGE_ACCOUNT")
        self.sas = sas or os.environ.get("AZURE_STORAGE_SAS")
        self.azcopy = azcopy
        self.dry = dry
        if not self.account and not dry:
            raise RuntimeError("AZURE_STORAGE_ACCOUNT not set — required to build blob destination URLs")

    def _url(self, container: str, blob_path: str) -> str:
        base = f"https://{self.account}.blob.core.windows.net/{container}/{blob_path}"
        return f"{base}?{self.sas.lstrip('?')}" if self.sas else base

    def _doc_prefix(self, case_key: str, dataset_key: str, document_name: str) -> str:
        # processed prefix = the document's source.pdf path minus the trailing "source.pdf"
        return self.builder.source_pdf(case_key, dataset_key, document_name).path[: -len("source.pdf")]

    def _copy(self, src_local: str, container: str, blob_path: str) -> None:
        if self.dry:
            return
        cmd = [self.azcopy, "copy", src_local, self._url(container, blob_path),
               "--overwrite=ifSourceNewer", "--log-level=ERROR"]
        subprocess.run(cmd, check=True, capture_output=True)

    def seed_placeholders(self) -> None:
        """Upload the shared placeholders once (idempotent) so the gateway's redaction path resolves."""
        for action, local in _PLACEHOLDER_LOCAL.items():
            name = os.path.basename(local)
            self._copy(local, PROCESSED_CONTAINER, f"tenants/{self.builder.tenant_id}/{_SYSTEM_REDACTION_PREFIX}/{name}")

    def upload_document(self, rows: list) -> dict:
        """Place every blob for one document. ``rows`` are the manifest entries for that document."""
        if not rows:
            return {"uploaded": 0, "placeholders": 0}
        r0 = rows[0]
        prefix = self._doc_prefix(r0["case_key"], r0["dataset_key"], r0["document_name"])
        uploaded = placeholders = 0
        for row in rows:
            blob_path = f"{prefix}{row['artifact']}"
            action = row["action"]
            if action == "upload":
                src = row.get("source_path")
                if not src or not os.path.exists(src):
                    continue
                self._copy(src, PROCESSED_CONTAINER, blob_path)
                uploaded += 1
            else:
                local = _PLACEHOLDER_LOCAL.get(action)
                if not local:
                    continue
                self._copy(local, PROCESSED_CONTAINER, blob_path)   # withheld/blank → placeholder bytes
                placeholders += 1
        return {"uploaded": uploaded, "placeholders": placeholders}


def group_manifest_by_document(pkg_dir: str) -> dict:
    """Read ``blobs.manifest.jsonl`` and group rows by ``(case_key, dataset_key, document_name)`` so each
    document's blobs upload together."""
    out: dict = {}
    path = os.path.join(pkg_dir, "blobs.manifest.jsonl")
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out.setdefault((row["case_key"], row["dataset_key"], row["document_name"]), []).append(row)
    return out
