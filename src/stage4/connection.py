"""Azure connections for Stage 4, sourced from the environment (Handoff §4/§5/§8).

Three independent Azure services, each with its OWN endpoint/key, so a single ``.env.local`` drives every
stage (names match the existing sv-kb pipeline):

  Vision (gpt-5-mini, Azure OpenAI) — REQUIRED:
    AZURE_OPENAI_ENDPOINT
    AZURE_OPENAI_API_KEY                 SECRET — never written to a sidecar/manifest
    AZURE_OPENAI_VISION_DEPLOYMENT       the gpt-5-mini vision deployment name
    AZURE_OPENAI_CHAT_API_VERSION        default 2025-04-01-preview (must support json_schema outputs)

  AI Vision multimodal embeddings — OPTIONAL (only when --embeddings; skipped if absent):
    AZURE_AI_VISION_ENDPOINT
    AZURE_AI_VISION_API_KEY              SECRET
    AZURE_AI_VISION_API_VERSION          default 2024-02-01
    AZURE_AI_VISION_MODEL_VERSION        default 2023-04-15

  Content Safety (image) — OPTIONAL (the CSAM net; gracefully skipped if not provisioned):
    AZURE_CONTENT_SAFETY_ENDPOINT
    AZURE_CONTENT_SAFETY_API_KEY         SECRET
    AZURE_CONTENT_SAFETY_API_VERSION     default 2024-09-01

CLI flags may override endpoints / deployment / api-versions (handy for testing); the KEYS are env-only on
purpose (keeps them out of shell history and process listings).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Reuse the Stage-3 Azure OpenAI Connection verbatim for the vision deployment — same chat-completions
# data-plane shape, same url builder, same "key lives only here" contract.
from ..stage3.connection import Connection, MissingConnection

DEFAULT_CHAT_API_VERSION = "2025-04-01-preview"
DEFAULT_VISION_DEPLOYMENT = "gpt-5-mini"
DEFAULT_AI_VISION_API_VERSION = "2024-02-01"
DEFAULT_AI_VISION_MODEL_VERSION = "2023-04-15"
DEFAULT_CONTENT_SAFETY_API_VERSION = "2024-09-01"


@dataclass(frozen=True)
class AIVisionConnection:
    """Azure AI Vision (Computer Vision) multimodal embeddings endpoint."""

    endpoint: str
    api_key: str
    api_version: str
    model_version: str

    @property
    def vectorize_image_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/computervision/retrieval:vectorizeImage"
                f"?api-version={self.api_version}&model-version={self.model_version}")


@dataclass(frozen=True)
class ContentSafetyConnection:
    """Azure AI Content Safety image:analyze endpoint."""

    endpoint: str
    api_key: str
    api_version: str

    @property
    def analyze_image_url(self) -> str:
        return (f"{self.endpoint.rstrip('/')}/contentsafety/image:analyze"
                f"?api-version={self.api_version}")


@dataclass(frozen=True)
class Stage4Connections:
    vision: Connection                              # REQUIRED
    ai_vision: AIVisionConnection | None            # None when not configured / --no-embeddings
    content_safety: ContentSafetyConnection | None  # None when not configured / --no-content-safety


def _vision_from_env(endpoint: str | None, deployment: str | None,
                     api_version: str | None) -> Connection:
    ep = endpoint or os.environ.get("AZURE_OPENAI_ENDPOINT")
    key = os.environ.get("AZURE_OPENAI_API_KEY")
    dep = deployment or os.environ.get("AZURE_OPENAI_VISION_DEPLOYMENT", DEFAULT_VISION_DEPLOYMENT)
    ver = api_version or os.environ.get("AZURE_OPENAI_CHAT_API_VERSION", DEFAULT_CHAT_API_VERSION)
    missing = [n for n, v in (("AZURE_OPENAI_ENDPOINT", ep), ("AZURE_OPENAI_API_KEY", key),
                              ("AZURE_OPENAI_VISION_DEPLOYMENT", dep)) if not v]
    if missing:
        raise MissingConnection("missing Azure OpenAI vision env vars: " + ", ".join(missing))
    return Connection(endpoint=ep, api_key=key, deployment=dep, api_version=ver)


def _ai_vision_from_env() -> AIVisionConnection | None:
    ep = os.environ.get("AZURE_AI_VISION_ENDPOINT")
    key = os.environ.get("AZURE_AI_VISION_API_KEY")
    if not ep or not key:
        return None
    return AIVisionConnection(
        endpoint=ep, api_key=key,
        api_version=os.environ.get("AZURE_AI_VISION_API_VERSION", DEFAULT_AI_VISION_API_VERSION),
        model_version=os.environ.get("AZURE_AI_VISION_MODEL_VERSION", DEFAULT_AI_VISION_MODEL_VERSION))


def _content_safety_from_env() -> ContentSafetyConnection | None:
    ep = os.environ.get("AZURE_CONTENT_SAFETY_ENDPOINT")
    key = os.environ.get("AZURE_CONTENT_SAFETY_API_KEY")
    if not ep or not key:
        return None
    return ContentSafetyConnection(
        endpoint=ep, api_key=key,
        api_version=os.environ.get("AZURE_CONTENT_SAFETY_API_VERSION", DEFAULT_CONTENT_SAFETY_API_VERSION))


def from_env(*, endpoint: str | None = None, deployment: str | None = None,
             api_version: str | None = None, want_embeddings: bool = True,
             want_content_safety: bool = True) -> Stage4Connections:
    """Build the Stage-4 connections. The vision connection is required (raises MissingConnection if
    absent); AI Vision + Content Safety are optional and returned as None when not configured or not
    requested — the caller warns and skips that capability rather than failing the run (Handoff §5)."""
    vision = _vision_from_env(endpoint, deployment, api_version)
    ai_vision = _ai_vision_from_env() if want_embeddings else None
    content_safety = _content_safety_from_env() if want_content_safety else None
    return Stage4Connections(vision=vision, ai_vision=ai_vision, content_safety=content_safety)
