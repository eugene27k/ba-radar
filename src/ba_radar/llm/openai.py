"""OpenAI adapter: the official `openai` SDK, Responses API, structured output via
`text.format` with `strict: true`.

Written from the published documentation on 2026-09-07
(https://developers.openai.com/api/docs/guides/structured-outputs and the
`/v1/responses` reference). **Nothing here has been exercised against the live OpenAI
API yet** — the model name is a config value the user fills in, and `ba-radar
llm-check` is the way to confirm the key, the model and this request shape together.

`temperature` is not sent: reasoning models reject it and the other models default to
a sensible value. `reasoning.effort` is forwarded only when the task configures it.
"""

from __future__ import annotations

from typing import Any

import openai
from openai.types.responses import Response
from pydantic import BaseModel

from ba_radar.llm.base import LLMRequest, LLMResponse, ProviderError, parse_reply, redact


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 120.0,
        client: openai.AsyncOpenAI | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = client or openai.AsyncOpenAI(api_key=api_key, timeout=timeout)

    async def complete[M: BaseModel](self, request: LLMRequest[M]) -> LLMResponse[M]:
        kwargs = build_request(request)
        try:
            response = await self._client.responses.create(**kwargs)
        except openai.APIStatusError as exc:
            raise ProviderError(
                redact(f"openai HTTP {exc.status_code}: {exc.message}", self._api_key)
            ) from exc
        except openai.APITimeoutError as exc:
            raise ProviderError(f"openai request timed out ({request.task})") from exc
        except openai.APIConnectionError as exc:
            raise ProviderError(redact(f"openai connection error: {exc}", self._api_key)) from exc
        return parse_response(response, request)


def build_request[M: BaseModel](request: LLMRequest[M]) -> dict[str, Any]:
    """The keyword arguments for `responses.create`, as one inspectable dict."""
    kwargs: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "input": request.user,
        "max_output_tokens": request.max_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": request.task,
                "schema": request.json_schema,
                "strict": True,
            }
        },
    }
    if request.effort:
        kwargs["reasoning"] = {"effort": request.effort}
    return kwargs


def parse_response[M: BaseModel](response: Response, request: LLMRequest[M]) -> LLMResponse[M]:
    """Turn a raw `Response` into the provider-neutral response."""
    if response.status == "incomplete":
        reason = response.incomplete_details.reason if response.incomplete_details else None
        raise ProviderError(
            f"openai reply incomplete ({reason or 'unknown reason'}, {request.task})"
        )

    texts: list[str] = []
    for item in response.output:
        if item.type != "message":
            continue
        for part in item.content:
            if part.type == "refusal":
                raise ProviderError(f"openai refused the {request.task} request: {part.refusal}")
            if part.type == "output_text":
                texts.append(part.text)
    if not texts:
        raise ProviderError(f"openai reply carried no text output ({request.task})")

    parsed = parse_reply("".join(texts), request.output_model)
    usage = response.usage
    return LLMResponse(
        parsed=parsed,
        input_tokens=usage.input_tokens if usage else 0,
        output_tokens=usage.output_tokens if usage else 0,
        model=response.model,
        provider=OpenAIProvider.name,
    )
