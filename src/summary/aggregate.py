"""Fold per-doc digests into per-dataset + corpus-wide rollups — the §3 metric catalog.

The subtle part is **stage applicability** (Handoff §7): ``pct_complete`` must use the right denominator per
stage, or it lies. Above all S4 is **vision-gated** — it applies ONLY to docs with non-empty
``image_assets``; text-only docs are ``n/a`` (they never get a ``stage4`` block by design), so computing S4%
over all docs would under-report badly. ``_applicable``/``_done`` below encode the §7 rules exactly.

Each :meth:`Aggregator.add` folds a digest into both its dataset accumulator AND a shared corpus accumulator,
so the corpus rollup is summed at the source (not by re-summing finalized dicts). ``finalize`` runs the same
code over every accumulator, so per-dataset and corpus reports are guaranteed consistent."""

from __future__ import annotations

import os
from collections import Counter

from . import STAGES, STAGE_LABELS
from .util import derive_identifiers

# Cost is the sum of the block-stamped estimated_cost_usd (authoritative — each block already encodes the
# rates that run used). text-embedding-3-small price for the Stage-7 projection in --chunks mode.
_EMBED_USD_PER_1M = 0.02


def _state(applicable: int, done: int) -> str:
    if applicable == 0:
        return "n/a"
    if done == 0:
        return "not_started"
    return "done" if done >= applicable else "in_progress"


def _quarantine_reasons(d: dict) -> list[str]:
    """The safety reasons (if any) a doc lands on the quarantine list for (Handoff §3 safety)."""
    r = []
    if d.get("s3_content_filtered"):
        r.append("text_content_filtered")
    if d.get("s3_safety_review"):
        r.append("text_safety_review")
    if d.get("s4_content_filtered"):
        r.append("vision_content_filtered")
    if d.get("s4_safety_review") or d.get("s4_content_safety_review"):
        r.append("vision_safety_review")
    if d.get("s4_assets_minor"):
        r.append("image_minor_indicator")
    if d.get("s4_assets_explicit"):
        r.append("image_explicit")
    if d.get("s5_content_filtered"):
        r.append("final_content_filtered")
    return r


