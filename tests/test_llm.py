"""The provider layer: schema, adapters, routing. No network anywhere."""

from __future__ import annotations

import json
from typing import Any

import anthropic
import httpx2
import openai
import pytest
from anthropic.types import Message
from openai.types.responses import Response
from pydantic import BaseModel, Field

from ba_radar.llm import (
    AnthropicProvider,
    LLMRequest,
    OpenAIProvider,
    ProviderConfigError,
    ProviderError,
    ProviderPool,
    estimate_cost,
    load_prompt,
    parse_reply,
    resolve_task,
    structured_schema,
)
from ba_radar.llm import anthropic as anthropic_adapter
from ba_radar.llm import openai as openai_adapter
from ba_radar.llm.base import redact
from ba_radar.pipeline.score import RelevanceBatch
from ba_radar.settings import LLMSecrets, LLMSettings, Settings

from .conftest import fixture_text


class Inner(BaseModel):
    label: str = Field(min_length=1, max_length=40)


class Reply(BaseModel):
    score: int = Field(ge=0, le=100)
    items: list[Inner]
    note: str


def _walk(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        found.append(node)
        for value in node.values():
            found.extend(_walk(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk(value))
    return found


def test_structured_schema_fits_both_providers_strict_modes() -> None:
    schema = structured_schema(Reply)
    nodes = _walk(schema)
    banned = {"minimum", "maximum", "minLength", "maxLength", "title", "pattern"}
    assert not any(key in node for node in nodes for key in banned)
    for node in nodes:
        if node.get("type") == "object" and "properties" in node:
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])


def relevance_request(**overrides: Any) -> LLMRequest[RelevanceBatch]:
    base: dict[str, Any] = {
        "task": "relevance",
        "model": "claude-sonnet-5",
        "system": "rubric",
        "user": "items",
        "output_model": RelevanceBatch,
        "max_tokens": 4000,
    }
    base.update(overrides)
    return LLMRequest(**base)


def test_parse_reply_validates_and_tolerates_a_fenced_block() -> None:
    payload = '{"verdicts": [{"key": "1", "relevance": "77", "tags": ["evals"], "rationale": "r"}]}'
    parsed = parse_reply(f"```json\n{payload}\n```", RelevanceBatch)
    assert parsed.verdicts[0].relevance == 77

    with pytest.raises(ProviderError, match="not valid JSON"):
        parse_reply("nope", RelevanceBatch)
    with pytest.raises(ProviderError, match="schema"):
        parse_reply('{"verdicts": [{"key": "1"}]}', RelevanceBatch)


def test_anthropic_request_sends_no_sampling_or_thinking_parameters() -> None:
    kwargs = anthropic_adapter.build_request(relevance_request())
    assert "temperature" not in kwargs and "thinking" not in kwargs
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert kwargs["output_config"]["format"]["schema"]["additionalProperties"] is False
    assert "effort" not in kwargs["output_config"]
    assert kwargs["messages"] == [{"role": "user", "content": "items"}]

    with_effort = anthropic_adapter.build_request(relevance_request(effort="medium"))
    assert with_effort["output_config"]["effort"] == "medium"


def test_openai_request_uses_strict_json_schema_on_the_responses_api() -> None:
    kwargs = openai_adapter.build_request(relevance_request(model="gpt-x"))
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "relevance"
    assert kwargs["instructions"] == "rubric" and kwargs["input"] == "items"
    assert "temperature" not in kwargs and "reasoning" not in kwargs

    with_effort = openai_adapter.build_request(relevance_request(model="gpt-x", effort="low"))
    assert with_effort["reasoning"] == {"effort": "low"}


