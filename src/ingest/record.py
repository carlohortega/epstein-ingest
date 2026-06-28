"""The full per-document **apply record** (offline, no DB/Storage) — the complete projection of what a live
ingest writes for one staged PDF: the ``documents`` typed columns + JSONB metadata bag, the ``document_pages``
rows, ``asset_context``, ``content_safety_findings``, the ordered child ``content_sha`` list, the §4a-resolved
blob plan, and the routed image vectors (``image_embeddings`` + ``image_occurrences``).

This is the data backbone of BOTH steps:
  * **S8 prepare** (``emit.py``) serializes an ``ApplyRecord`` into the apply-package (apply-plan.jsonl +
    ``*.copy`` vectors + blobs.manifest.jsonl), with NO creds.
  * **S9 apply** (``register.py``/``load.py``/``blobs.py``) replays it against the live system.

It is intentionally **tenant-agnostic**: the tenant_id, the tenant-scoped blob paths, and the embedding
target column are all chosen at apply (§6). So a record carries *logical* blob destinations
(case/dataset/document + artifact suffix + §4a action) and *un-tenanted* vectors; S9 projects them.

Reuses the validated offline core (``discover``/``safety``/``store_reader``) — same classifier as the
dry-run ``plan.py`` (CSAM short-circuits the whole document; high-confidence withhold only; bare ``review``
never triggers). Self-contained: imports no psycopg/azure/PyMuPDF.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field

from .config import IngestConfig
from .discover import sidecar_path, chunks_path, derive_identifiers, IdentifierError
from .safety import image_asset_verdict, page_is_blank, Verdict

# Deterministic artifact-id namespace (uuid5 over the image content_hash) so blob paths + occurrences are
# idempotent across re-prepares without minting random ids. Mirrors a stable per-image identity.
_ARTIFACT_NS = uuid.UUID("a5e9b1c2-0000-5000-8000-000000000001")

# §4a blob actions written into the blob plan. S9 resolves the tenant path then executes the action.
ACTION_UPLOAD = "upload"                 # azcopy the staged file to the tenant blob path
ACTION_PLACEHOLDER_REDACTED = "placeholder:content-redacted"   # withheld unit → shared placeholder
ACTION_PLACEHOLDER_BLANK = "placeholder:page-blank"            # blank page → gray placeholder

# document_pages.page_class CHECK vocabulary (0002). The merged sidecar's page-level ``page_class`` (Stage 1)
# uses this vocab; the Stage-2 ``page_kind`` (text/mixed/image/blank/redacted) is a DIFFERENT field and is
# NOT a valid page_class — so we only forward a page_class value when it is in this allow-set, else NULL.
_PAGE_CLASSES = {"ocr_backing_scan", "full_page_visual", "mixed_content", "embedded_subimage"}

# content_safety category-name → findings column. Azure Content Safety / vision category strings vary in
# case/punctuation; normalize then map. Unmapped categories contribute no severity column (still recorded
# in the rollup if you extend it), keeping the four typed columns faithful.
_CS_COL = {"hate": "hate", "sexual": "sexual", "violence": "violence",
           "self_harm": "self_harm", "self-harm": "self_harm", "selfharm": "self_harm"}


@dataclass
class BlobRef:
    """One file the live load must place at a tenant blob path. ``artifact`` is the suffix under the
    document's processed prefix (sv-kb ``blob_paths`` vocabulary). ``source_path`` is the staged file to
    azcopy (None for placeholder/csam actions). ``action`` is the §4a decision."""
    artifact: str                       # e.g. "source.pdf" | "ocr/page-3.png" | "artifacts/<uuid>/image.png"
    action: str                         # ACTION_UPLOAD | ACTION_PLACEHOLDER_*
    source_path: str | None = None      # staged absolute path (UPLOAD only)
    content_type: str | None = None


@dataclass
class ImageVector:
    """A routed image embedding (→ ``image_embeddings``) plus its occurrence (→ ``image_occurrences``).
    ``content_hash`` = sha256 of the image bytes (the dedup key); ``vector`` from the asset's ``.emb.json``."""
    content_hash: str
    dim: int
    embedding_model: str
    vector: list[float]
    artifact_kind: str                  # 'pdf_image' (artifact) — 'keyframe' is media-track only
    page_number: int                    # -1 = n/a
    artifact: str                       # the image blob suffix this occurrence points at