class _Acc:
    """Running totals for one dataset (or the corpus). All fields are sums/counters so a fold is O(1)."""

    def __init__(self) -> None:
        self.docs = 0
        self.status = Counter()                 # ok|error|missing|parse_error|unknown
        self.error_codes = Counter()
        self.missing_paths: list[str] = []      # PDFs with no sidecar (sampled, capped for the report)
        # progress grid
        self.applicable = Counter()             # stage -> applicable docs
        self.done = Counter()                   # stage -> done docs
        # volume / content
        self.vol = Counter()
        # cost / usage (Σ block-stamped estimated_cost_usd, authoritative)
        self.cost_usd = {"stage3": 0.0, "stage4": 0.0, "stage5": 0.0}
        self.tokens = {s: Counter() for s in ("stage3", "stage4", "stage5")}
        self.calls = Counter()
        # safety
        self.safety = Counter()
        self.quarantine: list[dict] = []
        # routing / mix
        self.routing = Counter()
        self.s5_source = Counter()
        self.source_kind = Counter()            # pdf today; media track later (§7 source-kind aware)
        # chunks
        self.chunks = Counter()
        # vision-embed coverage (ground truth = per-asset embedding_ref) + gap docs
        self.vision = Counter()
        self.vision_gap: list[dict] = []        # docs where Stage-4 vision embeddings are missing/failed
        # embed (S7) per-doc child coverage against the staged vector store
        self.embed = Counter()
        # activity
        self.last_activity: str | None = None

    def add(self, d: dict) -> None:
        self.docs += 1
        status = d.get("status", "unknown")
        self.status[status] += 1
        if status == "error":
            self.error_codes[d.get("error_code") or "unknown"] += 1
        if status == "missing" and len(self.missing_paths) < 1000:
            self.missing_paths.append(d.get("rel") or "")

        ok = status == "ok"
        self.source_kind[d.get("source_kind") or "pdf"] += 1
        # ---- progress grid (applicability per §7) -------------------------------------------------
        self.applicable["stage1"] += 1
        if status in ("ok", "error"):
            self.done["stage1"] += 1
        if ok:
            # applicability per §7: S2/S3/S5 -> every ok doc; S4 -> only vision docs; S6 -> docs with pages.
            applies = {"stage2": True, "stage3": True, "stage5": True,
                       "stage4": bool(d.get("has_image_assets")),
                       "stage6": d.get("page_count", 0) > 0}
            for st, ap in applies.items():
                if ap:
                    self.applicable[st] += 1
                    if d.get("s" + st[-1]):     # digest flags d["s2"]..d["s6"] = block present
                        self.done[st] += 1
            # S7 embed applies to docs with >=1 child chunk (empty docs are n/a — nothing to embed). "done"
            # is store-aware: fold() injects s7_done/s7_child_* from the staged vector store when available.
            has_children = bool(d.get("s6")) and not d.get("s6_empty") and d.get("s6_children", 0) > 0
            if has_children:
                self.applicable["stage7"] += 1
                self.embed["child_total"] += d.get("s7_child_total", 0)
                self.embed["child_embedded"] += d.get("s7_child_embedded", 0)
                if d.get("s7_done"):
                    self.done["stage7"] += 1
            # S8 ingest applies to every ok doc; "done" needs a live-DB cross-check (offline => unknown, kept
            # n/a in _finalize). fold() may inject s8_done from a DB check / ingest-receipt later.
            self.applicable["stage8"] += 1
            if d.get("s8_done"):
                self.done["stage8"] += 1

        # ---- volume / content ---------------------------------------------------------------------
        if ok:
            for k in ("page_count", "bytes", "text_chars", "visible_chars", "invisible_chars",
                      "scrubbed_chars", "pages_text_sufficient", "pages_need_vision",
                      "pages_redaction_flagged", "pages_blank"):
                self.vol[k] += d.get(k, 0)
            if d.get("routing"):
                self.routing[d["routing"]] += 1

        # ---- cost / usage (Σ block-stamped estimated_cost_usd — authoritative) ---------------------
        for st in ("stage3", "stage4", "stage5"):
            self.cost_usd[st] += d.get("s" + st[-1] + "_cost_usd", 0.0)
            self.calls[st] += d.get("s" + st[-1] + "_calls", 0)
        for tk in ("prompt_tokens", "completion_tokens"):
            self.tokens["stage3"][tk] += d.get("s3_" + tk, 0)
        for tk in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            self.tokens["stage4"][tk] += d.get("s4_" + tk, 0)

        # ---- safety -------------------------------------------------------------------------------
        self.safety["text_safety_review_pages"] += d.get("s3_safety_review", 0)
        self.safety["vision_safety_review"] += d.get("s4_safety_review", 0)
        self.safety["content_safety_review"] += d.get("s4_content_safety_review", 0)
        self.safety["assets_minor"] += d.get("s4_assets_minor", 0)
        self.safety["assets_explicit"] += d.get("s4_assets_explicit", 0)
        if d.get("pages_redaction_flagged", 0) > 0:
            self.safety["docs_with_redaction"] += 1
        reasons = _quarantine_reasons(d)
        if reasons:
            self.safety["docs_quarantined"] += 1
            self.quarantine.append({"path": d.get("rel") or "", "reasons": reasons})

        # ---- routing mix (Stage-5 source) + chunks ------------------------------------------------
        if d.get("s5_source"):
            self.s5_source[d["s5_source"]] += 1
        if d.get("s4"):
            for k in ("s4_assets", "s4_captioned", "s4_pregated_blank", "s4_has_visual_content_true",
                      "s4_embedded"):
                self.safety[k] += d.get(k, 0)     # reuse counter store; surfaced under "vision" in finalize
            # ---- vision-embed coverage (the user-visible "did vision embeddings succeed?") -----------
            # Ground truth = per-asset embedding_ref (null => not embedded), NOT the stage4.counts.embedded
            # tally (which can read complete while refs are null — e.g. Stage 4 ran with no AI-Vision env).
            hv = d.get("s4_assets_has_visual", 0)
            er = d.get("s4_assets_embedded_ref", 0)
            errs = d.get("s4_embed_errors", 0)
            self.vision["has_visual"] += hv
            self.vision["embedded_ref"] += er
            self.vision["embed_errors"] += errs
            not_embedded = max(0, hv - er)
            if not_embedded > 0 or errs > 0:
                self.vision["docs_with_gap"] += 1
                self.vision["assets_not_embedded"] += not_embedded
                if len(self.vision_gap) < 1000:
                    self.vision_gap.append({"path": d.get("rel") or "", "has_visual": hv,
                                            "embedded": er, "not_embedded": not_embedded,
                                            "embed_errors": errs})
        if d.get("s6"):
            for k in ("s6_parents", "s6_children", "s6_pages_chunked", "s6_skipped_pages"):
                self.chunks[k] += d.get(k, 0)
            if d.get("s6_empty"):
                self.chunks["empty_docs"] += 1
        if "chunk_children" in d:
            self.chunks["chunk_children"] += d.get("chunk_children", 0)
            self.chunks["chunk_unique_sha"] += d.get("chunk_unique_sha", 0)

        # ---- activity -----------------------------------------------------------------------------
        la = d.get("last_activity")
        if isinstance(la, str) and (self.last_activity is None or la > self.last_activity):
            self.last_activity = la