def test_both_adapters_parse_recorded_responses_to_the_same_object() -> None:
    request = relevance_request()
    message = Message.model_validate(json.loads(fixture_text("anthropic_message.json")))
    response = Response.model_validate(json.loads(fixture_text("openai_response.json")))

    via_anthropic = anthropic_adapter.parse_message(message, request)
    via_openai = openai_adapter.parse_response(response, request)

    assert via_anthropic.parsed == via_openai.parsed
    assert via_anthropic.parsed.verdicts[0].relevance == 72
    assert via_anthropic.parsed.verdicts[1].tags == []
    assert (via_anthropic.provider, via_openai.provider) == ("anthropic", "openai")
    assert (via_anthropic.input_tokens, via_anthropic.output_tokens) == (1843, 96)
    assert (via_openai.input_tokens, via_openai.output_tokens) == (1901, 140)
    assert via_anthropic.model == "claude-sonnet-5"


def test_anthropic_truncation_and_refusal_are_provider_errors() -> None:
    request = relevance_request()
    raw = json.loads(fixture_text("anthropic_message.json"))
    for stop_reason, phrase in (("max_tokens", "truncated"), ("refusal", "refused")):
        message = Message.model_validate({**raw, "stop_reason": stop_reason})
        with pytest.raises(ProviderError, match=phrase):
            anthropic_adapter.parse_message(message, request)


