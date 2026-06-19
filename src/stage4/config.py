"""Stage-4 tunable knobs (Handoff §4/§5/§6) in one place.

Every field is echoed verbatim into each sidecar's ``stage4.config`` so a JSON is self-describing and a
run is reproducible from its own output (mirrors Stage 1/2/3's ``*.config`` convention).

NOT in here (Handoff §8): the Azure connections — endpoints, deployment, api-versions, and the api KEYS.
Those come from the environment (see ``connection.py``); keys are secrets and are NEVER written to a
sidecar or manifest. ``deployment``/``api_version``/``model``/``prompt_version`` ARE recorded as
provenance, but endpoints and keys are not.

The cost fields default to the ORIGINAL gpt-5-mini list price (Handoff §6). The live deployment may be a
versioned successor (e.g. gpt-5.4-mini ≈ 3× the price) — re-check and override ``--input-cost-per-million``
/ ``--output-cost-per-million`` at build/run time so the cost preview is honest.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Stage4Config:
    # --- model / determinism (Handoff §4) -----------------------------------------------------------
    model: str = "gpt-5-mini"        # informational provenance; the live deployment NAME comes from env
    temperature: float = 0.0         # chat-shape only; gpt-5-mini (reasoning shape) ignores temp/seed
    seed: int = 7                    #   (we stamp provenance instead of promising identical bytes)
    max_output_tokens: int = 900     # per-asset completion cap (reasoning models also spend it on thinking)

    # Request shape (shared with Stage 3's client). "auto" picks the reasoning style
    # (max_completion_tokens + reasoning_effort, no temperature/seed) for gpt-5/o-series deployments and
    # the chat style otherwise. gpt-5-mini => reasoning shape. Force with "chat"/"reasoning".
    param_style: str = "auto"        # auto | chat | reasoning
    reasoning_effort: str = "low"    # reasoning-style only: low keeps a visual verdict cheap/fast

    # Image detail sent to the model. "high" keeps dense/handwritten pages legible (the corpus is mostly
    # scans); "low"/"auto" are cheaper. Either way a full US-Letter page hits the 1536-patch cap and bills
    # ~2,490 tokens (Handoff §6), so "high" rarely costs more here but reads better.
    image_detail: str = "high"       # high | low | auto

    # --- Tier-1 local pre-gate (Handoff §2) — the free blank filter, ~zero risk -----------------------
    pregate: bool = True             # master switch for the local pre-gate
    # An `image` page whose Stage-1 dark_coverage (fraction of near-black pixels) is below this is a
    # confident WHITE BLANK (blank scan / broken-image placeholder / slip-sheet) -> skip the vision call.
    # CONSERVATIVE: only ultra-confident blanks, so vision still adjudicates anything ambiguous.
    pregate_blank_dark_max: float = 0.004
    # An asset whose near-black fraction is above this is an essentially solid-black page (Stage 1 already
    # gates > 0.9992 as blank; this catches the slightly-less-pure ones) -> black_or_redacted, no content.
    pregate_black_dark_min: float = 0.995
    # For escalated low_quality_text pages + artifacts (no Stage-1 dark_coverage), measure the rendered
    # PNG's near-black fraction at gate time (PIL). Disable to send every such asset straight to vision.
    pregate_measure_png: bool = True
    pregate_png_dark_level: int = 64    # 0..255 grayscale threshold: pixels <= this count as "near-black"

    # --- image embeddings (Handoff §5) — Azure AI Vision multimodal, has_visual_content=true only ------
    embeddings: bool = True          # produce an image embedding for kept assets (cheap vs the vision call)

    # --- content safety (Handoff §5) — Azure Content Safety on photo artifacts (the CSAM net) ----------
    content_safety: bool = True      # run Content Safety when the env is configured; gracefully skipped if not
    content_safety_artifacts_only: bool = True  # scan `artifact` assets only (photos embedded in text docs);
    #                                              set false to also scan full image pages

    # --- throughput / rate limiting (Handoff §6/§8) -------------------------------------------------
    concurrency: int = 6             # bounded in-flight vision calls (thread pool; this stage is I/O-bound)
    requests_per_minute: int = 0     # 0 = no client-side RPM cap (rely on server 429 + backoff)
    tokens_per_minute: int = 0       # 0 = no client-side TPM cap
    max_retries: int = 6             # per-call retries on 429/5xx (exponential backoff + jitter)
    retry_base_seconds: float = 0.5
    retry_max_seconds: float = 30.0
    request_timeout_seconds: float = 180.0   # image upload + reasoning can be slow

    # --- cost accounting (Handoff §6) — rates are configurable; defaults = ORIGINAL gpt-5-mini --------
    input_cost_per_million: float = 0.25
    cached_input_cost_per_million: float = 0.025
    output_cost_per_million: float = 2.00
    batch_discount: float = 0.5      # Azure Batch API ~50% cheaper (Handoff §6)

    # --- dry-run cost preview (Handoff §10) ---------------------------------------------------------
    dryrun_image_input_tokens: int = 2490   # billed tokens for a full-page image at the 1536-patch cap
    dryrun_artifact_input_tokens: int = 1100  # artifacts are smaller (fewer patches)
    dryrun_prompt_overhead_tokens: int = 220  # the constant system+instruction text per call
    dryrun_avg_output_tokens: int = 230       # assumed completion tokens/asset when projecting cost

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``stage4.config`` (stable key order => deterministic output)."""
        return asdict(self)
