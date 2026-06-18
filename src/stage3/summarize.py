"""Stage-3 domain logic, independent of transport (realtime or batch) so both paths and the tests share
it: page routing, truncation, response normalization, per-page summarization, the (possibly hierarchical)
document-summary reduce, the safety rollup, and a token/cost accumulator.

Everything that calls the model takes a ``client`` with a ``complete_json(messages, schema,
max_output_tokens) -> LLMResult`` method, so a fake client drives the offline tests (Handoff §9 — outputs
are LLM-generated, byte-determinism is not required; we inject a deterministic stub instead).
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Stage3Config
from .client import LLMResult, estimate_cost_usd
from .prompts import (SAFETY_CATEGORIES, SAFETY_FLAGS, PAGE_SCHEMA, DOC_SCHEMA,
                      build_page_messages, build_doc_messages)

ELIGIBLE_KINDS = {"text", "mixed"}              # Handoff §4 routing table
_ENTITY_TYPES = {"person", "org", "place", "date", "other"}


# --- token / cost accounting (Handoff §8) ----------------------------------------------------------
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0

    def add(self, r: LLMResult) -> "Usage":
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.cached_tokens += r.cached_tokens
        self.calls += 1
        return self

    def merge(self, other: "Usage") -> "Usage":
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cached_tokens += other.cached_tokens
        self.calls += other.calls
        return self

    def as_dict(self, cfg: Stage3Config, *, batch: bool) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "cached_tokens": self.cached_tokens,
            "calls": self.calls,
            "estimated_cost_usd": round(estimate_cost_usd(
                self.prompt_tokens, self.completion_tokens, self.cached_tokens, cfg, batch=batch), 6),
            "batch": batch,
        }


# --- routing / truncation (Handoff §3/§4) ----------------------------------------------------------
def route_page(page: dict, cfg: Stage3Config) -> tuple[str, str | None]:
    """('summarize', None) or ('skip', reason). No LLM needed — decided from Stage-2 page_kind/flags."""
    kind = page.get("page_kind")
    if kind not in ELIGIBLE_KINDS:
        return ("skip", f"page_kind:{kind}")
    flags = page.get("flags") or []
    if cfg.skip_low_quality_text and "low_quality_text" in flags:
        return ("skip", "low_quality_text")
    if not ((page.get("text") or {}).get("content") or "").strip():
        return ("skip", "empty_text")
    return ("summarize", None)


def truncate(text: str, cfg: Stage3Config) -> tuple[str, bool]:
    if len(text) <= cfg.max_input_chars:
        return text, False
    return text[:cfg.max_input_chars], True


def is_low_confidence(page: dict, truncated: bool) -> bool:
    """A summary is low_confidence when its source was Stage-2 low_quality_text (and we summarized it
    anyway) or when the page text was truncated before the call (Handoff §4/§7)."""
    return truncated or ("low_quality_text" in (page.get("flags") or []))


# --- response normalization (defensive even though the schema is strict) ----------------------------
def normalize_page_output(data: dict) -> tuple[str, dict, dict]:
    """Clamp/validate the model object to the documented bounds; returns (summary, summary_json_core,
    text_safety). summary_json_core has key_points/entities/topics only (provenance added by the caller)."""
    summary = str(data.get("summary") or "").strip()
    key_points = [str(k).strip() for k in (data.get("key_points") or []) if str(k).strip()][:8]
    topics = [str(t).strip() for t in (data.get("topics") or []) if str(t).strip()][:6]
    entities = []
    for e in (data.get("entities") or []):
        name = str((e or {}).get("name") or "").strip()
        if not name:
            continue
        etype = (e or {}).get("type")
        entities.append({"name": name, "type": etype if etype in _ENTITY_TYPES else "other"})

    ts = data.get("text_safety") or {}
    flag = ts.get("flag") if ts.get("flag") in SAFETY_FLAGS else "ok"
    cats = [c for c in (ts.get("categories") or []) if c in SAFETY_CATEGORIES] if flag == "review" else []
    text_safety = {"flag": flag, "categories": cats}

    core = {"key_points": key_points, "entities": entities, "topics": topics}
    return summary, core, text_safety


def make_summary_json(core: dict, *, low_confidence: bool, truncated: bool,
                      prompt_tokens: int, completion_tokens: int) -> dict:
    """Assemble the per-page summary_json with the REQUIRED token counts + provenance (Handoff §7/§8)."""
    return {
        **core,
        "low_confidence": low_confidence,
        "truncated": truncated,
        "input_token_count": prompt_tokens,      # = usage.prompt_tokens of THIS page's call
        "output_token_count": completion_tokens,  # = usage.completion_tokens of THIS page's call
    }


# --- the calls (Handoff §3/§5) ---------------------------------------------------------------------
def summarize_page(client, page_text: str, cfg: Stage3Config) -> LLMResult:
    text, _ = truncate(page_text, cfg)
    return client.complete_json(build_page_messages(text), PAGE_SCHEMA, cfg.max_output_tokens)


def _reduce_once(client, summaries: list[str], cfg: Stage3Config, usage: Usage) -> str:
    r = client.complete_json(build_doc_messages(summaries), DOC_SCHEMA, cfg.max_doc_output_tokens)
    usage.add(r)
    return str(r.data.get("summary") or "").strip()


def summarize_document(client, page_summaries: list[str], cfg: Stage3Config) -> tuple[str, Usage, int]:
    """Provisional document summary from the ordered page summaries (Handoff §5). Single reduce when the
    concatenation fits; otherwise hierarchical fan-in in batches up to a depth cap. Returns
    (summary_text, usage, reduce_depth)."""
    usage = Usage()
    level = list(page_summaries)
    depth = 0
    # Reduce level-by-level until the concatenation fits one call or we hit the depth cap.
    while depth < cfg.doc_summary_max_depth - 1 and \
            sum(len(s) + 1 for s in level) > cfg.max_doc_summary_input_chars and len(level) > 1:
        depth += 1
        nxt: list[str] = []
        for i in range(0, len(level), cfg.doc_summary_batch_size):
            nxt.append(_reduce_once(client, level[i:i + cfg.doc_summary_batch_size], cfg, usage))
        level = nxt
    summary = _reduce_once(client, level, cfg, usage)
    return summary, usage, depth + 1   # +1 for the final reduce


# --- rollups (Handoff §6) --------------------------------------------------------------------------
def safety_rollup(pages: list[dict]) -> dict:
    """Doc-level triage rollup so an operator/Stage 4 runs Content Safety text:analyze on review pages
    only. No 'block' (we LABEL, not block); page numbers are stringified to match the Stage-7 mapping."""
    flagged = [str(p.get("page_number")) for p in pages
               if (p.get("text_safety") or {}).get("flag") == "review"]
    return {"review": len(flagged), "pages_flagged": flagged}
