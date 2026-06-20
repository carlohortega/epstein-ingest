"""Report emitters: canonical JSON, a Markdown dashboard, and a self-contained HTML dashboard.

The JSON is the source of truth (Handoff §4); Markdown is the terminal/PR view; the HTML is the "fantastic
dashboard" — one file, inline CSS, no external assets or JS, opens straight in a browser. All three are
deterministic functions of the finalized report dict (stable key order, sorted lists) so reports diff
cleanly over time (acceptance §8)."""

from __future__ import annotations

import html

from . import SUMMARY_TOOL_NAME, SUMMARY_TOOL_VERSION, SCHEMA_VERSION, STAGES, STAGE_LABELS

_STATE_GLYPH = {"done": "✓", "in_progress": "~", "not_started": "·", "n/a": "—"}
_STATE_COLOR = {"done": "#2ea043", "in_progress": "#d29922", "not_started": "#6e7681", "n/a": "#30363d"}


# --------------------------------------------------------------------------- formatting helpers
def _n(x) -> str:
    return f"{int(x):,}" if isinstance(x, (int, float)) else "—"


def _usd(x) -> str:
    return f"${x:,.2f}" if isinstance(x, (int, float)) else "—"


def _usd4(x) -> str:
    return f"${x:,.4f}" if isinstance(x, (int, float)) else "—"


def _bytes(x) -> str:
    if not isinstance(x, (int, float)) or x <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024 or unit == "TB":
            return f"{x:.0f} {unit}" if unit == "B" else f"{x:,.1f} {unit}"
        x /= 1024.0
    return f"{x:.1f} PB"


def _pct(x) -> str:
    return f"{x:.1f}%" if isinstance(x, (int, float)) else "n/a"


def _warnings(report: dict) -> list[str]:
    """Cross-dataset callouts that deserve top-of-report prominence — chiefly vision-embedding gaps (a
    dataset whose Stage-4 image vectors are missing/failed; remediation = re-run Stage 4 with AI-Vision env)."""
    out = []
    for d in report["datasets"]:
        ve = d.get("vision_embedding") or {}
        if ve.get("docs_with_gap"):
            cov = _pct(ve.get("coverage_pct"))
            extra = f", {_n(ve['embed_errors'])} embed errors" if ve.get("embed_errors") else ""
            out.append(f"**{d['dataset']}**: vision embeddings incomplete — "
                       f"{_n(ve['embedded_assets'])}/{_n(ve['has_visual_assets'])} assets embedded ({cov}), "
                       f"{_n(ve['docs_with_gap'])} docs with gaps{extra}. Re-run Stage 4 with AI-Vision env.")
    return out


