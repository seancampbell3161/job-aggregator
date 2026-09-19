"""Shared LLM wiring: provider clients (providers.py) and structured output
(structured.py)."""
from src.llm.providers import (  # noqa: F401
    PROVIDER_KEYS, LlmBinding, build_binding, build_client, missing_key, needs_key, resolve,
)
from src.llm.structured import complete_json  # noqa: F401,E402
