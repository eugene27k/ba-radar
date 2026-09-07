"""Anthropic adapter: the official `anthropic` SDK, structured output via
`output_config.format`.

What is deliberately *not* sent, and why (current API, checked against the
`/claude-api` skill on 2026-09-07):

* `temperature` / `top_p` / `top_k` — rejected with a 400 by the 4.7+ family.
* `thinking` — omitted. Claude Sonnet 5 and Opus 5 run adaptive thinking by default;
  Haiku 4.5 runs without thinking unless given `budget_tokens`, which the newer models
  reject. Leaving the parameter out is the one shape every current model accepts.
  Thinking tokens count against `max_tokens`, which is why the task budgets in
  `settings.yaml` are generous.
* Assistant prefill — rejected; the JSON shape comes from `output_config.format`.

`effort` is forwarded inside `output_config` only when a task configures it; Haiku
4.5 does not accept it, so the setting is left empty for that model.

The SDK's own retries (2, exponential backoff on 408/409/429/5xx) are left at their
defaults; the timeout is explicit because the default is ten minutes, which is the
whole cycle budget.
"""

from __future__ import annotations

from typing import Any

import anthropic
from pydantic import BaseModel

from ba_radar.llm.base import LLMRequest, LLMResponse, ProviderError, parse_reply, redact


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)

    async def complete[M: BaseModel](self, request: LLMRequest[M]) -> LLMResponse[M]:
        kwargs = build_request(request)
        try:
            message = await self._client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            raise ProviderError(
                redact(f"anthropic HTTP {exc.status_code}: {exc.message}", self._api_key)
            ) from exc
        except anthropic.APITimeoutError as exc:
            raise ProviderError(f"anthropic request timed out ({request.task})") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(
                redact(f"anthropic connection error: {exc}", self._api_key)
            ) from exc
        return parse_message(message, request)


def build_request[M: BaseModel](request: LLMRequest[M]) -> dict[str, Any]:
    """The keyword arguments for `messages.create`, as one inspectable dict."""
    output_config: dict[str, Any] = {
        "format": {"type": "json_schema", "schema": request.json_schema}
    }
    if request.effort:
        output_config["effort"] = request.effort
    return {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "messages": [{"role": "user", "content": request.user}],
        "output_config": output_config,
    }


def parse_message[M: BaseModel](
    message: anthropic.types.Message, request: LLMRequest[M]
) -> LLMResponse[M]:
    """Turn a raw `Message` into the provider-neutral response."""
    if message.stop_reason == "refusal":
        raise ProviderError(f"anthropic refused the {request.task} request")
    if message.stop_reason == "max_tokens":
        raise ProviderError(
            f"anthropic reply truncated at max_tokens={request.max_tokens} ({request.task})"
        )
    text = "".join(block.text for block in message.content if block.type == "text")
    if not text:
        raise ProviderError(f"anthropic reply carried no text block ({request.task})")
    parsed = parse_reply(text, request.output_model)
    return LLMResponse(
        parsed=parsed,
        input_tokens=message.usage.input_tokens,
        output_tokens=message.usage.output_tokens,
        model=message.model,
        provider=AnthropicProvider.name,
    )
