"""Provider-agnostic model calls (Stage 2).

Public surface: the request/response types and the `Provider` protocol from `base`,
the two adapters, the config-driven `ProviderPool`, and the versioned prompts. Nothing
outside this package imports a vendor SDK.
"""

from ba_radar.llm.anthropic import AnthropicProvider
from ba_radar.llm.base import (
    LLMError,
    LLMRequest,
    LLMResponse,
    Provider,
    ProviderConfigError,
    ProviderError,
    parse_reply,
    structured_schema,
)
from ba_radar.llm.openai import OpenAIProvider
from ba_radar.llm.prompts import ENUM_GUIDE, PROMPT_VERSIONS, PromptFile, load_prompt
from ba_radar.llm.select import (
    COLLECT_TASKS,
    KEY_ENV_NAMES,
    ProviderPool,
    TaskRoute,
    estimate_cost,
    resolve_task,
)

__all__ = [
    "COLLECT_TASKS",
    "ENUM_GUIDE",
    "KEY_ENV_NAMES",
    "PROMPT_VERSIONS",
    "AnthropicProvider",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "OpenAIProvider",
    "PromptFile",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderPool",
    "TaskRoute",
    "estimate_cost",
    "load_prompt",
    "parse_reply",
    "resolve_task",
    "structured_schema",
]
