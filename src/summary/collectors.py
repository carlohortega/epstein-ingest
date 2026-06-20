"""The collector registry — sidecar -> compact per-doc *digest* (Handoff §2/§5).

This is the extensible heart of the scanner. Each collector reads one block and returns a small, flat,
JSON-serializable partial dict; ``extract_digest`` merges them. Adding a metric (or a Stage 7) is a localized
change: write a collector, register it, bump ``SCHEMA_VERSION``. We return only the extracted *numbers*,
never the whole sidecar — the digest is what crosses the process boundary and lands in the cache, so it must
stay tiny (Handoff §5/§6).

Everything is defensive: a missing/renamed/half-typed field yields ``None``/0, never an exception. Presence
of a block is the done-signal for its stage (§2); the counts inside it are the metrics."""

from __future__ import annotations


def _i(v) -> int:
    """Coerce to int, treating None/garbage as 0 (counts must sum cleanly across millions of docs)."""
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0


def _f(v) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0


def _g(d, *path):
    """Nested get: _g(sc, 'stage3', 'usage', 'calls') -> value or None, never raising on a non-dict hop."""
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def collect_base(sc: dict) -> dict:
    """Stage 1 / base: health, identifiers-irrelevant volume, routing. Done-signal for S1 = a sidecar with a
    ``status`` set (Stage 1 has no block — it IS the base document)."""
    status = sc.get("status")
    src = sc.get("source") or {}
    es = sc.get("extraction_summary") or {}
    return {
        "status": status if status in ("ok", "error") else (status or "unknown"),
        "error_code": _g(sc, "error", "code") if status == "error" else None,
        "rel": src.get("relative_path"),                    # for the quarantine/safety list (§4)
        "source_kind": _g(sc, "document", "source_kind") or "pdf",   # PDF today; media track later (§7)
        "page_count": _i(src.get("page_count")),
        "bytes": _i(src.get("bytes")),
        "has_image_assets": bool(sc.get("image_assets")),
        # volume / content
        "text_chars": _i(es.get("total_text_chars")),
        "visible_chars": _i(es.get("visible_chars")),
        "invisible_chars": _i(es.get("invisible_chars")),
        "scrubbed_chars": _i(es.get("scrubbed_chars")),
        "pages_text_sufficient": _i(es.get("pages_text_sufficient")),
        "pages_need_vision": _i(es.get("pages_need_vision")),
        "pages_redaction_flagged": _i(es.get("pages_redaction_flagged")),
        "pages_blank": _i(es.get("pages_blank")),
        # routing (Stage-1 recommendation: text|mixed|vision)
        "routing": es.get("routing_recommendation"),
    }


def collect_stage2(sc: dict) -> dict:
    s2 = sc.get("stage2")
    if not isinstance(s2, dict):
        return {"s2": False}
    c = s2.get("counts") or {}
    return {
        "s2": True,
        "s2_pages_rendered": _i(c.get("pages_rendered")),
        "s2_artifacts": _i(c.get("artifacts")),
        "s2_vision_workload": _i(c.get("vision_workload")),
    }


def collect_stage3(sc: dict) -> dict:
    s3 = sc.get("stage3")
    if not isinstance(s3, dict):
        return {"s3": False}
    c = s3.get("counts") or {}
    u = s3.get("usage") or {}
    return {
        "s3": True,
        "s3_pages_summarized": _i(c.get("pages_summarized")),
        "s3_pages_errored": _i(c.get("pages_errored")),
        "s3_low_confidence": _i(c.get("low_confidence")),
        "s3_safety_review": _i(c.get("safety_review")),
        "s3_content_filtered": _i(c.get("content_filtered")),
        "s3_prompt_tokens": _i(u.get("prompt_tokens")),
        "s3_completion_tokens": _i(u.get("completion_tokens")),
        "s3_calls": _i(u.get("calls")),
        "s3_cost_usd": _f(u.get("estimated_cost_usd")),
    }


