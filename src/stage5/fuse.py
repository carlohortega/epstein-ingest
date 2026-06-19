"""Stage-5 domain logic, independent of transport so the realtime scheduler and the offline tests share
it: the per-document routing decision, the page-ordered text+caption interleave that feeds the reduce, the
(possibly hierarchical) fusion reduce, and the doc-level safety rollup.

Everything that calls the model takes a ``client`` with the same ``complete_json(messages, schema,
max_output_tokens) -> LLMResult`` contract as Stage 3, so a deterministic fake drives the offline tests
(Handoff §9 — outputs are LLM-generated; we inject a stub instead of calling Azure).

Reuses Stage 3's ``Usage`` accumulator and ``Stage3CallError`` unchanged (the nano plumbing is shared).
"""

from __future__ import annotations

from ..stage3.summarize import Usage
from .config import Stage5Config
from .prompts import FUSION_SCHEMA, build_fusion_messages

# Routing archetypes (Handoff §2). The string is recorded verbatim as document_summary_final.source.
PROMOTED = "promoted"       # text-only: copy provisional text -> final, NO call
FUSED = "fused"             # vision + text: one reduce over page summaries + captions/descriptions
IMAGE_ONLY = "image_only"   # vision, no text: one reduce over captions/descriptions only (first summary)
NO_CONTENT = "no_content"   # neither: deterministic sentinel, NO call


def kept_vision_assets(sidecar: dict) -> list[dict]:
    """The assets Stage 5 folds in: ONLY ``has_visual_content`` is True — pages AND artifacts (Handoff §2).
    Vision-ness is decided here, never from ``page_kind``."""
    return [a for a in (sidecar.get("image_assets") or []) if a.get("has_visual_content") is True]


def route_document(sidecar: dict) -> tuple[str, list[dict], bool]:
    """Compute (route, kept_vision, has_text) from the FROZEN Stage-3/Stage-4 sidecar (Handoff §2).

    ``has_text`` is ``document_summary is not None`` — Stage 3 wrote null iff the doc had no usable
    text/mixed page summaries. The gate is ``any(has_visual_content)`` over pages AND artifacts, so a doc
    Stage 2 classified ``text`` that carries an embedded-photo artifact still routes to FUSED, not PROMOTE.
    """
    kept = kept_vision_assets(sidecar)
    has_text = sidecar.get("document_summary") is not None
    if kept and has_text:
        route = FUSED
    elif kept and not has_text:
        route = IMAGE_ONLY
    elif not kept and has_text:
        route = PROMOTED
    else:
        route = NO_CONTENT
    return route, kept, has_text


def _page_summaries(sidecar: dict) -> dict:
    """page_number -> Stage-3 page summary text, for pages that actually have a non-empty summary."""
    out: dict = {}
    for p in (sidecar.get("pages") or []):
        s = p.get("summary")
        if isinstance(s, str) and s.strip():
            out[p.get("page_number")] = s.strip()
    return out


def _sort_key(page_number):
    """Order by page_number; assets/pages with a missing page_number sort last (deterministically)."""
    return (page_number is None, page_number if page_number is not None else 0)


def build_fusion_inputs(sidecar: dict) -> tuple[list[str], int, int]:
    """The reduce input = summaries + captions, IN PAGE ORDER (Handoff §3). For each page in order emit its
    Stage-3 text summary (if any), followed by the **caption + description** (both, decided §11) of every
    ``has_visual_content`` asset on that page (assets grouped by ``page_number``; artifacts carry one too).

    Returns (items, covered_text_pages, covered_vision_assets). For an image-only doc there are no page
    summaries (document_summary is null), so the items are captions/descriptions only — naturally."""
    summ_by_page = _page_summaries(sidecar)
    by_page: dict = {}
    for a in kept_vision_assets(sidecar):
        by_page.setdefault(a.get("page_number"), []).append(a)

    page_nums = sorted(set(summ_by_page) | set(by_page), key=_sort_key)
    items: list[str] = []
    covered_text = covered_vision = 0
    for pn in page_nums:
        label = f"page {pn}" if pn is not None else "page (unknown)"
        if pn in summ_by_page:
            items.append(f"[{label} — text summary]\n{summ_by_page[pn]}")
            covered_text += 1
        for a in by_page.get(pn, []):
            caption = str(a.get("caption") or "").strip()
            description = str(a.get("description") or "").strip()
            body = "\n".join(x for x in (caption, description) if x)
            items.append(f"[{label} — image]\n{body or '(image with no caption available)'}")
            covered_vision += 1
    return items, covered_text, covered_vision


# --- the fusion reduce (mirrors src/stage3/summarize.summarize_document) ----------------------------
def _reduce_once(client, items: list[str], cfg: Stage5Config, usage: Usage) -> str:
    r = client.complete_json(build_fusion_messages(items), FUSION_SCHEMA, cfg.max_doc_output_tokens)
    usage.add(r)
    return str(r.data.get("summary") or "").strip()


def summarize_fusion(client, items: list[str], cfg: Stage5Config) -> tuple[str, Usage, int]:
    """Final document summary from the page-ordered interleaved items. Single reduce when the concatenation
    fits; otherwise the same level-by-level hierarchical fan-in (batches up to a depth cap) Stage 3 uses, so
    an oversized document folds identically. Returns (summary_text, usage, reduce_depth)."""
    usage = Usage()
    level = list(items)
    depth = 0
    while depth < cfg.doc_summary_max_depth - 1 and \
            sum(len(s) + 1 for s in level) > cfg.max_doc_summary_input_chars and len(level) > 1:
        depth += 1
        nxt: list[str] = []
        for i in range(0, len(level), cfg.doc_summary_batch_size):
            nxt.append(_reduce_once(client, level[i:i + cfg.doc_summary_batch_size], cfg, usage))
        level = nxt
    summary = _reduce_once(client, level, cfg, usage)
    return summary, usage, depth + 1   # +1 for the final reduce


# --- doc-level safety rollup (Handoff §6) ----------------------------------------------------------
def _page_content_filtered(page: dict) -> bool:
    ts = page.get("text_safety") or {}
    return bool(ts.get("content_filtered")) or "content_filtered" in (page.get("flags") or [])


def _asset_content_filtered(asset: dict) -> bool:
    if asset.get("content_filtered"):
        return True
    return bool((asset.get("safety") or {}).get("content_filtered"))


def safety_rollup(sidecar: dict) -> dict:
    """Combine into the doc-level ``document_summary_final.safety`` (Handoff §6):
      - any Stage-3 page ``text_safety.flag == "review"`` (the existing page triage),
      - any Stage-4 asset ``safety.flag == "review"`` (incl. minor_indicator/explicit),
      - any ``content_filtered`` (a text page OR an image asset).
    If any fire -> doc ``flag:"review"``; ``content_filtered`` is propagated so Stage 7 routes the
    quarantine (CSAM/minors process) and an empty doc is never mistaken for innocuous-empty (§4)."""
    review = False
    content_filtered = False
    for p in (sidecar.get("pages") or []):
        if (p.get("text_safety") or {}).get("flag") == "review":
            review = True
        if _page_content_filtered(p):
            content_filtered = True
    for a in (sidecar.get("image_assets") or []):
        if (a.get("safety") or {}).get("flag") == "review":
            review = True
        if _asset_content_filtered(a):
            content_filtered = True
    flag = "review" if (review or content_filtered) else "ok"
    return {"flag": flag, "content_filtered": content_filtered}
