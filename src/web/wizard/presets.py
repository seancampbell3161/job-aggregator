"""Ready-made LLM recipes for the wizard's first-run step.

Provider, model and host have to agree — gpt-oss:120b targets hosted Ollama
Cloud and is far too big for the bundled local service, while gpt-oss:20b
returns empty content on Cloud — and no single set of config defaults can be
right for both. A preset carries the whole combination, so the coupling is
something the user picks rather than something they have to already know.

Data, not markup: defining the recipes here lets the tests apply each one to
AppConfig and assert it validates, so a typo'd provider or a renamed config
path fails CI instead of shipping a button that fills the form with rubbish.
The cost lines and key URLs are quoted from GETTING_STARTED.md's provider
table (tests/web/wizard/test_presets.py guards them against drifting apart).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LLMPreset:
    key: str            # stable id, used as the button's data attribute
    label: str          # button text
    summary: str        # one line under the button: what this route needs
    cost: str           # quoted from GETTING_STARTED.md's provider table
    steps: tuple[str, ...]
    values: dict[str, object]   # config path -> value, applied to the form
    secret: str | None = None   # the Secrets field this route needs, if any
    key_url: str | None = None  # where that key comes from; guarded against docs
    commands: tuple[str, ...] = field(default_factory=tuple)


LLM_PRESETS: tuple[LLMPreset, ...] = (
    LLMPreset(
        key="ollama_local",
        label="Local Ollama",
        summary="runs on your machine · no key",
        cost="$0 — runs on your box.",
        steps=(
            "Start the bundled Ollama service (it does not run by default).",
            "Pull the model — a few GB, once.",
            "No API key needed.",
        ),
        commands=(
            "docker compose --profile ollama up -d",
            "docker compose exec ollama ollama pull llama3.1:8b",
        ),
        values={
            "relevance.provider": "ollama",
            "relevance.model": "llama3.1:8b",
            "relevance.ollama_host": "http://ollama:11434",
            # Local models are slower than the 10s default; the shipped
            # recommendation for a large local model is 20 and llama3.1:8b
            # on a laptop can sit just past it under load.
            "relevance.timeout_seconds": 30,
        },
    ),
    LLMPreset(
        key="ollama_cloud",
        label="Ollama Cloud",
        summary="hosted · needs an ollama api key",
        cost="Flat monthly subscription (GPU-time, not per token).",
        steps=(
            "Create an account at ollama.com.",
            "Generate an API key at ollama.com/settings/keys.",
            "Paste it into the ollama api key field below.",
        ),
        values={
            "relevance.provider": "ollama",
            "relevance.model": "gpt-oss:120b",
            "relevance.ollama_host": "https://ollama.com",
            "relevance.timeout_seconds": 10,
        },
        secret="ollama_api_key",
        key_url="https://ollama.com/settings/keys",
    ),
    LLMPreset(
        key="anthropic",
        label="Anthropic",
        summary="Claude Haiku · needs an anthropic api key",
        cost=("Per-token; a few $/day while clearing a backlog, "
              "pennies/day at steady state."),
        steps=(
            "Sign up at console.anthropic.com.",
            "Create an API key.",
            "Paste it into the anthropic api key field below.",
        ),
        values={
            "relevance.provider": "anthropic",
            "relevance.model": "claude-haiku-4-5",
            # Unused by this provider, but still reset: a recipe sets every
            # field the others do, so clicking one never leaves a stale value
            # behind from the last (see test_every_preset_sets_the_same_fields).
            "relevance.ollama_host": "http://ollama:11434",
            "relevance.timeout_seconds": 10,
        },
        secret="anthropic_api_key",
        key_url="https://console.anthropic.com/",
    ),
    LLMPreset(
        key="gemini",
        label="Gemini",
        summary="free tier · needs a google api key",
        cost="Free tier (~1,500 req/day).",
        steps=(
            "Get a key at aistudio.google.com — no card required.",
            "Paste it into the google api key field below.",
        ),
        values={
            "relevance.provider": "gemini",
            "relevance.model": "gemini-2.0-flash",
            # Unused by this provider, but still reset: a recipe sets every
            # field the others do, so clicking one never leaves a stale value
            # behind from the last (see test_every_preset_sets_the_same_fields).
            "relevance.ollama_host": "http://ollama:11434",
            "relevance.timeout_seconds": 10,
        },
        secret="google_api_key",
        key_url="https://aistudio.google.com",
    ),
)


def preset_for_provider(provider: str, ollama_host: str) -> str:
    """The preset whose panel to open on arrival, so the instructions match
    the form the user is looking at rather than starting blank. Ollama splits
    on the host, which is the only thing distinguishing local from Cloud."""
    if provider == "ollama":
        return "ollama_cloud" if "ollama.com" in ollama_host else "ollama_local"
    return provider if provider in {p.key for p in LLM_PRESETS} else "ollama_local"
