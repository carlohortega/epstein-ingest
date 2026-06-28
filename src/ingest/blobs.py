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

**Batched upload (scale).** Spawning one ``azcopy`` per blob is fine for a canary but dies at corpus scale
(hundreds of thousands of files = that many process startups). Instead we build a **symlink farm** — a temp
tree mirroring the exact ``{container}/{blob_path}`` layout, each leaf a symlink to the staged source (or the
§4a placeholder) — then run a **single** ``azcopy copy --recursive --follow-symlinks`` over it, letting azcopy
parallelize the transfers internally. Symlinks (not copies) mean no byte duplication, even when one placeholder
fans out to thousands of paths. (Falls back to a file copy where symlinks aren't permitted, e.g. Windows.)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

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
                 endpoint: str | None = None, azcopy: str = "azcopy", dry: bool = False) -> None:
        from svkb_pipeline.blob_paths import TenantPathBuilder   # lazy: sv-kb on path only at apply
        self.builder = TenantPathBuilder(tenant_id)
        self.account = account or os.environ.get("AZURE_STORAGE_ACCOUNT")
        self.sas = sas or os.environ.get("AZURE_STORAGE_SAS")
        # Explicit blob endpoint for non-default hosts (Azurite dev emulator, sovereign clouds). When set it
        # supersedes the public `{account}.blob.core.windows.net` host — e.g. http://127.0.0.1:10000/devstoreaccount1
        self.endpoint = endpoint or os.environ.get("AZURE_STORAGE_BLOB_ENDPOINT")
        self.azcopy = azcopy
        self.dry = dry
        if not self.account and not self.endpoint and not dry:
            raise RuntimeError("set AZURE_STORAGE_ACCOUNT (or AZURE_STORAGE_BLOB_ENDPOINT) to build blob URLs")
        self._farm: str | None = None      # temp symlink tree, created lazily on first stage
        self._staged = 0

    def _container_url(self, container: str) -> str:
        root = self.endpoint.rstrip("/") if self.endpoint else f"https://{self.account}.blob.core.windows.net"
        base = f"{root}/{container}"
        return f"{base}?{self.sas.lstrip('?')}" if self.sas else base

    def _doc_prefix(self, case_key: str, dataset_key: str, document_name: str) -> str:
        # processed prefix = the document's source.pdf path minus the trailing "source.pdf"
        return self.builder.source_pdf(case_key, dataset_key, document_name).path[: -len("source.pdf")]

    def _stage(self, container: str, blob_path: str, src_local: str | None) -> bool:
        """Link ``src_local`` into the symlink farm at ``{farm}/{container}/{blob_path}`` (no bytes copied)."""
        if self.dry or not src_local or not os.path.exists(src_local):
            return False
        if self._farm is None:
            self._farm = tempfile.mkdtemp(prefix="ingest-blobs-")
        dest = os.path.join(self._farm, container, blob_path.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.lexists(dest):
            os.remove(dest)
        try:
            os.symlink(os.path.abspath(src_local), dest)
        except OSError:
            shutil.copy2(src_local, dest)        # symlinks not permitted (Windows w/o dev mode) → copy
        self._staged += 1
        return True

    def seed_placeholders(self) -> None:
        """Stage the shared placeholders once so the gateway's redaction path resolves after flush."""
        for local in _PLACEHOLDER_LOCAL.values():
            name = os.path.basename(local)
            self._stage(PROCESSED_CONTAINER,
                        f"tenants/{self.builder.tenant_id}/{_SYSTEM_REDACTION_PREFIX}/{name}", local)

    def stage_document(self, rows: list) -> int:
        """Stage every blob for one document into the farm (cheap symlinks). Upload happens later in ``flush``.
        ``upload`` rows link the staged file; ``placeholder:*`` rows (withheld/blank) link the placeholder."""
        if not rows:
            return 0
        r0 = rows[0]
        prefix = self._doc_prefix(r0["case_key"], r0["dataset_key"], r0["document_name"])
        n = 0
        for row in rows:
            blob_path = f"{prefix}{row['artifact']}"
            src = row.get("source_path") if row["action"] == "upload" else _PLACEHOLDER_LOCAL.get(row["action"])
            if self._stage(PROCESSED_CONTAINER, blob_path, src):
                n += 1
        return n

    def flush(self) -> dict:
        """One ``azcopy copy --recursive --follow-symlinks`` over the whole staged farm → the container."""
        if self.dry or self._farm is None:
            return {"uploaded": self._staged}
        src_dir = os.path.join(self._farm, PROCESSED_CONTAINER)
        if not os.path.isdir(src_dir):
            return {"uploaded": 0}
        cmd = [self.azcopy, "copy", src_dir.replace(os.sep, "/") + "/*",
               self._container_url(PROCESSED_CONTAINER), "--recursive", "--follow-symlinks",
               "--from-to=LocalBlob", "--overwrite=ifSourceNewer", "--log-level=ERROR"]
        subprocess.run(cmd, check=True, capture_output=True)
        return {"uploaded": self._staged}

    def close(self) -> None:
        if self._farm and os.path.isdir(self._farm):
            shutil.rmtree(self._farm, ignore_errors=True)
        self._farm = None


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