def build_report(result: dict, scan_config: dict, generated_at: str) -> dict:
    """Wrap finalize()'s {datasets, corpus} with the report header (schema_version, tool, config)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": SUMMARY_TOOL_NAME,
        "tool_version": SUMMARY_TOOL_VERSION,
        "generated_at_utc": generated_at,
        "scan_config": scan_config,
        "corpus": result["corpus"],
        "datasets": result["datasets"],
    }


# --------------------------------------------------------------------------- Markdown
def to_markdown(report: dict) -> str:
    corpus = report["corpus"]
    out: list[str] = []
    out.append(f"# Dataset Summary — {SUMMARY_TOOL_NAME} {report['tool_version']}")
    out.append("")
    out.append(f"_generated {report['generated_at_utc']} · schema {report['schema_version']}_")
    sc = corpus.get("scan") or {}
    if sc:
        mode = sc.get("mode", "full")
        extra = f" · **SAMPLE {sc.get('sampled_pct')}% — totals extrapolated**" if sc.get("extrapolated") else ""
        out.append(f"_scan: {mode}, {_n(sc.get('docs_scanned'))}/{_n(sc.get('docs_total'))} docs, "
                   f"{_n(sc.get('cache_hits'))} cache hits, {sc.get('wall_clock_s')}s{extra}_")
    out.append("")

    # ---- prominent warnings (vision-embedding gaps the most-asked) ----
    for w in _warnings(report):
        out.append(f"> ⚠ {w}")
    if _warnings(report):
        out.append("")

    # ---- corpus headline tiles ----
    t, c, s = corpus["totals"], corpus["cost"], corpus["safety"]
    ve, em = corpus.get("vision_embedding") or {}, corpus.get("embed") or {}
    out.append("## Corpus")
    out.append("")
    out.append(f"- **{_n(t['docs'])}** docs · **{_n(t['pages'])}** pages · **{_bytes(t['bytes'])}** "
               f"· **{_n(t['text_chars'])}** text chars")
    out.append(f"- **spent {_usd(c['total_usd'])}** "
               f"(S3 {_usd(c['stage3_usd'])} · S4 {_usd(c['stage4_usd'])} · S5 {_usd(c['stage5_usd'])}) "
               f"· projected remaining ~{_usd(c['projected_remaining_usd'])} _(est)_")
    out.append(f"- safety: **{_n(s['docs_quarantined'])}** quarantined · "
               f"{_n(s['docs_with_redaction'])} with redaction · "
               f"{_n(s['image_assets_minor'])} minor / {_n(s['image_assets_explicit'])} explicit assets")
    if ve.get("has_visual_assets"):
        gap = f" — ⚠ **{_n(ve['docs_with_gap'])} docs missing vision embeddings**" if ve.get("docs_with_gap") else ""
        out.append(f"- vision embeddings (S4 image vectors): **{_n(ve['embedded_assets'])}/"
                   f"{_n(ve['has_visual_assets'])}** assets ({_pct(ve.get('coverage_pct'))}){gap}")
    if em.get("store_present"):
        s7line = (f"**{_n(em['embedded_unique_content_sha'])}** content_shas embedded "
                  f"({em.get('embedding_model')})")
    else:
        s7line = "_not started — no vector store yet_"
        cch = corpus.get("chunks") or {}
        if cch.get("chunk_unique_sha"):
            proj = em.get("projected_embed_usd")
            s7line += (f" · **{_n(cch['chunk_unique_sha'])}** unique chunks ready"
                       + (f" (~{_usd(proj)} to embed)" if proj is not None else ""))
    out.append(f"- embed store (S7): {s7line}")
    out.append("")

    # ---- status grid: datasets × stages ----
    out.append("## Progress (done / applicable)")
    out.append("")
    head = ["Dataset", "Docs"] + [STAGE_LABELS[s] for s in STAGES] + ["Spent", "Last activity"]
    out.append("| " + " | ".join(head) + " |")
    out.append("|" + "|".join(["---"] * len(head)) + "|")
    rows = report["datasets"] + ([corpus] if len(report["datasets"]) != 1 else [])
    for d in rows:
        out.append("| " + " | ".join(_grid_cells(d)) + " |")
    out.append("")

    # ---- per-dataset detail cards ----
    for d in report["datasets"]:
        out.extend(_md_card(d))
    return "\n".join(out) + "\n"


def _grid_cells(d: dict) -> list[str]:
    cells = [f"**{d['dataset']}**", _n(d["totals"]["docs"])]
    for st in STAGES:
        sg = d["stages"][st]
        g = _STATE_GLYPH.get(sg["state"], "?")
        if sg["state"] == "n/a":
            cells.append("—")
        else:
            cells.append(f"{g} {_n(sg['done'])}/{_n(sg['applicable'])} ({_pct(sg['pct_complete'])})")
    cells.append(_usd(d["cost"]["total_usd"]))
    cells.append((d.get("last_activity_utc") or "—"))
    return cells


def _md_card(d: dict) -> list[str]:
    t, c, s, r = d["totals"], d["cost"], d["safety"], d["routing"]
    out = [f"### {d['dataset']}", ""]
    out.append(f"`{d.get('root', '')}`")
    out.append("")
    out.append(f"- **volume:** {_n(t['docs'])} docs · {_n(t['pages'])} pages · {_bytes(t['bytes'])} · "
               f"{_n(t['pages_text_sufficient'])} text-sufficient / {_n(t['pages_need_vision'])} need-vision pages")
    out.append(f"- **cost:** {_usd(c['total_usd'])} spent · ~{_usd(c['projected_remaining_usd'])} remaining _(est)_ · "
               f"calls S3 {_n(c['calls']['stage3'])} / S4 {_n(c['calls']['stage4'])}")
    out.append(f"- **safety:** {_n(s['docs_quarantined'])} quarantined · {_n(s['docs_with_redaction'])} redacted · "
               f"{_n(s['pages_redaction_flagged'])} redaction-flagged pages")
    if r["stage1_recommendation"]:
        mix = " · ".join(f"{k} {_n(v)}" for k, v in sorted(r["stage1_recommendation"].items()))
        out.append(f"- **routing (S1):** {mix}")
    if r["stage5_source"]:
        mix = " · ".join(f"{k} {_n(v)}" for k, v in sorted(r["stage5_source"].items()))
        out.append(f"- **final source (S5):** {mix}")
    ve = d.get("vision_embedding") or {}
    if ve.get("has_visual_assets"):
        gap = f" · ⚠ **{_n(ve['docs_with_gap'])} docs missing embeddings**" if ve.get("docs_with_gap") else " ✓"
        out.append(f"- **vision embeddings:** {_n(ve['embedded_assets'])}/{_n(ve['has_visual_assets'])} "
                   f"assets ({_pct(ve.get('coverage_pct'))}){gap}")
    em, sg7, ch = d.get("embed") or {}, d["stages"]["stage7"], d["chunks"]
    if em.get("store_present") and em.get("store_aware"):
        out.append(f"- **embed (S7):** {_n(sg7['done'])}/{_n(sg7['applicable'])} docs ({_pct(sg7['pct_complete'])}) · "
                   f"{_n(em.get('child_embedded'))}/{_n(em.get('child_total'))} child chunks in store")
    elif em.get("store_present"):
        out.append(f"- **embed (S7):** vector store present ({em.get('embedding_model')}); "
                   f"run `--chunks` for per-doc coverage")
    else:
        ready = ""
        if ch.get("chunk_unique_sha"):
            ready = f" · **{_n(ch['chunk_unique_sha'])}** unique chunks ready to embed"
            if em.get("projected_embed_usd") is not None:
                ready += f" (~{_usd(em['projected_embed_usd'])})"
        elif sg7["applicable"]:
            ready = f" · {_n(sg7['applicable'])} docs queued (run `--chunks` for chunk count + cost)"
        out.append(f"- **embed (S7):** not started — no vector store yet{ready}")
    out.append("- **ingest (S8):** n/a — requires a live-DB cross-check (online)")
    src = d.get("source_kinds") or {}
    if len(src) > 1:
        out.append("- **source kinds:** " + " · ".join(f"{k} {_n(v)}" for k, v in sorted(src.items())))
    ch = d["chunks"]
    if ch:
        line = f"- **chunks:** {_n(ch.get('s6_parents'))} parents · {_n(ch.get('s6_children'))} children · " \
               f"{_n(ch.get('empty_docs'))} empty docs"
        if "dedup_ratio" in ch:
            line += f" · dedup {ch['dedup_ratio']*100:.1f}%"
        out.append(line)
    out.append("")
    return out


# --------------------------------------------------------------------------- HTML
_CSS = """
:root{--bg:#0d1117;--card:#161b22;--card2:#1c2128;--line:#30363d;--txt:#e6edf3;--mut:#8b949e;
--accent:#58a6ff;--green:#2ea043;--amber:#d29922;--red:#f85149;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
.wrap{max-width:1180px;margin:0 auto;padding:32px 24px 64px;}
h1{font-size:24px;margin:0 0 4px;}
.sub{color:var(--mut);font-size:13px;margin-bottom:24px;}
.banner{background:#3d2b12;border:1px solid var(--amber);color:#f0d8a8;padding:10px 14px;border-radius:8px;
margin:0 0 12px;font-size:13px;}
.banner.err{background:#3d1518;border-color:var(--red);color:#f5c2c2;}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin:0 0 28px;}
.tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;}
.tile .v{font-size:24px;font-weight:600;}
.tile .k{color:var(--mut);font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin-top:4px;}
.tile .s{color:var(--mut);font-size:12px;margin-top:6px;}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
border-bottom:1px solid var(--line);padding-bottom:8px;margin:32px 0 16px;}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;margin:0 0 18px;}
.card.corpus{border-color:var(--accent);}
.chead{display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;margin-bottom:14px;}
.chead .name{font-size:18px;font-weight:600;}
.chead .meta{color:var(--mut);font-size:12px;}
.stages{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:14px 0;}
.stage{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:10px 12px;}
.stage .top{display:flex;justify-content:space-between;font-size:12px;margin-bottom:6px;}
.stage .lab{color:var(--txt);font-weight:600;}
.stage .num{color:var(--mut);}
.bar{height:6px;border-radius:3px;background:#21262d;overflow:hidden;}
.bar > i{display:block;height:100%;}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px 24px;margin-top:14px;
font-size:13px;}
.fact{display:flex;justify-content:space-between;border-bottom:1px dotted var(--line);padding:4px 0;}
.fact .k{color:var(--mut);}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;}
.foot{color:var(--mut);font-size:12px;margin-top:32px;text-align:center;}
.barrow{display:flex;gap:3px;align-items:center;margin-top:4px;}
.seg{height:8px;border-radius:2px;flex:1;}
.legend{font-size:12px;color:var(--mut);margin-top:6px;}
.legend span{margin-right:14px;}
.dot{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px;vertical-align:middle;}
"""


def to_html(report: dict) -> str:
    corpus = report["corpus"]
    sc = corpus.get("scan") or {}
    parts = ["<!doctype html><html lang=en><head><meta charset=utf-8>",
             "<meta name=viewport content='width=device-width,initial-scale=1'>",
             f"<title>Dataset Summary — {html.escape(SUMMARY_TOOL_NAME)}</title>",
             f"<style>{_CSS}</style></head><body><div class=wrap>"]
    parts.append(f"<h1>Dataset Summary</h1>")
    parts.append(f"<div class=sub>{html.escape(SUMMARY_TOOL_NAME)} {report['tool_version']} · "
                 f"generated {html.escape(report['generated_at_utc'])} · schema {report['schema_version']} · "
                 f"scan {html.escape(str(sc.get('mode','full')))} "
                 f"({_n(sc.get('docs_scanned'))}/{_n(sc.get('docs_total'))} docs, "
                 f"{_n(sc.get('cache_hits'))} cache hits, {sc.get('wall_clock_s')}s)</div>")
    if sc.get("extrapolated"):
        parts.append(f"<div class=banner>⚠ SAMPLE mode — only {sc.get('sampled_pct')}% of docs were read; "
                     f"all totals below are <b>extrapolated estimates</b>.</div>")
    for w in _warnings(report):
        parts.append(f"<div class='banner err'>⚠ {html.escape(w)}</div>")

    # hero tiles (corpus)
    t, c, s = corpus["totals"], corpus["cost"], corpus["safety"]
    ve, em = corpus.get("vision_embedding") or {}, corpus.get("embed") or {}
    parts.append("<div class=tiles>")
    parts.append(_tile(_n(len(report["datasets"])), "datasets"))
    parts.append(_tile(_n(t["docs"]), "documents", f"{_n(t['pages'])} pages · {_bytes(t['bytes'])}"))
    parts.append(_tile(_usd(c["total_usd"]), "spent so far",
                       f"~{_usd(c['projected_remaining_usd'])} to finish"))
    if ve.get("has_visual_assets"):
        cov = ve.get("coverage_pct")
        sub = (f"⚠ {_n(ve['docs_with_gap'])} docs with gaps" if ve.get("docs_with_gap")
               else f"{_n(ve['embedded_assets'])}/{_n(ve['has_visual_assets'])} assets")
        parts.append(_tile(_pct(cov), "vision embeddings", sub))
    parts.append(_tile(_n(s["docs_quarantined"]), "quarantined",
                       f"{_n(s['image_assets_minor'])} minor · {_n(s['image_assets_explicit'])} explicit"))
    parts.append("</div>")

    parts.append("<h2>Progress by dataset</h2>")
    if len(report["datasets"]) != 1:    # for a single-dataset report the corpus card == the dataset card
        parts.append(_html_card(corpus, corpus=True))
    for d in report["datasets"]:
        parts.append(_html_card(d, corpus=False))

    parts.append(f"<div class=foot>Read-only scan · derived from per-sidecar blocks, not run-manifests · "
                 f"schema {report['schema_version']}</div>")
    parts.append("</div></body></html>")
    return "".join(parts)


def _tile(v: str, k: str, s: str = "") -> str:
    sub = f"<div class=s>{html.escape(s)}</div>" if s else ""
    return f"<div class=tile><div class=v>{html.escape(v)}</div><div class=k>{html.escape(k)}</div>{sub}</div>"


def _html_card(d: dict, *, corpus: bool) -> str:
    t, c, s, r = d["totals"], d["cost"], d["safety"], d["routing"]
    cls = "card corpus" if corpus else "card"
    last = d.get("last_activity_utc") or "—"
    head = (f"<div class=chead><div class=name>{html.escape(d['dataset'])}</div>"
            f"<div class=meta>{_n(t['docs'])} docs · {_usd(c['total_usd'])} spent · last activity {html.escape(last)}</div></div>")

    stages = ["<div class=stages>"]
    for st in STAGES:
        sg = d["stages"][st]
        col = _STATE_COLOR.get(sg["state"], "#888")
        pct = sg["pct_complete"] if isinstance(sg["pct_complete"], (int, float)) else 0
        if sg["state"] == "n/a":
            num, width = "n/a", 0
        else:
            num = f"{_n(sg['done'])}/{_n(sg['applicable'])} · {_pct(sg['pct_complete'])}"
            width = pct
        stages.append(
            f"<div class=stage><div class=top><span class=lab>{html.escape(STAGE_LABELS[st])}</span>"
            f"<span class=num>{num}</span></div>"
            f"<div class=bar><i style='width:{width:.0f}%;background:{col}'></i></div></div>")
    stages.append("</div>")

    facts = ["<div class=facts>"]
    facts += _facts([
        ("pages", _n(t["pages"])), ("size", _bytes(t["bytes"])),
        ("text-sufficient pages", _n(t["pages_text_sufficient"])),
        ("need-vision pages", _n(t["pages_need_vision"])),
        ("S3 cost", _usd4(c["stage3_usd"])), ("S4 cost", _usd4(c["stage4_usd"])),
        ("S5 cost", _usd4(c["stage5_usd"])), ("projected remaining", _usd(c["projected_remaining_usd"])),
        ("quarantined", _n(s["docs_quarantined"])), ("docs w/ redaction", _n(s["docs_with_redaction"])),
        ("minor / explicit assets", f"{_n(s['image_assets_minor'])} / {_n(s['image_assets_explicit'])}"),
        ("chunk parents / children",
         f"{_n(d['chunks'].get('s6_parents'))} / {_n(d['chunks'].get('s6_children'))}"),
    ])
    ve, em = d.get("vision_embedding") or {}, d.get("embed") or {}
    if ve.get("has_visual_assets"):
        v = f"{_n(ve['embedded_assets'])}/{_n(ve['has_visual_assets'])} ({_pct(ve.get('coverage_pct'))})"
        if ve.get("docs_with_gap"):
            v += f"  ⚠ {_n(ve['docs_with_gap'])} gap"
        facts += _facts([("vision embeddings", v)])
    ch = d["chunks"]
    if em.get("store_present"):
        s7v = (f"{_n(em.get('embedded_unique_content_sha'))} shas" +
               (f" · {_n(em.get('child_embedded'))}/{_n(em.get('child_total'))} children"
                if em.get("store_aware") else " · --chunks for per-doc"))
    elif ch.get("chunk_unique_sha"):
        proj = em.get("projected_embed_usd")
        s7v = (f"not started · {_n(ch['chunk_unique_sha'])} ready"
               + (f" (~{_usd(proj)})" if proj is not None else ""))
    else:
        s7v = "not started — no store"
    facts += _facts([("embed (S7)", s7v), ("ingest (S8)", "n/a — needs DB")])
    facts.append("</div>")

    extras = []
    if r["stage1_recommendation"]:
        extras.append(_dist_bar("Routing (S1 recommendation)", r["stage1_recommendation"]))
    if r["stage5_source"]:
        extras.append(_dist_bar("Final summary source (S5)", r["stage5_source"]))

    return f"<div class='{cls}'>{head}{''.join(stages)}{''.join(facts)}{''.join(extras)}</div>"


def _facts(pairs) -> list[str]:
    return [f"<div class=fact><span class=k>{html.escape(k)}</span><span>{html.escape(v)}</span></div>"
            for k, v in pairs]


_DIST_COLORS = ["#58a6ff", "#2ea043", "#d29922", "#bc8cff", "#f85149", "#8b949e", "#39c5cf"]


def _dist_bar(title: str, dist: dict) -> str:
    total = sum(dist.values()) or 1
    items = sorted(dist.items(), key=lambda kv: (-kv[1], kv[0]))
    segs, legend = [], []
    for i, (k, v) in enumerate(items):
        col = _DIST_COLORS[i % len(_DIST_COLORS)]
        segs.append(f"<i class=seg style='flex:{v};background:{col}'></i>")
        legend.append(f"<span><i class=dot style='background:{col}'></i>{html.escape(str(k))} {_n(v)}</span>")
    return (f"<div style='margin-top:14px'><div class=k style='color:var(--mut);font-size:12px'>{html.escape(title)}</div>"
            f"<div class=barrow>{''.join(segs)}</div><div class=legend>{''.join(legend)}</div></div>")