class Aggregator:
    """Routes each digest to its dataset accumulator + a shared corpus accumulator."""

    def __init__(self) -> None:
        self._datasets: dict[str, _Acc] = {}
        self._names: dict[str, str] = {}
        self.corpus = _Acc()

    def add(self, dataset_root: str, digest: dict) -> None:
        acc = self._datasets.get(dataset_root)
        if acc is None:
            acc = self._datasets[dataset_root] = _Acc()
            ds, _case = derive_identifiers(dataset_root)
            self._names[dataset_root] = ds or os.path.basename(dataset_root)
        acc.add(digest)
        self.corpus.add(digest)

    def safety_list(self) -> dict:
        """The complete quarantine list + vision-embedding gap list per dataset for --safety-list (§4)."""
        return {
            "quarantine": {self._names[root]: sorted(self._datasets[root].quarantine, key=lambda q: q["path"])
                           for root in sorted(self._datasets)},
            "vision_embedding_gaps": {self._names[root]: sorted(self._datasets[root].vision_gap,
                                                                key=lambda q: q["path"])
                                      for root in sorted(self._datasets)},
        }

    def finalize(self, scans: dict[str, dict], embed_info: dict | None = None) -> dict:
        """Build the corpus report: a per-dataset summary list + the corpus rollup. ``scans`` maps a dataset
        root to its ``scan`` meta dict; ``embed_info`` describes the shared S7 vector store (or None)."""
        datasets = []
        for root in sorted(self._datasets):
            datasets.append(_finalize(self._names[root], root, self._datasets[root],
                                      scans.get(root), embed_info))
        corpus = _finalize("CORPUS", None, self.corpus, _merge_scans(scans.values()), embed_info)
        # Projected-remaining must SUM the per-dataset estimates, not re-average corpus-wide: per-doc cost
        # rates differ sharply between datasets (a cheap dataset's rate would otherwise mask an expensive
        # dataset's large backlog), so a blended corpus average badly under-estimates "cost to finish".
        corpus["cost"]["projected_remaining_usd"] = round(
            sum(d["cost"]["projected_remaining_usd"] for d in datasets), 4)
        return {"datasets": datasets, "corpus": corpus}


