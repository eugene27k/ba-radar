"""Provider selection from configuration.

`llm.provider` is the default for every task; `llm.tasks.<task>.provider` overrides it
per task. Switching providers is a config edit plus a secret — never a code change.
Only the providers that configured tasks actually use need a key, and a missing key is
reported *before* any state is touched (see `ProviderConfigError`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ba_radar.llm.anthropic import AnthropicProvider
from ba_radar.llm.base import Provider, ProviderConfigError
from ba_radar.llm.openai import OpenAIProvider
from ba_radar.settings import LLMSecrets, LLMSettings

# The tasks a `collect` run needs. `weekly` is configured but not used in Stage 2.
COLLECT_TASKS: tuple[str, ...] = ("relevance", "analysis")

ProviderFactory = Callable[[str, float], Provider]


def _anthropic(key: str, timeout: float) -> Provider:
    return AnthropicProvider(key, timeout=timeout)


def _openai(key: str, timeout: float) -> Provider:
    return OpenAIProvider(key, timeout=timeout)


FACTORIES: dict[str, ProviderFactory] = {
    AnthropicProvider.name: _anthropic,
    OpenAIProvider.name: _openai,
}

KEY_ENV_NAMES: dict[str, str] = {
    AnthropicProvider.name: "ANTHROPIC_API_KEY",
    OpenAIProvider.name: "OPENAI_API_KEY",
}


@dataclass(frozen=True)
class TaskRoute:
    task: str
    provider: str
    model: str
    max_tokens: int
    batch_size: int
    effort: str | None


def resolve_task(settings: LLMSettings, task: str) -> TaskRoute:
    try:
        cfg = settings.tasks[task]
    except KeyError:
        raise ProviderConfigError(f"llm.tasks.{task} is not configured") from None
    provider = cfg.provider or settings.provider
    if provider not in FACTORIES:
        known = ", ".join(sorted(FACTORIES))
        raise ProviderConfigError(
            f"unknown provider '{provider}' for task '{task}' (known: {known})"
        )
    return TaskRoute(
        task=task,
        provider=provider,
        model=cfg.model,
        max_tokens=cfg.max_tokens,
        batch_size=cfg.batch_size or 1,
        effort=cfg.effort,
    )


def api_key_for(provider: str, secrets: LLMSecrets) -> str | None:
    if provider == AnthropicProvider.name:
        return secrets.anthropic_api_key
    if provider == OpenAIProvider.name:
        return secrets.openai_api_key
    return None


class ProviderPool:
    """Providers keyed by name, built lazily from settings and secrets.

    `for_tasks` performs the fail-fast check: every provider the given tasks route to
    must have a key. Tests pass ready-made providers (the fake) via `providers`.
    """

    def __init__(
        self,
        settings: LLMSettings,
        secrets: LLMSecrets | None = None,
        *,
        providers: Mapping[str, Provider] | None = None,
        factories: Mapping[str, ProviderFactory] | None = None,
    ) -> None:
        self.settings = settings
        self._secrets = secrets or LLMSecrets()
        self._providers: dict[str, Provider] = dict(providers or {})
        self._factories = dict(factories or FACTORIES)

    @classmethod
    def for_tasks(
        cls, settings: LLMSettings, secrets: LLMSecrets, tasks: tuple[str, ...] = COLLECT_TASKS
    ) -> ProviderPool:
        """Build a pool and verify every provider the tasks need has a key."""
        pool = cls(settings, secrets)
        missing: dict[str, list[str]] = {}  # provider -> tasks that route to it
        for task in tasks:
            route = resolve_task(settings, task)
            if not api_key_for(route.provider, secrets):
                missing.setdefault(route.provider, []).append(task)
        if missing:
            parts = [
                f"{KEY_ENV_NAMES.get(provider, provider.upper() + '_API_KEY')} "
                f"(provider '{provider}', used by {', '.join(names)})"
                for provider, names in missing.items()
            ]
            raise ProviderConfigError("missing model provider key(s): " + "; ".join(parts))
        return pool

    def route(self, task: str) -> TaskRoute:
        return resolve_task(self.settings, task)

    def provider(self, name: str) -> Provider:
        cached = self._providers.get(name)
        if cached is not None:
            return cached
        factory = self._factories.get(name)
        if factory is None:
            raise ProviderConfigError(f"unknown provider '{name}'")
        key = api_key_for(name, self._secrets)
        if not key:
            env_name = KEY_ENV_NAMES.get(name, f"{name.upper()}_API_KEY")
            raise ProviderConfigError(f"{env_name} is not set; provider '{name}' cannot be used")
        built = factory(key, self.settings.request_timeout_seconds)
        self._providers[name] = built
        return built

    def for_task(self, task: str) -> tuple[Provider, TaskRoute]:
        route = self.route(task)
        return self.provider(route.provider), route


def estimate_cost(
    settings: LLMSettings, model: str, input_tokens: int, output_tokens: int
) -> float | None:
    """USD estimate from the `llm.pricing` table; None when the model is not listed."""
    price = settings.pricing.get(model)
    if price is None:
        return None
    return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1_000_000
