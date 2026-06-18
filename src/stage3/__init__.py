"""text-first-extract Stage 3 — per-page text summaries + safety triage (gpt-4.1-nano, Azure OpenAI).

For every PDF already processed by Stages 1–2 (a ``status:"ok"`` sidecar with a complete ``stage2``
block), this stage makes **one bundled gpt-4.1-nano call per text/mixed page** that returns a faithful
``summary`` + structured ``summary_json`` (key_points/entities/topics) + a ``text_safety`` triage flag,
then rolls the page summaries up into a **provisional** per-document summary.

This is the FIRST stage that calls an LLM and the first that costs money, so token + USD cost is
measured per page, per document, and per run. It is also the first stage that touches the network —
but it writes ONLY to the local sidecar + run manifest: no Azure Storage uploads, no DB writes (Stage 7).

Safety here is a cheap *triage gate*, not a verdict: ``text_safety.flag`` is ``ok`` (no downstream check
needed) or ``review`` (escalate to the authoritative Azure Content Safety ``text:analyze`` scan later, on
that tiny subset only). Following the sv-kb policy we LABEL, we do not block — so there is no ``block``
value. The optional ``categories`` hint aligns to the Content Safety taxonomy
(``hate|sexual|violence|self_harm|csam_suspected``) but is advisory; downstream does the real scoring.

Idempotent, resumable, fault-isolated. Realtime and Azure Batch API modes. Azure connection (endpoint,
deployment, api-version, key) comes from env/config and the api KEY is never written to disk.

Spec: docs/HANDOFF-stage3-text-summaries.md
"""

STAGE3_TOOL_NAME = "text-first-extract-stage3"
STAGE3_TOOL_VERSION = "1.0.0"

# Bumped whenever the system prompt / response schema changes. Stamped into every summary's provenance so
# a sidecar is self-describing (outputs are LLM-generated => provenance over byte-determinism, Handoff §9).
PROMPT_VERSION = "stage3-2026-06-16.1"

__all__ = ["STAGE3_TOOL_NAME", "STAGE3_TOOL_VERSION", "PROMPT_VERSION"]