def _finalize(name: str, root: str | None, a: _Acc, scan: dict | None,
              embed_info: dict | None = None) -> dict:
    stages = {}
    for st in STAGES:
        ap, dn = a.applicable[st], a.done[st]
        stages[st] = {
            "label": STAGE_LABELS[st],
            "applicable": ap,
            "done": dn,
            "pct_complete": round(100.0 * dn / ap, 1) if ap else None,
            "state": _state(ap, dn),
        }
    # S7 (embed) is NOT a sidecar block — its done-signal is the staged vector store. Be honest about what we
    # could actually determine: no store => nothing embedded (not_started); store present but scanned without
    # --chunks => per-doc coverage is unknown (n/a + note); store-aware => the computed coverage stands.
    store_present = bool(embed_info and embed_info.get("store_present"))
    store_aware = bool(embed_info and embed_info.get("store_aware"))
    s7 = stages["stage7"]
    if not store_present:
        s7["state"] = "not_started" if s7["applicable"] else "n/a"
    elif not store_aware:
        s7["state"], s7["pct_complete"] = "n/a", None
        s7["note"] = "vector store present — re-run with --chunks for per-doc coverage"
    # S8 (ingest) writes the live DB; offline we can't see those rows -> n/a unless a check injected done>0.
    s8 = stages["stage8"]
    if a.done["stage8"] == 0:
        s8["state"], s8["pct_complete"] = "n/a", None
        s8["note"] = "requires DB cross-check (online)"

    # projected remaining $ = per-stage avg cost over DONE docs × not-yet-done applicable docs (an estimate).
    projected = 0.0
    for st in ("stage3", "stage4", "stage5"):
        done = a.done[st]
        remaining = max(0, a.applicable[st] - done)
        if done and remaining:
            projected += (a.cost_usd[st] / done) * remaining
    total_cost = sum(a.cost_usd.values())

    chunks = dict(a.chunks)
    if chunks.get("chunk_children"):
        uniq, tot = chunks["chunk_unique_sha"], chunks["chunk_children"]
        chunks["dedup_ratio"] = round(1.0 - uniq / tot, 4) if tot else 0.0

    out = {
        "dataset": name,
        "totals": {
            "docs": a.docs,
            "docs_ok": a.status["ok"],
            "docs_error": a.status["error"],
            "docs_missing_sidecar": a.status["missing"],
            "docs_parse_error": a.status["parse_error"],
            "pages": a.vol["page_count"],
            "bytes": a.vol["bytes"],
            "text_chars": a.vol["text_chars"],
            "visible_chars": a.vol["visible_chars"],
            "invisible_chars": a.vol["invisible_chars"],
            "scrubbed_chars": a.vol["scrubbed_chars"],
            "pages_text_sufficient": a.vol["pages_text_sufficient"],
            "pages_need_vision": a.vol["pages_need_vision"],
            "pages_blank": a.vol["pages_blank"],
        },
        "stages": stages,
        "cost": {
            "stage3_usd": round(a.cost_usd["stage3"], 4),
            "stage4_usd": round(a.cost_usd["stage4"], 4),
            "stage5_usd": round(a.cost_usd["stage5"], 4),
            "stage6_usd": 0.0,
            "total_usd": round(total_cost, 4),
            "projected_remaining_usd": round(projected, 4),
            "calls": {s: a.calls[s] for s in ("stage3", "stage4", "stage5")},
            "tokens": {s: dict(a.tokens[s]) for s in ("stage3", "stage4", "stage5")},
        },
        "safety": {
            "docs_quarantined": a.safety["docs_quarantined"],
            "docs_with_redaction": a.safety["docs_with_redaction"],
            "pages_redaction_flagged": a.vol["pages_redaction_flagged"],
            "text_safety_review_pages": a.safety["text_safety_review_pages"],
            "vision_safety_review": a.safety["vision_safety_review"],
            "content_safety_review": a.safety["content_safety_review"],
            "image_assets_minor": a.safety["assets_minor"],
            "image_assets_explicit": a.safety["assets_explicit"],
        },
        "source_kinds": dict(a.source_kind),
        "routing": {
            "stage1_recommendation": dict(a.routing),
            "stage5_source": dict(a.s5_source),
            "vision": {
                "assets": a.safety["s4_assets"],
                "captioned": a.safety["s4_captioned"],
                "pregated_blank": a.safety["s4_pregated_blank"],
                "has_visual_content_true": a.safety["s4_has_visual_content_true"],
                "embedded": a.safety["s4_embedded"],
            },
        },
        # Vision-embedding coverage — surfaces a dataset whose Stage-4 image vectors are missing/failed (e.g.
        # ran with no AI-Vision env). Ground truth = per-asset embedding_ref. A gap => re-run Stage 4 (§3).
        "vision_embedding": {
            "has_visual_assets": a.vision["has_visual"],
            "embedded_assets": a.vision["embedded_ref"],
            "not_embedded_assets": a.vision["assets_not_embedded"],
            "embed_errors": a.vision["embed_errors"],
            "coverage_pct": (round(100.0 * a.vision["embedded_ref"] / a.vision["has_visual"], 1)
                             if a.vision["has_visual"] else None),
            "docs_with_gap": a.vision["docs_with_gap"],
        },
        # S7 embed: the shared staged vector store (corpus-global), + this dataset's per-doc child coverage
        # (only meaningful when scanned store-aware, i.e. --chunks with a store present).
        "embed": {
            "store_present": store_present,
            "store_aware": store_aware,
            "embedding_model": (embed_info or {}).get("embedding_model"),
            "dimensions": (embed_info or {}).get("dimensions"),
            "store_root": (embed_info or {}).get("store_root"),
            "embedded_unique_content_sha": (embed_info or {}).get("embedded_unique", 0),
            "child_total": a.embed["child_total"],
            "child_embedded": a.embed["child_embedded"],
            # Stage-7 cost projection (heavy/--chunks mode): unique child shas × ~125 tok × text-embed-3-small
            # rate. The dedup win (chunk_unique_sha << chunk_children) is the lever (Handoff §3/§8).
            "projected_embed_usd": (round(a.chunks["chunk_unique_sha"] * 125 * _EMBED_USD_PER_1M / 1e6, 4)
                                    if a.chunks.get("chunk_unique_sha") else None),
        },
        "chunks": chunks,
        "health": {
            "errors_by_code": dict(a.error_codes),
            "missing_sidecars": a.status["missing"],
            "parse_errors": a.status["parse_error"],
        },
        "last_activity_utc": a.last_activity,
    }
    if root is not None:
        out["root"] = root
    if scan is not None:
        out["scan"] = scan
    return out


def _merge_scans(scans) -> dict | None:
    """Combine per-dataset scan meta into a corpus-level one (sums counts, max wall-clock, worst mode)."""
    scans = [s for s in scans if s]
    if not scans:
        return None
    out = {
        "mode": "sample" if any(s["mode"] == "sample" for s in scans) else "full",
        "docs_total": sum(s["docs_total"] for s in scans),
        "docs_scanned": sum(s["docs_scanned"] for s in scans),
        "docs_sampled_out": sum(s.get("docs_sampled_out", 0) for s in scans),
        "cache_hits": sum(s["cache_hits"] for s in scans),
        "cache_misses": sum(s["cache_misses"] for s in scans),
        "extrapolated": any(s.get("extrapolated") for s in scans),
        "wall_clock_s": round(sum(s["wall_clock_s"] for s in scans), 3),
    }
    out["sampled_pct"] = round(100.0 * out["docs_scanned"] / out["docs_total"], 2) if out["docs_total"] else 100.0
    return out
