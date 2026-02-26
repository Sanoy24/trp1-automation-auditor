"""
llm.py — Dynamic multi-provider LLM factory for the Automaton Auditor.

Supports per-node LLM configuration so you can use different models and
providers for different parts of the swarm:

  - Detectives: typically don't need LLM (pure forensic tools), but
    VisionInspector uses multimodal LLM for image classification.
  - Judges: use structured output (`.with_structured_output()`)
  - Chief Justice: uses free-text LLM for narrative generation only
  - VisionInspector: uses multimodal LLM for diagram classification

Supported providers: ollama, openai, anthropic, google

Configuration via .env:
  # Default LLM (used when no role-specific config exists)
  LLM_PROVIDER=ollama
  LLM_MODEL=qwen2.5
  LLM_BASE_URL=http://localhost:11434    # Ollama only

  # Role-specific overrides (optional)
  JUDGE_LLM_PROVIDER=ollama
  JUDGE_LLM_MODEL=qwen2.5
  JUSTICE_LLM_PROVIDER=ollama
  JUSTICE_LLM_MODEL=qwen2.5
  VISION_LLM_PROVIDER=ollama
  VISION_LLM_MODEL=llava

  # Provider API keys (only needed if using that provider)
  OPENAI_API_KEY=sk-...
  ANTHROPIC_API_KEY=sk-ant-...
  GOOGLE_API_KEY=AIza...
    GROQ_API_KEY=grq-...  # Groq Cloud API key (optional)
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# ---------------------------------------------------------------------------
# Default configuration (read once at import)
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
DEFAULT_MODEL: str = os.getenv("LLM_MODEL", os.getenv("OLLAMA_MODEL", "minimax-m2:cloud"))
DEFAULT_BASE_URL: str = os.getenv("LLM_BASE_URL", os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"))

# Role-specific overrides
ROLE_CONFIG = {
    "judge": {
        "provider": os.getenv("JUDGE_LLM_PROVIDER"),
        "model": os.getenv("JUDGE_LLM_MODEL"),
    },
    "justice": {
        "provider": os.getenv("JUSTICE_LLM_PROVIDER"),
        "model": os.getenv("JUSTICE_LLM_MODEL"),
    },
    "vision": {
        "provider": os.getenv("VISION_LLM_PROVIDER"),
        "model": os.getenv("VISION_LLM_MODEL"),
    },
}


def _resolve_config(role: Optional[str] = None) -> dict:
    """Resolve provider and model for a given role, falling back to defaults."""
    if role and role in ROLE_CONFIG:
        cfg = ROLE_CONFIG[role]
        provider = cfg.get("provider") or DEFAULT_PROVIDER
        model = cfg.get("model") or DEFAULT_MODEL
    else:
        provider = DEFAULT_PROVIDER
        model = DEFAULT_MODEL

    return {"provider": provider, "model": model}


# ---------------------------------------------------------------------------
# Provider-specific LLM constructors
# ---------------------------------------------------------------------------


def _create_ollama(model: str, temperature: float):
    """Create a ChatOllama instance."""
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=model,
        base_url=DEFAULT_BASE_URL,
        temperature=temperature,
        timeout=30,  # 30 second timeout for responsiveness
    )


def _create_openai(model: str, temperature: float):
    """Create a ChatOpenAI instance."""
    from langchain_openai import ChatOpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY not set in .env")
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=temperature,
        timeout=30,
    )


def _create_anthropic(model: str, temperature: float):
    """Create a ChatAnthropic instance."""
    from langchain_anthropic import ChatAnthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set in .env")
    return ChatAnthropic(
        model=model,
        api_key=api_key,
        temperature=temperature,
        timeout=30,
    )


def _create_google(model: str, temperature: float):
    """Create a ChatGoogleGenerativeAI instance."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not set in .env")
    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        temperature=temperature,
        timeout=30,
    )


def _create_groq(model: str, temperature: float):
    """Create a Groq Chat instance.

    This attempts to import a LangChain Groq integration (`langchain_groq`).
    If that package is not available, the import will raise with a clear
    message instructing how to install the required integration. The
    `GROQ_API_KEY` environment variable must be set when using this provider.
    """
    try:
        from langchain_groq import ChatGroq
    except Exception as exc:  # pragma: no cover - host-specific
        msg = (
            "To use the 'groq' provider you must install a Groq client integration,"
            " e.g. `pip install langchain-groq` (or provide your own factory)."
        )
        raise ImportError(msg) from exc

    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set in .env")

    return ChatGroq(
        model=model,
        api_key=api_key,
        temperature=temperature,
        timeout=30,
    )


# Registry mapping provider names to constructors
_PROVIDERS = {
    "ollama": _create_ollama,
    "openai": _create_openai,
    "anthropic": _create_anthropic,
    "google": _create_google,
    "groq": _create_groq,
    "gemini": _create_google,  # alias
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_llm(
    role: Optional[str] = None,
    temperature: float = 0.2,
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """Return a ChatModel instance for the given role.

    Resolution order for provider/model:
    1. Explicit `provider` / `model` arguments (highest priority)
    2. Role-specific env vars (e.g. JUDGE_LLM_PROVIDER)
    3. Default env vars (LLM_PROVIDER / LLM_MODEL)

    Args:
        role: Node role — "judge", "justice", "vision", or None for default.
        temperature: LLM temperature.
        provider: Override the provider (ollama/openai/anthropic/google).
        model: Override the model name.

    Returns:
        A LangChain ChatModel instance.
    """
    cfg = _resolve_config(role)
    prov = provider or cfg["provider"]
    mdl = model or cfg["model"]

    factory = _PROVIDERS.get(prov.lower())
    if factory is None:
        supported = ", ".join(_PROVIDERS.keys())
        raise ValueError(
            f"Unknown LLM provider '{prov}'. Supported: {supported}"
        )

    logger.debug(
        "Creating LLM: provider=%s, model=%s, role=%s, temperature=%.2f",
        prov, mdl, role or "default", temperature,
    )
    return factory(mdl, temperature)


def get_structured_llm(
    schema: Type[T],
    role: Optional[str] = None,
    temperature: float = 0.1,
    provider: Optional[str] = None,
    model: Optional[str] = None,
):
    """Return a ChatModel bound to a Pydantic schema via `.with_structured_output()`.

    The returned object always returns instances of *schema* on `.invoke()`.

    Args:
        schema: A Pydantic BaseModel subclass.
        role: Node role for config resolution.
        temperature: Lower = more predictable structured outputs.
        provider: Override provider.
        model: Override model.

    Returns:
        An LLM runnable whose `.invoke()` returns ``schema`` instances.
    """
    llm = get_llm(role=role, temperature=temperature, provider=provider, model=model)
    return llm.with_structured_output(schema)
