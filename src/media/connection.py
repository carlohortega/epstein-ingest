"""Azure connections for the media track, sourced from the environment (HANDOFF-media-track §8).

Three resources, each env-driven (the api KEY is SECRET — never written to a sidecar/manifest, only the
non-secret endpoint host/deployment/api-version are recorded as provenance):

  Transcription   AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY
                  AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT (default gpt-4o-mini-transcribe)
                  AZURE_OPENAI_CHAT_API_VERSION       (default 2025-04-01-preview)
  Image vision    AZURE_AI_VISION_ENDPOINT / AZURE_AI_VISION_KEY (vectorizeImage, 1024-d)
  Content Safety  AZURE_CONTENT_SAFETY_ENDPOINT / AZURE_CONTENT_SAFETY_KEY

Env var names match the sv-kb pipeline + the PDF track's Stage 3/4 so a single ``.env.local`` drives
everything. CLI flags may override endpoint/deployment/api-version (handy for testing); the KEY is
env-only on purpose (keeps it out of shell history and process listings).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_TRANSCRIBE_DEPLOYMENT = "gpt-4o-mini-transcribe"
DEFAULT_CHAT_API_VERSION = "2025-04-01-preview"
DEFAULT_VISION_API_VERSION = "2024-02-01"
DEFAULT_VISION_MODEL_VERSION = "2023-04-15"
DEFAULT_CONTENT_SAFETY_API_VERSION = "2024-09-01"


class MissingConnection(RuntimeError):
    """Raised when a required Azure connection isn't configured (endpoint/key/deployment missing)."""


def _env(name: str, default: str | None = None) -> str | None:
    """Read an env var, stripping surrounding whitespace incl. trailing ``\\r``. ``~/.env.local`` is
    CRLF, so a naive ``os.environ.get`` yields ``'...azure.com\\r'`` which corrupts every URL/api-version
    and silently fails all calls. Strip defensively so the media stages work however env is loaded."""
    v = os.environ.get(name, default)
    return v.strip() if isinstance(v, str) else v


@dataclass(frozen=True)
class TranscribeConnection:
    endpoint: str
    api_key: str
    deployment: str
    api_version: str

    @property
    def transcription_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/openai/deployments/{self.deployment}"
                f"/audio/transcriptions?api-version={self.api_version}")

    def provenance(self) -> dict:
        """Non-secret provenance for the sidecar (endpoint host only; never the key)."""
        return {"deployment": self.deployment, "api_version": self.api_version,
                "endpoint_host": _host(self.endpoint)}


@dataclass(frozen=True)
class VisionConnection:
    endpoint: str
    api_key: str
    api_version: str
    model_version: str

    @property
    def vectorize_image_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/computervision/retrieval:vectorizeImage"
                f"?api-version={self.api_version}&model-version={self.model_version}")

    def provenance(self) -> dict:
        return {"api_version": self.api_version, "model_version": self.model_version,
                "endpoint_host": _host(self.endpoint)}


@dataclass(frozen=True)
class ContentSafetyConnection:
    endpoint: str
    api_key: str
    api_version: str

    @property
    def analyze_image_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/contentsafety/image:analyze"
                f"?api-version={self.api_version}")

    def provenance(self) -> dict:
        return {"api_version": self.api_version, "endpoint_host": _host(self.endpoint)}


def _host(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    e = endpoint.split("://", 1)[-1]
    return e.split("/", 1)[0]


def transcribe_from_env(*, endpoint: str | None = None, deployment: str | None = None,
                        api_version: str | None = None) -> TranscribeConnection:
    ep = endpoint or _env("AZURE_OPENAI_ENDPOINT")
    key = _env("AZURE_OPENAI_API_KEY")
    dep = deployment or _env("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT", DEFAULT_TRANSCRIBE_DEPLOYMENT)
    ver = api_version or _env("AZURE_OPENAI_CHAT_API_VERSION", DEFAULT_CHAT_API_VERSION)
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", ep), ("AZURE_OPENAI_API_KEY", key),
                              ("AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT", dep)) if not v]
    if missing:
        raise MissingConnection("missing transcription env vars: " + ", ".join(missing))
    return TranscribeConnection(endpoint=ep, api_key=key, deployment=dep, api_version=ver)


def vision_from_env(*, endpoint: str | None = None, api_version: str | None = None,
                    model_version: str | None = None) -> VisionConnection:
    ep = endpoint or _env("AZURE_AI_VISION_ENDPOINT")
    key = _env("AZURE_AI_VISION_KEY") or _env("AZURE_AI_VISION_API_KEY")
    ver = api_version or _env("AZURE_AI_VISION_API_VERSION", DEFAULT_VISION_API_VERSION)
    mv = model_version or _env("AZURE_AI_VISION_MODEL_VERSION", DEFAULT_VISION_MODEL_VERSION)
    missing = [n for n, v in (("AZURE_AI_VISION_ENDPOINT", ep), ("AZURE_AI_VISION_KEY", key)) if not v]
    if missing:
        raise MissingConnection("missing AI Vision env vars: " + ", ".join(missing))
    return VisionConnection(endpoint=ep, api_key=key, api_version=ver, model_version=mv)


def caption_from_env(*, endpoint: str | None = None, deployment: str | None = None,
                     api_version: str | None = None):
    """Build the Azure OpenAI chat connection for media captioning (gpt-5-mini, multimodal). Reuses the
    PDF track's Stage-3 ``Connection`` (same chat-completions data-plane + url builder) and the Stage-4
    vision deployment env var, so caption calls share one ``.env.local`` with the PDF vision lane. The
    api KEY is env-only. Returns a ``src.stage3.connection.Connection``."""
    from ..stage3.connection import Connection, MissingConnection as _S3Missing
    ep = endpoint or _env("AZURE_OPENAI_ENDPOINT")
    key = _env("AZURE_OPENAI_API_KEY")
    dep = deployment or _env("AZURE_OPENAI_VISION_DEPLOYMENT", "gpt-5-mini")
    ver = api_version or _env("AZURE_OPENAI_CHAT_API_VERSION", DEFAULT_CHAT_API_VERSION)
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", ep), ("AZURE_OPENAI_API_KEY", key),
                              ("AZURE_OPENAI_VISION_DEPLOYMENT", dep)) if not v]
    if missing:
        raise MissingConnection("missing caption (Azure OpenAI vision) env vars: " + ", ".join(missing))
    return Connection(endpoint=ep, api_key=key, deployment=dep, api_version=ver)


def content_safety_from_env(*, endpoint: str | None = None,
                            api_version: str | None = None) -> ContentSafetyConnection:
    ep = endpoint or _env("AZURE_CONTENT_SAFETY_ENDPOINT")
    key = _env("AZURE_CONTENT_SAFETY_KEY") or _env("AZURE_CONTENT_SAFETY_API_KEY")
    ver = api_version or _env("AZURE_CONTENT_SAFETY_API_VERSION", DEFAULT_CONTENT_SAFETY_API_VERSION)
    missing = [n for n, v in (("AZURE_CONTENT_SAFETY_ENDPOINT", ep),
                              ("AZURE_CONTENT_SAFETY_KEY", key)) if not v]
    if missing:
        raise MissingConnection("missing Content Safety env vars: " + ", ".join(missing))
    return ContentSafetyConnection(endpoint=ep, api_key=key, api_version=ver)
