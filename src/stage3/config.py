"""Stage-3 tunable knobs (Handoff §3/§4/§5/§8) in one place.

Every field is echoed verbatim into each sidecar's ``stage3.config`` so a JSON is self-describing and a
run is reproducible from its own output (mirrors Stage 1's ``generator.config`` / Stage 2's
``stage2.config`` convention).

NOT in here (Handoff §2/§9): the Azure connection — endpoint, deployment, api-version, and the api KEY.
Those come from the environment (see ``connection.py``); the key is a secret and is NEVER written to the
sidecar or manifest. ``deployment``/``api_version``/``model``/``prompt_version`` ARE recorded as
provenance (Handoff §7), but the endpoint and key are not.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Stage3Config:
    # --- model / determinism (Handoff §3) -----------------------------------------------------------
    model: str = "gpt-4.1-nano"      # informational provenance; the live deployment NAME comes from env
    temperature: float = 0.0         # deterministic-as-possible (still not byte-reproducible -> we stamp
    seed: int = 7                    #   provenance instead of promising identical bytes, Handoff §9)
    max_output_tokens: int = 1024    # per-page completion cap (reasoning models also spend it on thinking)
    max_doc_output_tokens: int = 1536  # document-summary completion cap (a bit larger)

    # Request shape. "auto" picks the reasoning style (max_completion_tokens + reasoning_effort, no
    # temperature/seed) for gpt-5/o-series deployments and the chat style (max_tokens + temperature + seed)
    # otherwise — so Stage 3 works whether the configured deployment is gpt-4.1-nano or, as in the current
    # sv-kb resource, gpt-5-mini. Force with "chat"/"reasoning". (Determinism is weaker on reasoning models;
    # Handoff §9 already prefers provenance over byte-reproducibility.)
    param_style: str = "auto"        # auto | chat | reasoning
    reasoning_effort: str = "minimal"  # reasoning-style only: minimal keeps summaries cheap/fast

    # --- input truncation (Handoff §3) --------------------------------------------------------------
    max_input_chars: int = 24_000    # ~6k tokens; pages over this are truncated and marked truncated:true
    max_doc_summary_input_chars: int = 24_000  # concat of page-summaries over this -> hierarchical reduce

    # --- document-summary hierarchical reduce (Handoff §5) ------------------------------------------
    doc_summary_batch_size: int = 40   # page-summaries folded per intermediate reduce call
    doc_summary_max_depth: int = 3     # fan-in depth cap (bias toward a single reduce where it fits)

    # --- page selection / spend levers (Handoff §4/§8/§11) -----------------------------------------
    skip_low_quality_text: bool = True   # DEFAULT (chosen §11): skip pages Stage 2 flagged
    #                                      ``low_quality_text`` to cut spend. Flip with
    #                                      --no-skip-low-quality-text to summarize them as low_confidence.

    # --- throughput / rate limiting (Handoff §8) ---------------------------------------------------
    concurrency: int = 8             # bounded in-flight HTTP calls (thread pool; this stage is I/O-bound)
    requests_per_minute: int = 0     # 0 = no client-side RPM cap (rely on server 429 + backoff). Set to
    tokens_per_minute: int = 0       # 0 = no client-side TPM cap.   the deployment's quota for bulk runs.
    max_retries: int = 6             # per-call retries on 429/5xx (exponential backoff + jitter)
    retry_base_seconds: float = 0.5  # backoff base; honours Retry-After when the server sends one
    retry_max_seconds: float = 30.0  # backoff ceiling
    request_timeout_seconds: float = 120.0

    # --- cost accounting (Handoff §8) — rates are configurable; defaults = gpt-4.1-nano list price ----
    input_cost_per_million: float = 0.10
    cached_input_cost_per_million: float = 0.025  # cached-input is cheaper; prompt caching at scale (§8)
    output_cost_per_million: float = 0.40
    batch_discount: float = 0.5      # Azure Batch API ~50% cheaper (Handoff §8)

    # --- dry-run cost preview (Handoff §11) --------------------------------------------------------
    dryrun_avg_output_tokens: int = 160  # assumed completion tokens/page when projecting cost with no API

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``stage3.config`` (stable key order => deterministic output)."""
        return asdict(self)
