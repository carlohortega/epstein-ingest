"""Registration (Handoff §2 steps 2–3, §3, §8) — ``ingest`` OWNS the rows ``index_document`` will NOT create.

Given a tenant-agnostic apply-plan record and the resolved ``case_id`` / ``dataset_id``, write (idempotently,
inside the caller's ``db.tenant`` transaction):
  * ``pipeline.documents``               — typed columns + the JSONB **metadata bag** + §4a rollups
  * ``pipeline.document_pages``           — one row per page (page_number + nullable sidecar fields)
  * ``pipeline.asset_context``            — summary_text / summary_json (filled at ingest, §11)
  * ``pipeline.content_safety_findings``  — per-unit severities (§4a parity data model)
  * ``pipeline.document_progress``        — so ``index_document``'s status UPDATE has a row to flip

Returns the ``document_id`` (so ``index_document`` finds the row and doesn't raise ``document_missing``).
Blob paths are computed up-front from the tenant path builder (the bytes upload happens in ``blobs.py``).
Lazy sv-kb / psycopg imports — this module is only touched on a live apply.
"""

from __future__ import annotations


def _rollup(findings: list) -> dict | None:
    """Element-wise max across a document's findings → the documents.content_safety FE rollup contract."""
    if not findings:
        return None
    roll = {"hate": 0, "sexual": 0, "violence": 0, "self_harm": 0, "csam_suspected": False}
    for f in findings:
        for k in ("hate", "sexual", "violence", "self_harm"):
            roll[k] = max(roll[k], int(f.get(k) or 0))
        roll["csam_suspected"] = roll["csam_suspected"] or bool(f.get("csam_suspected"))
    return roll


def register_document(cur, plan: dict, *, tenant_id: str, case_id: str, dataset_id: str,
                      ingestion_id: str, correlation_id: str, source_kind: str, path_builder) -> str:
    """Insert/upsert the document + child rows; return the ``document_id``."""
    from psycopg.types.json import Jsonb                    # lazy: psycopg only on a live apply

    ident = plan["identifiers"]
    case_key, dataset_key, document_name = ident["case_key"], ident["dataset_key"], ident["document_name"]
    doc = plan.get("document") or {}
    source_withheld = bool(plan.get("source_withheld"))
    review_status = "withheld" if source_withheld else "none"

    pdf_loc = path_builder.source_pdf(case_key, dataset_key, document_name)
    pdf_blob_path = pdf_loc.path
    processed_prefix = pdf_blob_path.rsplit("/", 1)[0] + "/"
    rollup = _rollup(plan.get("content_safety_findings") or [])

    cur.execute(
        """INSERT INTO pipeline.documents
             (tenant_id, case_id, dataset_id, case_key, dataset_key, document_name, source_kind,
              content_type, source, filehash, page_count, title, metadata, content_safety, safety_status,
              csam_suspected, review_status, source_withheld, pdf_blob_path, processed_prefix,
              correlation_id, ingested_by_user_sub)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'upload',%s,%s,%s,%s,%s,'scanned',false,%s,%s,%s,%s,%s,NULL)
           ON CONFLICT (tenant_id, case_id, dataset_id, document_name) DO UPDATE SET
             metadata = pipeline.documents.metadata || EXCLUDED.metadata,
             content_safety = EXCLUDED.content_safety, safety_status = EXCLUDED.safety_status,
             review_status = EXCLUDED.review_status, source_withheld = EXCLUDED.source_withheld,
             pdf_blob_path = EXCLUDED.pdf_blob_path, processed_prefix = EXCLUDED.processed_prefix,
             updated_at = now()
           RETURNING document_id""",
        (tenant_id, case_id, dataset_id, case_key, dataset_key, document_name, source_kind,
         doc.get("content_type"), doc.get("filehash"), doc.get("page_count"), doc.get("title"),
         Jsonb(plan.get("metadata") or {}), Jsonb(rollup) if rollup else None,
         review_status, source_withheld, pdf_blob_path, processed_prefix, correlation_id))
    document_id = str(cur.fetchone()[0])

    for pg in (plan.get("pages") or []):
        cur.execute(
            """INSERT INTO pipeline.document_pages
                 (tenant_id, document_id, page_number, page_class, ocr_text, summary, summary_json,
                  safety_tags, review_status)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, document_id, page_number) DO UPDATE SET
                 page_class = EXCLUDED.page_class, ocr_text = EXCLUDED.ocr_text,
                 summary = EXCLUDED.summary, summary_json = EXCLUDED.summary_json,
                 safety_tags = EXCLUDED.safety_tags, review_status = EXCLUDED.review_status,
                 updated_at = now()""",
            (tenant_id, document_id, pg.get("page_number"), pg.get("page_class"), pg.get("ocr_text"),
             pg.get("summary"), Jsonb(pg.get("summary_json") or {}),
             Jsonb(pg.get("safety_tags")) if pg.get("safety_tags") else None,
             pg.get("review_status") or "none"))

    ac = plan.get("asset_context")
    if ac:
        cur.execute(
            """INSERT INTO pipeline.asset_context
                 (document_id, tenant_id, summary_text, summary_json, model, version)
               VALUES (%s,%s,%s,%s,%s,%s)
               ON CONFLICT (document_id) DO UPDATE SET
                 summary_text = EXCLUDED.summary_text, summary_json = EXCLUDED.summary_json,
                 model = EXCLUDED.model, version = EXCLUDED.version, updated_at = now()""",
            (document_id, tenant_id, ac.get("summary_text"), Jsonb(ac.get("summary_json") or {}),
             ac.get("model"), ac.get("version") or 1))

    for f in (plan.get("content_safety_findings") or []):
        cur.execute(
            """INSERT INTO pipeline.content_safety_findings
                 (tenant_id, document_id, modality, locator, hate, sexual, violence, self_harm,
                  csam_suspected, model)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (tenant_id, document_id, modality, locator) DO NOTHING""",
            (tenant_id, document_id, f.get("modality"), f.get("locator"), int(f.get("hate") or 0),
             int(f.get("sexual") or 0), int(f.get("violence") or 0), int(f.get("self_harm") or 0),
             bool(f.get("csam_suspected")), f.get("model")))

    # progress row so index_document's UPDATE (status='complete', current_stage='index') has a target
    cur.execute(
        """INSERT INTO pipeline.document_progress
             (ingestion_id, tenant_id, document_id, correlation_id, status, current_stage)
           VALUES (%s,%s,%s,%s,'indexing','register')
           ON CONFLICT (ingestion_id) DO NOTHING""",
        (ingestion_id, tenant_id, document_id, correlation_id))

    return document_id
