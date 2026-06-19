"""Stage-5 tunable knobs (Handoff §3/§9) in one place.

Every field is echoed verbatim into each sidecar's ``stage5.config`` so a JSON is self-describing and a
run is reproducible from its own output (mirrors Stage 1/2/3/4's ``*.config`` convention).

Stage 5 reuses the Stage-3 nano plumbing unchanged (``NanoClient`` / ``estimate_cost_usd`` /
``is_reasoning_model`` are duck-typed against these fields), so this config carries the same model /
determinism / rate-limit / cost fields as ``Stage3Config`` — minus the page-level knobs Stage 5 doesn't
use (per-page ``max_output_tokens``, ``max_input_chars``, ``skip_low_quality_text``), since Stage 5's only
call is the document reduce.

NOT in here (Handoff §9): the Azure connection — endpoint, deployment, api-version, and the api KEY. Those
come from the environment (see ``src/stage3/connection.py``, reused); the key is a secret and is NEVER
written to a sidecar or manifest. ``deployment``/``api_version``/``model``/``prompt_version`` ARE recorded
as provenance, but the endpoint and key are not.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Stage5Config:
    # --- model / determinism (Handoff §3) -----------------------------------------------------------
    model: str = "gpt-4.1-nano"      # informational provenance; the live deployment NAME comes from env
    temperature: float = 0.0         # deterministic-as-possible (still not byte-reproducible -> we stamp
    seed: int = 7                    #   provenance instead of promising identical bytes)
    max_doc_output_tokens: int = 1536  # the fusion reduce completion cap (reasoning models also think here)

    # Request shape (shared with Stage 3's client). "auto" picks the reasoning style
    # (max_completion_tokens + reasoning_effort, no temperature/seed) for gpt-5/o-series deployments and
    # the chat style (max_tokens + temperature + seed) otherwise — so Stage 5 works whether the configured
    # nano slot is gpt-4.1-nano or, as in the current sv-kb resource, gpt-5-mini. Force with chat/reasoning.
    param_style: str = "auto"        # auto | chat | reasoning
    reasoning_effort: str = "minimal"  # reasoning-style only: minimal keeps the fusion summary cheap/fast

    # --- the fusion reduce hierarchical fan-in (Handoff §3, mirrors Stage 3's doc-reduce) ------------
    max_doc_summary_input_chars: int = 24_000  # concat of the interleaved text+caption items over this ->
    #                                            hierarchical reduce (bias toward a single reduce where it fits)
    doc_summary_batch_size: int = 40   # items folded per intermediate reduce call
    doc_summary_max_depth: int = 3     # fan-in depth cap

    # --- throughput / rate limiting (Handoff §9) ----------------------------------------------------
    concurrency: int = 8             # bounded in-flight HTTP calls (thread pool; this stage is I/O-bound)
    requests_per_minute: int = 0     # 0 = no client-side RPM cap (rely on server 429 + backoff). Set to
    tokens_per_minute: int = 0       # 0 = no client-side TPM cap.   the deployment's quota for bulk runs.
    max_retries: int = 6             # per-call retries on 429/5xx (exponential backoff + jitter)
    retry_base_seconds: float = 0.5  # backoff base; honours Retry-After when the server sends one
    retry_max_seconds: float = 30.0  # backoff ceiling
    request_timeout_seconds: float = 120.0

    # --- cost accounting (Handoff §3/§10) — rates configurable; defaults = gpt-4.1-nano list price -----
    input_cost_per_million: float = 0.10
    cached_input_cost_per_million: float = 0.025  # cached-input is cheaper; prompt caching at scale
    output_cost_per_million: float = 0.40
    batch_discount: float = 0.5      # parity with Stage 3's cost fields (Stage 5 is realtime-only)

    # --- dry-run cost preview (Handoff §10) ---------------------------------------------------------
    dryrun_avg_output_tokens: int = 200  # assumed completion tokens for one fusion reduce when projecting

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for ``stage5.config`` (stable key order => deterministic output)."""
        return asdict(self)