def collect_stage4(sc: dict) -> dict:
    """Vision lane — present ONLY for docs with non-empty image_assets (drives S4's vision-gated %, §7)."""
    s4 = sc.get("stage4")
    if not isinstance(s4, dict):
        return {"s4": False}
    c = s4.get("counts") or {}
    u = s4.get("usage") or {}
    # Iterate image_assets[] (vision docs only, few) for two things the rolled-up counts can't tell us:
    #   * safety: minor/explicit indicators
    #   * vision-embed GROUND TRUTH: an asset is actually embedded iff embedding_ref is non-null. The
    #     stage4.counts.embedded tally can read "complete" while embedding_ref is null (e.g. Stage 4 ran with
    #     no AI-Vision env), so the user-visible "did vision embeddings succeed?" must come from the ref.
    minor = explicit = has_visual = embedded_ref = 0
    for a in (sc.get("image_assets") or []):
        a = a or {}
        saf = a.get("safety") or {}
        if saf.get("minor_indicator"):
            minor += 1
        if saf.get("explicit"):
            explicit += 1
        if a.get("has_visual_content"):
            has_visual += 1
            if a.get("embedding_ref"):
                embedded_ref += 1
    return {
        "s4": True,
        "s4_assets": _i(c.get("assets")),
        "s4_captioned": _i(c.get("captioned")),
        "s4_pregated_blank": _i(c.get("pregated_blank")),
        "s4_has_visual_content_true": _i(c.get("has_visual_content_true")),
        "s4_content_filtered": _i(c.get("content_filtered")),
        "s4_safety_review": _i(c.get("safety_review")),
        "s4_content_safety_review": _i(c.get("content_safety_review")),
        "s4_embedded": _i(c.get("embedded")),
        "s4_embed_errors": _i(c.get("embed_errors")),
        "s4_errored": _i(c.get("errored")),
        "s4_assets_minor": minor,
        "s4_assets_explicit": explicit,
        # vision-embed coverage ground truth (per-asset embedding_ref):
        "s4_assets_has_visual": has_visual,
        "s4_assets_embedded_ref": embedded_ref,
        "s4_prompt_tokens": _i(u.get("prompt_tokens")),
        "s4_completion_tokens": _i(u.get("completion_tokens")),
        "s4_cached_tokens": _i(u.get("cached_tokens")),
        "s4_calls": _i(u.get("calls")),
        "s4_cost_usd": _f(u.get("estimated_cost_usd")),
    }


def collect_stage5(sc: dict) -> dict:
    s5 = sc.get("stage5")
    if not isinstance(s5, dict):
        return {"s5": False}
    u = s5.get("usage") or {}
    dsf = sc.get("document_summary_final") or {}
    saf = dsf.get("safety") or {}
    return {
        "s5": True,
        "s5_calls": _i(u.get("calls")),
        "s5_cost_usd": _f(u.get("estimated_cost_usd")),
        "s5_source": dsf.get("source"),                     # promoted|fused|image_only|no_content
        "s5_no_content": bool(dsf.get("no_content")),
        "s5_safety_flag": saf.get("flag"),
        "s5_content_filtered": bool(saf.get("content_filtered")),
    }


def collect_stage6(sc: dict) -> dict:
    s6 = sc.get("stage6")
    if not isinstance(s6, dict):
        return {"s6": False}
    c = s6.get("counts") or {}
    return {
        "s6": True,
        "s6_parents": _i(c.get("parents")),
        "s6_children": _i(c.get("children")),
        "s6_pages_chunked": _i(c.get("pages_chunked")),
        "s6_skipped_pages": _i(c.get("skipped_pages")),
        "s6_empty": s6.get("chunks_ref") is None,            # empty doc (no chunkable text)
    }


def collect_activity(sc: dict) -> dict:
    """Max ``generated_at`` seen across the base generator + any stage block — answers "is a run live?"
    (ISO-8601 sorts lexically, so max() is the latest)."""
    stamps = [_g(sc, "generator", "generated_at_utc")]
    for s in ("stage2", "stage3", "stage4", "stage5", "stage6"):
        stamps.append(_g(sc, s, "generated_at_utc"))
    stamps = [s for s in stamps if isinstance(s, str)]
    return {"last_activity": max(stamps) if stamps else None}


# The registry. Order is irrelevant (keys are disjoint); listed in pipeline order for readability. Append a
# collector here to grow the dashboard (Handoff §5).
COLLECTORS = (
    collect_base,
    collect_stage2,
    collect_stage3,
    collect_stage4,
    collect_stage5,
    collect_stage6,
    collect_activity,
)


def extract_digest(sc: dict) -> dict:
    """Run every collector over one parsed sidecar and merge into the compact per-doc digest."""
    digest: dict = {}
    for fn in COLLECTORS:
        digest.update(fn(sc))
    return digest