def test_openai_incomplete_and_refusal_are_provider_errors() -> None:
    request = relevance_request(model="gpt-x")
    raw = json.loads(fixture_text("openai_response.json"))

    incomplete = Response.model_validate(
        {**raw, "status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}
    )
    with pytest.raises(ProviderError, match="max_output_tokens"):
        openai_adapter.parse_response(incomplete, request)

    refused = json.loads(json.dumps(raw))
    refused["output"][1]["content"] = [{"type": "refusal", "refusal": "no"}]
    with pytest.raises(ProviderError, match="refused"):
        openai_adapter.parse_response(Response.model_validate(refused), request)


class _StubMessages:
    def __init__(self, outcome: Message | Exception) -> None:
        self.outcome = outcome
        self.kwargs: dict[str, Any] | None = None

    async def create(self, **kwargs: Any) -> Message:
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _StubAnthropic:
    def __init__(self, outcome: Message | Exception) -> None:
        self.messages = _StubMessages(outcome)


async def test_anthropic_complete_round_trips_through_the_sdk_client() -> None:
    message = Message.model_validate(json.loads(fixture_text("anthropic_message.json")))
    stub = _StubAnthropic(message)
    provider = AnthropicProvider("sk-secret", client=stub)  # type: ignore[arg-type]

    response = await provider.complete(relevance_request())

    assert response.parsed.verdicts[0].key == "1"
    assert stub.messages.kwargs is not None and stub.messages.kwargs["model"] == "claude-sonnet-5"


async def test_anthropic_sdk_errors_become_provider_errors_without_the_key() -> None:
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    timeout = _StubAnthropic(anthropic.APITimeoutError(request=request))
    with pytest.raises(ProviderError, match="timed out"):
        await AnthropicProvider("sk-secret", client=timeout).complete(  # type: ignore[arg-type]
            relevance_request()
        )

    body = {"error": {"type": "authentication_error", "message": "bad key sk-secret"}}
    http_response = httpx2.Response(401, request=request, json=body)
    status = _StubAnthropic(
        anthropic.AuthenticationError("bad key sk-secret", response=http_response, body=body)
    )
    with pytest.raises(ProviderError) as excinfo:
        await AnthropicProvider("sk-secret", client=status).complete(  # type: ignore[arg-type]
            relevance_request()
        )
    assert "sk-secret" not in str(excinfo.value)
    assert "401" in str(excinfo.value)


class _StubResponses:
    def __init__(self, outcome: Response | Exception) -> None:
        self.outcome = outcome

    async def create(self, **kwargs: Any) -> Response:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _StubOpenAI:
    def __init__(self, outcome: Response | Exception) -> None:
        self.responses = _StubResponses(outcome)


async def test_openai_complete_round_trips_and_maps_errors() -> None:
    response = Response.model_validate(json.loads(fixture_text("openai_response.json")))
    provider = OpenAIProvider("sk-openai", client=_StubOpenAI(response))  # type: ignore[arg-type]
    result = await provider.complete(relevance_request(model="gpt-x"))
    assert result.provider == "openai" and result.parsed.verdicts[1].relevance == 12

    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    failing = _StubOpenAI(openai.APIConnectionError(request=request))
    with pytest.raises(ProviderError, match="connection"):
        await OpenAIProvider("sk-openai", client=failing).complete(  # type: ignore[arg-type]
            relevance_request(model="gpt-x")
        )


def test_redact_strips_the_secret() -> None:
    assert redact("key sk-1 leaked", "sk-1") == "key *** leaked"
    assert redact("nothing", None) == "nothing"


# ---------------------------------------------------------------------------
# routing and keys
# ---------------------------------------------------------------------------


def llm_settings(**overrides: Any) -> LLMSettings:
    raw: dict[str, Any] = {
        "provider": "anthropic",
        "tasks": {
            "relevance": {"model": "claude-sonnet-5", "max_tokens": 4000, "batch_size": 20},
            "analysis": {"model": "claude-sonnet-5", "max_tokens": 2000},
        },
    }
    raw.update(overrides)
    return LLMSettings.model_validate(raw)


def test_task_routing_uses_the_global_default_and_per_task_overrides() -> None:
    settings = llm_settings()
    settings.tasks["analysis"].provider = "openai"
    settings.tasks["analysis"].model = "gpt-x"

    relevance = resolve_task(settings, "relevance")
    analysis = resolve_task(settings, "analysis")

    assert (relevance.provider, relevance.model, relevance.batch_size) == (
        "anthropic",
        "claude-sonnet-5",
        20,
    )
    assert (analysis.provider, analysis.model, analysis.batch_size) == ("openai", "gpt-x", 1)


def test_unknown_provider_and_unconfigured_task_are_config_errors() -> None:
    with pytest.raises(ProviderConfigError, match="unknown provider 'azure'"):
        resolve_task(llm_settings(provider="azure"), "relevance")
    with pytest.raises(ProviderConfigError, match=r"llm\.tasks\.weekly"):
        resolve_task(llm_settings(), "weekly")


def test_missing_key_is_reported_by_environment_variable_name() -> None:
    settings = llm_settings()
    with pytest.raises(ProviderConfigError) as excinfo:
        ProviderPool.for_tasks(settings, LLMSecrets())
    assert "ANTHROPIC_API_KEY" in str(excinfo.value)
    assert "OPENAI_API_KEY" not in str(excinfo.value)  # only the selected provider's key

    settings.tasks["analysis"].provider = "openai"
    with pytest.raises(ProviderConfigError) as excinfo:
        ProviderPool.for_tasks(settings, LLMSecrets(anthropic_api_key="a"))
    assert "OPENAI_API_KEY" in str(excinfo.value) and "analysis" in str(excinfo.value)


def test_pool_builds_the_adapter_the_config_selects() -> None:
    settings = llm_settings(provider="openai")
    pool = ProviderPool.for_tasks(settings, LLMSecrets(openai_api_key="sk-o"))
    provider, route = pool.for_task("relevance")
    assert isinstance(provider, OpenAIProvider) and route.provider == "openai"

    pool = ProviderPool.for_tasks(llm_settings(), LLMSecrets(anthropic_api_key="sk-a"))
    provider, _ = pool.for_task("analysis")
    assert isinstance(provider, AnthropicProvider)
    assert pool.provider("anthropic") is provider  # cached, one client per run


def test_cost_estimate_uses_the_pricing_table() -> None:
    settings = Settings.load().llm
    assert estimate_cost(settings, "claude-sonnet-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert estimate_cost(settings, "gpt-unknown", 10, 10) is None


def test_shipped_prompts_load_with_their_pinned_versions() -> None:
    relevance = load_prompt("relevance")
    analysis = load_prompt("analysis")
    assert relevance.name == "relevance_v1" and analysis.name == "analysis_v1"
    assert "0–100" in relevance.text
    assert "Ukrainian" in analysis.text


def test_shipped_settings_route_every_collect_task() -> None:
    settings = Settings.load().llm
    for task in ("relevance", "analysis"):
        route = resolve_task(settings, task)
        assert route.provider == "anthropic" and route.model.startswith("claude-")