@dataclass
class ApplyRecord:
    """Everything ingest writes for one document — or the reason it is skipped."""
    rel: str
    status: str = "plan"                # plan | skip_status | skip_csam | no_sidecar | error
    error: str | None = None
    staged_pdf: str | None = None       # absolute staged PDF path — apply reads its chunks.json (§2 step 6)
    identifiers: dict = field(default_factory=dict)   # {document_name, case_key, dataset_key}

    # pipeline.documents — typed columns (apply sets tenant_id/case_id/dataset_id/pdf_blob_path)
    document: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)      # documents.metadata JSONB bag (provenance, lossless)

    pages: list = field(default_factory=list)         # document_pages rows (page_number + nullable cols)
    asset_context: dict | None = None                 # asset_context row (summary_text/json/model/version)
    safety_findings: list = field(default_factory=list)  # content_safety_findings rows

    content_shas: list = field(default_factory=list)  # ordered child content_sha (index order)
    vectors_present: int = 0
    vectors_missing: int = 0
    missing_shas: list = field(default_factory=list)

    blobs: list = field(default_factory=list)         # list[BlobRef]
    image_vectors: list = field(default_factory=list) # list[ImageVector]

    # §4a rollups
    source_withheld: bool = False
    images_withheld: int = 0
    images_embed: int = 0
    csam_assets: int = 0
    withheld_tiers: dict = field(default_factory=dict)
    blank_pages: int = 0


