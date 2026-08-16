"""Telegram delivery.

Sending is all-or-nothing per digest: if message 2 of 3 fails, the whole send is
treated as failed so the catch-up run resends the complete digest rather than leaving
the reader with a truncated one.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import httpx

API_ROOT = "https://api.telegram.org"
MAX_SEND_ATTEMPTS = 3


class TelegramError(Exception):
    pass


class TelegramClient:
    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        *,
        timeout: float = 20.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> TelegramClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self._timeout))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("TelegramClient must be used as an async context manager")
        return self._client

    async def send_messages(self, messages: list[str]) -> list[int]:
        """Send messages in order. Returns the Telegram message ids."""
        message_ids: list[int] = []
        for text in messages:
            message_ids.append(await self._send_one(text))
        return message_ids

    async def _send_one(self, text: str) -> int:
        url = f"{API_ROOT}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        }

        last_error: str = "unknown error"
        for attempt in range(MAX_SEND_ATTEMPTS):
            try:
                response = await self.client.post(url, json=payload)
            except (httpx.HTTPError, OSError) as exc:
                last_error = f"transport error: {exc}"
            else:
                if response.status_code == 200:
                    body = response.json()
                    if body.get("ok"):
                        return int(body["result"]["message_id"])
                    last_error = f"Telegram rejected the message: {body.get('description')}"
                    # A rejected message will be rejected again; do not burn retries.
                    break

                if response.status_code == 429:
                    retry_after = 1.0
                    with suppress(ValueError, KeyError, TypeError):
                        retry_after = float(
                            response.json().get("parameters", {}).get("retry_after", 1)
                        )
                    await asyncio.sleep(min(retry_after, 30.0))
                    last_error = "rate limited"
                    continue

                last_error = f"HTTP {response.status_code}: {response.text[:200]}"

            if attempt < MAX_SEND_ATTEMPTS - 1:
                await asyncio.sleep(2.0 * (attempt + 1))

        raise TelegramError(last_error)
