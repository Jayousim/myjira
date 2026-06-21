"""Provider-agnostic chat-model construction (LangChain).

Every agent role builds its LLM through this module so that Anthropic, OpenAI
(ChatGPT), and Google (Gemini) can be selected from a single ``"provider:model"``
string, e.g. ``"anthropic:claude-opus-4-8"``, ``"openai:gpt-4o"``, or
``"google_genai:gemini-1.5-pro"``.

Each provider's integration package reads its API key from the environment
(``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` / ``GOOGLE_API_KEY``), so callers
only need to pass the model string. Integration packages are imported lazily so
only the providers you actually use need to be installed.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage

# Provider prefix -> the env var the matching integration package expects for
# its API key.
_PROVIDER_ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google_genai": "GOOGLE_API_KEY",
    "google": "GOOGLE_API_KEY",
}

# Used when a bare (unprefixed) model string is given.
_DEFAULT_PROVIDER = "anthropic"


def provider_of(model: str) -> str:
    """Return the provider prefix of a ``"provider:model"`` string."""
    if ":" in model:
        return model.split(":", 1)[0]
    return _DEFAULT_PROVIDER


def _model_name(model: str) -> str:
    return model.split(":", 1)[1] if ":" in model else model


def build_chat_model(model: str, **kwargs: Any) -> BaseChatModel:
    """Build a LangChain chat model from a ``"provider:model"`` string.

    Extra keyword args (e.g. ``max_tokens``) are forwarded to the underlying
    chat model constructor. Provider-specific differences (such as Gemini using
    ``max_output_tokens``) are normalized here so callers can pass ``max_tokens``
    uniformly.
    """
    provider = provider_of(model)
    name = _model_name(model)

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(model=name, **kwargs)

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=name, **kwargs)

    if provider in ("google_genai", "google"):
        from langchain_google_genai import ChatGoogleGenerativeAI

        # Gemini exposes the output-token cap as ``max_output_tokens``.
        if "max_tokens" in kwargs:
            kwargs.setdefault("max_output_tokens", kwargs.pop("max_tokens"))
        return ChatGoogleGenerativeAI(model=name, **kwargs)

    raise ValueError(
        f"Unsupported model provider '{provider}' in '{model}'. "
        "Use one of: anthropic, openai, google_genai."
    )


def missing_provider_keys(*models: str) -> list[str]:
    """Return the env-var names required by ``models`` that are currently unset."""
    missing: list[str] = []
    for model in models:
        env_var = _PROVIDER_ENV_KEYS.get(provider_of(model))
        if env_var and not os.getenv(env_var) and env_var not in missing:
            missing.append(env_var)
    return missing


def to_openai_tools(tool_defs: list[dict]) -> list[dict]:
    """Convert Anthropic-style tool dicts to the OpenAI tool schema.

    The codebase defines tools once in Anthropic's native shape
    (``{"name", "description", "input_schema"}``). LangChain's ``bind_tools``
    normalizes the OpenAI ``{"type": "function", "function": {...}}`` shape for
    every provider, so this keeps a single source of truth for the schemas.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": defn["name"],
                "description": defn["description"],
                "parameters": defn["input_schema"],
            },
        }
        for defn in tool_defs
    ]


def message_text(message: BaseMessage) -> str:
    """Extract plain text from a chat message across providers.

    ``content`` is a string for some providers and a list of content blocks for
    others (notably Anthropic). Concatenate any text blocks and ignore the rest.
    """
    content = message.content
    if isinstance(content, str):
        return content.strip()

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts).strip()
