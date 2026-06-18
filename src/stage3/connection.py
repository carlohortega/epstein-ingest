"""Azure OpenAI connection, sourced from the environment (Handoff §2/§9).

Env var names match the existing sv-kb pipeline (svkb_pipeline/config.py, vision-page-job) so a single
``.env.local`` drives every stage:

    AZURE_OPENAI_ENDPOINT              e.g. https://<resource>.openai.azure.com  (or .cognitiveservices.azure.com)
    AZURE_OPENAI_API_KEY              the api KEY — SECRET, never written to a sidecar/manifest
    AZURE_OPENAI_CHAT_API_VERSION    default 2025-04-01-preview (must support json_schema structured outputs)
    AZURE_OPENAI_CHAT_NANO_DEPLOYMENT the gpt-4.1-nano deployment name

CLI flags may override endpoint / deployment / api-version (handy for testing); the KEY is env-only on
purpose (keeps it out of shell history and process listings).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_API_VERSION = "2025-04-01-preview"
DEFAULT_NANO_DEPLOYMENT = "gpt-4.1-nano"


@dataclass(frozen=True)
class Connection:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str

    @property
    def chat_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/openai/deployments/{self.deployment}"
                f"/chat/completions?api-version={self.api_version}")

    def url(self, path: str) -> str:
        """A non-deployment data-plane URL (Files / Batches), e.g. ``/openai/files``."""
        return f"{self.endpoint.rstrip('/')}/openai/{path.lstrip('/')}?api-version={self.api_version}"


class MissingConnection(RuntimeError):
    """Raised when the Azure connection isn't configured (endpoint/key/deployment missing)."""


def from_env(*, endpoint: str | None = None, deployment: str | None = None,
             api_version: str | None = None) -> Connection:
    """Build a Connection from the environment, with optional CLI overrides. Raises MissingConnection if
    a required value is absent — the api KEY is always env-only."""
    ep = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    dep = deployment or os.environ.get("AZURE_OPENAI_CHAT_NANO_DEPLOYMENT", DEFAULT_NANO_DEPLOYMENT)
    ver = api_version or os.environ.get("AZURE_OPENAI_CHAT_API_VERSION", DEFAULT_API_VERSION)
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", ep), ("AZURE_OPENAI_API_KEY", key),
                              ("AZURE_OPENAI_CHAT_NANO_DEPLOYMENT", dep)) if not v]
    if missing:
        raise MissingConnection("missing Azure OpenAI connection env vars: " + ", ".join(missing))
    return Connection(endpoint=ep, api_key=key, deployment=dep, api_version=ver)
