"""Azure OpenAI **embeddings** connection, sourced from the environment (Handoff §6).

Env var names match the existing sv-kb pipeline (svkb_pipeline/embeddings.py, indexer-job/main.py) so a
single ``.env.local`` drives every stage:

    AZURE_OPENAI_ENDPOINT            e.g. https://<resource>.openai.azure.com  (or .cognitiveservices.azure.com)
    AZURE_OPENAI_API_KEY             the api KEY — SECRET, never written to a manifest
    AZURE_OPENAI_EMBED_DEPLOYMENT    the text-embedding-3-* deployment name (default text-embedding-3-small)
    AZURE_OPENAI_EMBED_API_VERSION   default 2024-10-21 (the api-version svkb_pipeline.embeddings.py pins)

The api KEY is env-only on purpose (keeps it out of shell history and process listings). CLI flags may
override endpoint / deployment / api-version (handy for testing). The MODEL name + DIMENSIONS are NOT here —
they are locked config (config.py) because they are baked into ``content_sha`` and select the target column.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Reuse Stage 3's MissingConnection so every stage raises the same "creds absent" error.
from ..stage3.connection import MissingConnection

DEFAULT_EMBED_DEPLOYMENT = "text-embedding-3-small"
DEFAULT_EMBED_API_VERSION = "2024-10-21"


@dataclass(frozen=True)
class EmbedConnection:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str

    @property
    def embeddings_url(self) -> str:
        # Mirrors svkb_pipeline.embeddings.AzureOpenAIEmbeddings's URL convention exactly (Handoff §3.2/§6).
        return (f"{self.endpoint.rstrip('/')}/openai/deployments/{self.deployment}"
                f"/embeddings?api-version={self.api_version}")


def _clean(v: str | None) -> str | None:
    # A CRLF .env.local makes sourced values carry a trailing '\r' (and stray spaces) that corrupts the
    # embeddings URL + trips the store's api-version match. Strip defensively (same fix as media/connection).
    return v.strip() if isinstance(v, str) else v


def from_env(*, endpoint: str | None = None, deployment: str | None = None,
             api_version: str | None = None) -> EmbedConnection:
    """Build the embeddings Connection from the environment, with optional CLI overrides. Raises
    MissingConnection if a required value is absent — the api KEY is always env-only."""
    ep = _clean(endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT"))
    key = _clean(os.environ.get("AZURE_OPENAI_API_KEY"))
    dep = _clean(deployment or os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT", DEFAULT_EMBED_DEPLOYMENT))
    ver = _clean(api_version or os.environ.get("AZURE_OPENAI_EMBED_API_VERSION", DEFAULT_EMBED_API_VERSION))
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", ep), ("AZURE_OPENAI_API_KEY", key),
                              ("AZURE_OPENAI_EMBED_DEPLOYMENT", dep)) if not v]
    if missing:
        raise MissingConnection("missing Azure OpenAI embeddings env vars: " + ", ".join(missing))
    return EmbedConnection(endpoint=ep, api_key=key, deployment=dep, api_version=ver)
