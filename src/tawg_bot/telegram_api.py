"""Narrow, token-safe Telegram Bot API client."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, cast

import httpx

from tawg_bot.http import SafeHttpError, SafeJsonHttpClient


class TelegramApiError(RuntimeError):
    """A safe Telegram API failure that never includes the bot token."""


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int
    chat_id: int


class TelegramApi:
    def __init__(self, *, token: str, client: httpx.AsyncClient) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._http = SafeJsonHttpClient(client)

    @classmethod
    def from_env(cls, *, client: httpx.AsyncClient) -> TelegramApi:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            raise TelegramApiError("TELEGRAM_BOT_TOKEN is not configured")
        return cls(token=token, client=client)

    async def get_all_updates(self, offset: int, limit: int = 100) -> list[dict[str, Any]]:
        if offset < 0:
            raise ValueError("Telegram update offset cannot be negative")
        if not 1 <= limit <= 100:
            raise ValueError("Telegram update limit must be between 1 and 100")
        updates: list[dict[str, Any]] = []
        next_offset = offset
        while True:
            page = await self._call(
                "getUpdates",
                {"offset": next_offset, "limit": limit, "timeout": 0},
            )
            if not page:
                return updates
            page_ids = [self._update_id(item) for item in page]
            advanced_offset = max(page_ids) + 1
            if advanced_offset <= next_offset:
                raise TelegramApiError("Telegram update pagination did not advance")
            updates.extend(page)
            next_offset = advanced_offset

    async def send_message(
        self, chat_id: int, text: str, reply_to_message_id: int | None = None
    ) -> SentMessage:
        data: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_to_message_id is not None:
            data["reply_parameters"] = f'{{"message_id":{reply_to_message_id}}}'
        result = await self._call("sendMessage", data)
        if len(result) != 1:
            raise TelegramApiError("Telegram sendMessage returned an invalid result")
        message = result[0]
        message_id = message.get("message_id")
        chat = message.get("chat")
        returned_chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(message_id, int) or not isinstance(returned_chat_id, int):
            raise TelegramApiError("Telegram sendMessage returned an invalid message")
        return SentMessage(message_id=message_id, chat_id=returned_chat_id)

    async def _call(self, method: str, data: dict[str, object]) -> list[dict[str, Any]]:
        try:
            payload = await self._http.post_form(f"{self._base_url}/{method}", data)
        except SafeHttpError as error:
            raise TelegramApiError(str(error)) from None
        if payload.get("ok") is not True or not isinstance(payload.get("result"), list | dict):
            raise TelegramApiError(f"Telegram {method} returned an invalid response")
        result = payload["result"]
        if isinstance(result, dict):
            return [result]
        if not all(isinstance(item, dict) for item in result):
            raise TelegramApiError(f"Telegram {method} returned invalid items")
        return cast(list[dict[str, Any]], result)

    @staticmethod
    def _update_id(update: dict[str, Any]) -> int:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise TelegramApiError("Telegram update has no integer update_id")
        return update_id
