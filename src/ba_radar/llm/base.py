"""Provider-neutral request and response types for the model layer.

Every model call in BA Radar is a *structured* call: a system prompt, one user
message, and a pydantic model describing the JSON reply. Adapters translate that into
the provider's own structured-output shape and back; nothing outside `ba_radar.llm`
imports a vendor SDK.

`LLMRequest.output_model` is the source of truth for the reply format. Its JSON schema
is derived at request time and sanitised for the subset both providers accept
(`structured_schema`), and the reply is validated with the pydantic model itself, so
range checks the providers cannot express in the schema still run on our side.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, cast

from pydantic import BaseModel, ValidationError


class LLMError(Exception):
    """Base class for everything the model layer raises."""


class ProviderConfigError(LLMError):
    """The configuration cannot produce a working provider: unknown name, missing key.

    Raised before any state is touched so a misconfigured run collects nothing — the
    excerpts of anything collected without a provider would be gone for good.
    """


class ProviderError(LLMError):
    """A call failed at runtime: API error, timeout, truncation, refusal, bad JSON."""


@dataclass(frozen=True)
class LLMRequest[T: BaseModel]:
    task: str
    model: str
    system: str
    user: str
    output_model: type[T]
    max_tokens: int
    effort: str | None = None
    # Free-form context for logging and for the fake provider in tests (e.g. the item
    # keys in a scoring batch). Never sent to a provider.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def json_schema(self) -> dict[str, Any]:
        return structured_schema(self.output_model)


@dataclass(frozen=True)
class LLMResponse[T: BaseModel]:
    parsed: T
    input_tokens: int
    output_tokens: int
    model: str
    provider: str


class Provider(Protocol):
    """One implementation per vendor. `name` is the config key (`llm.provider`)."""

    name: str

    async def complete[T: BaseModel](self, request: LLMRequest[T]) -> LLMResponse[T]: ...


# ---------------------------------------------------------------------------
# JSON schema and reply parsing shared by every adapter
# ---------------------------------------------------------------------------

# Keywords neither provider's strict structured-output mode accepts. Pydantic emits
# them for `Field(ge=..., le=...)` and friends; the pydantic model still enforces the
# constraint when the reply is validated, so dropping them from the wire schema loses
# nothing.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
        "uniqueItems",
        "format",
        "title",
    }
)


def structured_schema(model: type[BaseModel]) -> dict[str, Any]:
    """The JSON schema of `model`, reduced to what both providers' strict modes accept.

    Every object gets `additionalProperties: false` and every property becomes
    required — OpenAI's strict mode demands both, Anthropic's requires the first.
    """
    schema = model.model_json_schema()
    return cast(dict[str, Any], _sanitise(schema))


def _sanitise(node: Any) -> Any:
    if isinstance(node, dict):
        cleaned: dict[str, Any] = {}
        for key, value in node.items():
            if key in _UNSUPPORTED_KEYWORDS:
                continue
            if key in ("properties", "$defs", "definitions"):
                cleaned[key] = {name: _sanitise(sub) for name, sub in value.items()}
            else:
                cleaned[key] = _sanitise(value)
        if cleaned.get("type") == "object" and "properties" in cleaned:
            cleaned["additionalProperties"] = False
            cleaned["required"] = list(cleaned["properties"])
        return cleaned
    if isinstance(node, list):
        return [_sanitise(item) for item in node]
    return node


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def parse_reply[T: BaseModel](text: str, output_model: type[T]) -> T:
    """Validate a provider's JSON text against the request's pydantic model.

    Structured-output modes guarantee valid JSON, but a fenced block is tolerated so a
    provider that falls back to plain text does not fail the whole batch on cosmetics.
    """
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"reply is not valid JSON: {exc}") from exc
    try:
        return output_model.model_validate(payload)
    except ValidationError as exc:
        raise ProviderError(
            f"reply does not match the {output_model.__name__} schema: {exc}"
        ) from exc


def redact(text: str, secret: str | None) -> str:
    """Strip a secret from error text before it reaches a run log or an Actions log."""
    if not secret:
        return text
    return text.replace(secret, "***")