def _sha256_file(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _asset_abs(base_dir: str, asset: dict) -> str:
    return os.path.join(base_dir, str(asset.get("path") or "").replace("/", os.sep))


def _emb_abs(base_dir: str, asset: dict) -> str:
    return os.path.join(base_dir, str(asset.get("embedding_ref") or "").replace("/", os.sep))


def _mime(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".webp": "image/webp", ".pdf": "application/pdf"}.get(ext, "application/octet-stream")


def _metadata_bag(sc: dict) -> dict:
    """The lossless provenance bag (documents.metadata, GIN-indexed) — the SAME mapping sv-kb's normalize-job
    merges on upsert (Handoff §8): the sidecar's ``source`` (sha/bytes/producer/pdf_metadata), ``document``
    (bates/title), and ``generator`` (tool/version/config)."""
    src = sc.get("source") or {}
    doc = sc.get("document") or {}
    gen = sc.get("generator") or {}
    bag = {
        "source": {k: src.get(k) for k in ("sha256", "bytes", "pdf_producer", "pdf_metadata", "filename")
                   if src.get(k) is not None},
        "document": {k: doc.get(k) for k in ("bates_begin", "bates_end", "title", "content_type")
                     if doc.get(k) is not None},
        "generator": {k: gen.get(k) for k in ("tool", "tool_version", "pymupdf_version", "reconstruction",
                                              "generated_at_utc", "config") if gen.get(k) is not None},
    }
    return {k: v for k, v in bag.items() if v}


def _documents_row(sc: dict, ident: dict, cfg: IngestConfig) -> dict:
    """pipeline.documents typed columns we can source offline (apply adds tenant/case_id/dataset_id + blob
    paths). Mandatory (§3): case_key/dataset_key/document_name/source_kind — all present here."""
    src = sc.get("source") or {}
    doc = sc.get("document") or {}
    return {
        "case_key": ident["case_key"],
        "dataset_key": ident["dataset_key"],
        "document_name": ident["document_name"],
        "source_kind": doc.get("source_kind") or cfg.source_kind,
        "content_type": doc.get("content_type"),
        "filehash": src.get("sha256"),
        "page_count": src.get("page_count"),
        "title": doc.get("title"),
        "source": "upload",
    }


def _asset_context(sc: dict) -> dict | None:
    """asset_context from the Stage-5 ``document_summary_final`` (display text) + aggregated per-page
    ``summary_json`` (key_points / entities / topics). Native sv-kb leaves this for a later summarizer; we
    fill it at ingest (a divergence in our favor, §11)."""
    fin = sc.get("document_summary_final") or {}
    text = fin.get("text")
    if fin.get("no_content") or (fin.get("source") == "no_content"):
        text = None
    key_points: list = []
    entities: list = []
    topics: list = []
    for pg in (sc.get("pages") or []):
        sj = pg.get("summary_json") or {}
        key_points += sj.get("key_points") or []
        entities += sj.get("entities") or []
        topics += sj.get("topics") or []
    summary_json = {
        "source": fin.get("source"),
        "covered_text_pages": fin.get("covered_text_pages"),
        "covered_vision_assets": fin.get("covered_vision_assets"),
        "key_points": key_points[:50],
        "entities": entities[:100],
        "topics": sorted(set(t for t in topics if t))[:30],
    }
    summary_json = {k: v for k, v in summary_json.items() if v not in (None, [], {})}
    if not text and not summary_json:
        return None
    stage5 = sc.get("stage5") or {}
    return {"summary_text": text, "summary_json": summary_json,
            "model": stage5.get("model") or (sc.get("stage3") or {}).get("model"), "version": 1}


def _severities(asset: dict) -> tuple[dict, bool]:
    """(per-category severity columns, csam_suspected) for one asset, from its content_safety + vision safety."""
    cols = {"hate": 0, "sexual": 0, "violence": 0, "self_harm": 0}
    cs = asset.get("content_safety") or {}
    for c in (cs.get("categories") or []):
        col = _CS_COL.get(str(c.get("category") or "").lower())
        if col:
            try:
                cols[col] = max(cols[col], int(c.get("severity") or 0))
            except (TypeError, ValueError):
                pass
    saf = asset.get("safety") or {}
    csam = bool(saf.get("minor_indicator") is True)
    return cols, csam


def _findings(sc: dict, ident: dict) -> list:
    """content_safety_findings rows (§4a parity data model) — one per scanned unit that carries a signal.
    Pages contribute ``page_text`` from their ``text_safety``; assets contribute ``artifact_image`` /
    ``page_image`` from their content_safety + vision safety. Indistinguishable to the reader from native."""
    out: list = []
    for pg in (sc.get("pages") or []):
        ts = pg.get("text_safety") or {}
        if ts.get("flag") == "review" or ts.get("content_filtered"):
            out.append({"modality": "page_text", "locator": f"page:{pg.get('page_number')}",
                        "hate": 0, "sexual": 0, "violence": 0, "self_harm": 0, "csam_suspected": False,
                        "model": (sc.get("stage3") or {}).get("model")})
    for a in (sc.get("image_assets") or []):
        cols, csam = _severities(a)
        if not any(cols.values()) and not csam and not (a.get("safety") or {}).get("explicit"):
            continue
        modality = "page_image" if a.get("kind") == "page" else "artifact_image"
        loc = f"page:{a.get('page_number')}" if a.get("kind") == "page" else f"artifact:{a.get('index')}"
        out.append({"modality": modality, "locator": loc, **cols, "csam_suspected": csam,
                    "model": (sc.get("stage4") or {}).get("model")})
    return out


def _page_rows(sc: dict, page_withheld: set) -> list:
    out: list = []
    for pg in (sc.get("pages") or []):
        n = pg.get("page_number")
        pc = pg.get("page_class")
        ts = pg.get("text_safety") or {}
        out.append({
            "page_number": n,
            "page_class": pc if pc in _PAGE_CLASSES else None,
            "ocr_text": ((pg.get("text") or {}).get("content")) or None,
            "summary": pg.get("summary"),
            "summary_json": pg.get("summary_json") or {},
            "safety_tags": ts or None,
            "review_status": "withheld" if n in page_withheld else "none",
        })
    return out


def build_apply_record(pdf_path: str, root: str, cfg: IngestConfig, store) -> ApplyRecord:
    """Build the full tenant-agnostic apply record for one staged PDF (or the skip reason). Mirrors
    ``plan.plan_document``'s classification exactly, then materializes the register payloads + blob plan +
    routed image vectors that ``plan_document`` only counts."""
    rel = os.path.relpath(pdf_path, root).replace(os.sep, "/")
    r = ApplyRecord(rel)
    r.staged_pdf = os.path.abspath(pdf_path)
    try:
        with open(sidecar_path(pdf_path), "r", encoding="utf-8") as f:
            sc = json.load(f)
    except FileNotFoundError:
        r.status = "no_sidecar"; return r
    except Exception as exc:
        r.status = "error"; r.error = f"bad_sidecar: {type(exc).__name__}"; return r
    if sc.get("status") != "ok":
        r.status = "skip_status"; return r

    try:
        document_name, case_key, dataset_key = derive_identifiers(pdf_path, cfg.case_key)
    except IdentifierError as exc:
        r.status = "error"; r.error = f"no_identifiers: {str(exc)[:120]}"; return r
    r.identifiers = {"document_name": document_name, "case_key": case_key, "dataset_key": dataset_key}

    base_dir = os.path.dirname(pdf_path)
    assets = sc.get("image_assets") or []

    # --- §4a classification (CSAM short-circuits the whole document) ---
    verdicts: list[tuple[dict, Verdict]] = []
    for a in assets:
        v = image_asset_verdict(a, threshold=cfg.content_safety_threshold,
                                withhold_on_missing=cfg.redact_on_missing_verdict)
        verdicts.append((a, v))
        if v.csam:
            r.csam_assets += 1
    if r.csam_assets:
        r.status = "skip_csam"                       # WHOLE doc NOT ingested; preserve + report upstream
        return r

    page_withheld: set = set()
    for a, v in verdicts:
        if v.withhold:
            r.images_withheld += 1
            t = v.tier or "unknown"
            r.withheld_tiers[t] = r.withheld_tiers.get(t, 0) + 1
            if a.get("page_number") is not None:
                page_withheld.add(a.get("page_number"))
    r.source_withheld = r.images_withheld > 0

    # --- register payloads ---
    r.document = _documents_row(sc, r.identifiers, cfg)
    r.metadata = _metadata_bag(sc)
    r.asset_context = _asset_context(sc)
    r.safety_findings = _findings(sc, r.identifiers)
    r.pages = _page_rows(sc, page_withheld)
    r.blank_pages = sum(1 for pg in (sc.get("pages") or []) if page_is_blank(pg))

    # --- canonical source PDF blob ---
    r.blobs.append(BlobRef("source.pdf", ACTION_UPLOAD, pdf_path, "application/pdf"))

    # --- per-asset blobs + routed image vectors (withheld → placeholder + vector ABSENT) ---
    for a, v in verdicts:
        page_no = a.get("page_number") if a.get("page_number") is not None else -1
        if a.get("kind") == "page" and isinstance(a.get("page_number"), int):
            suffix = f"ocr/page-{a['page_number']}.png"
        else:
            # deterministic artifact id from the image content (stable across re-prepares)
            chash_for_path = a.get("content_hash") or (a.get("path") or f"idx{a.get('index')}")
            aid = str(uuid.uuid5(_ARTIFACT_NS, str(chash_for_path)))
            suffix = f"artifacts/{aid}/image.png"
        if v.withhold:
            r.blobs.append(BlobRef(suffix, ACTION_PLACEHOLDER_REDACTED))
            continue                                  # vector ABSENT — the asset never existed for retrieval
        src_png = _asset_abs(base_dir, a)
        if a.get("has_visual_content") is True and a.get("embedding_ref") and os.path.exists(src_png):
            content_hash = _sha256_file(src_png)
            emb = _read_emb(_emb_abs(base_dir, a))
            if content_hash and emb:
                r.image_vectors.append(ImageVector(
                    content_hash=content_hash, dim=emb["dim"], embedding_model=emb["model"],
                    vector=emb["vector"], artifact_kind="pdf_image", page_number=page_no, artifact=suffix))
                r.images_embed += 1
            r.blobs.append(BlobRef(suffix, ACTION_UPLOAD, src_png, _mime(src_png)))
        elif os.path.exists(src_png):
            r.blobs.append(BlobRef(suffix, ACTION_UPLOAD, src_png, _mime(src_png)))

    # --- blank-page placeholder renders ---
    for pg in (sc.get("pages") or []):
        if page_is_blank(pg) and isinstance(pg.get("page_number"), int):
            r.blobs.append(BlobRef(f"ocr/page-{pg['page_number']}.png", ACTION_PLACEHOLDER_BLANK))

    # --- child content_sha (ordered) + vector presence ---
    shas = _ordered_child_shas(pdf_path)
    r.content_shas = shas
    if shas and store.exists():
        present = store.get_many(set(shas))
        r.vectors_present = sum(1 for s in shas if s in present)
        r.missing_shas = [s for s in shas if s not in present]
    else:
        r.missing_shas = list(shas)
    r.vectors_missing = len(r.missing_shas)
    return r


def _read_emb(path: str) -> dict | None:
    """Read a Stage-4 ``*.emb.json`` ({embedding_model, model_version, dim, vector}). Returns a normalized
    {model, dim, vector} or None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        vec = d.get("vector")
        if not isinstance(vec, list) or not vec:
            return None
        return {"model": d.get("embedding_model") or "azure-ai-vision-multimodal",
                "dim": int(d.get("dim") or len(vec)), "vector": [float(x) for x in vec]}
    except (OSError, ValueError, TypeError):
        return None


def _ordered_child_shas(pdf_path: str) -> list:
    """The document's child ``content_sha`` in chunk order (parents then children), deduped preserving
    order — the set ``index_document`` will look up in ``pipeline.embeddings`` (pre-load must cover them)."""
    out: list = []
    seen: set = set()
    cpath = chunks_path(pdf_path)
    if not os.path.exists(cpath):
        return out
    try:
        with open(cpath, "r", encoding="utf-8") as f:
            parents = json.load(f)
    except (OSError, ValueError):
        return out
    for par in parents:
        for c in (par.get("children") or []):
            s = c.get("content_sha")
            if s and s not in seen:
                seen.add(s)
                out.append(s)
    return out
