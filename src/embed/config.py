"""Stage-7 tunable knobs (Handoff §3/§6/§7). Echoed verbatim into the run manifest so a run is reproducible
from its own output (mirrors Stage 1–6's ``*.config`` convention).

NOT in here (Handoff §6): the Azure connection — endpoint, deployment, api-version, and the api KEY. Those
come from the environment (see ``connection.py``); the key is a secret and is NEVER written to a manifest.
``api_version``/``deployment`` ARE recorded as provenance, but the endpoint and key are not.

The model + dimensions ARE config (Handoff §3.2): they are baked into every child's ``content_sha`` and pick
the target column (``embedding_small``/``_large``), so they are stamped into the store + manifest and locked
before a bulk run.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from . import DEFAULT_EMBEDDING_MODEL, DEFAULT_DIMENSIONS, DEFAULT_COST_PER_MILLION_TOKENS


@dataclass(frozen=True)
class EmbedConfig:
    # --- locked model identity (Handoff §3.2) — stamped into the store + manifest, baked into content_sha --
    model: str = DEFAULT_EMBEDDING_MODEL        # MUST match Stage 6's EMBEDDING_MODEL (inside content_sha)
    dimensions: int = DEFAULT_DIMENSIONS        # 1536 -> embedding_small; 3072 -> embedding_large

    # --- throughput / rate limiting (Handoff §6) — this stage IS network-bound ----------------------------
    batch_size: int = 32             # children per embeddings request (sv-kb convention)
    concurrency: int = 8             # bounded in-flight embedding requests (thread pool)
    requests_per_minute: int = 0     # 0 = no client-side RPM cap (rely on server 429 + backoff). Set to the
    tokens_per_minute: int = 0       # 0 = no client-side TPM cap.   deployment's quota for bulk runs.
    max_retries: int = 8             # per-request retries on 429/5xx (a real limiter — sv-kb's built-in is 4,
    #                                  thin for a multi-hour bulk run, Handoff §6)
    retry_base_seconds: float = 0.5  # exponential backoff base; honours Retry-After when the server sends one
    retry_max_seconds: float = 30.0  # backoff ceiling
    request_timeout_seconds: float = 60.0

    # --- cost accounting (Handoff §8) — rate configurable; default = text-embedding-3-small list price ------
    cost_per_million_tokens: float = DEFAULT_COST_PER_MILLION_TOKENS

    def echo(self) -> dict:
        """Ordered, JSON-safe echo for the run manifest (stable key order => deterministic output)."""
        return asdict(self)
