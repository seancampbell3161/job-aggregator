"""Shared LLM wiring: provider clients (providers.py) and structured output
(structured.py)."""
from src.llm.providers import (  # noqa: F401
    PROVIDER_KEYS, LlmBinding, build_binding, build_client, needs_key, resolve,
)
