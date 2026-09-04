"""Narrow, token-safe Telegram Bot API client."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from html import escape
from typing import Any, Literal, cast

import httpx

_LEGACY_TEXT_LIMIT = 4096


class TelegramApiError(RuntimeError):
    """A safe Telegram API failure that never includes the bot token."""

    ambiguous = False


class TelegramApiAmbiguousError(TelegramApiError):
    """A Telegram failure where the server may have accepted the request."""

    ambiguous = True


class TelegramRichMessageRejected(TelegramApiError):
    """An explicit pre-acceptance rejection eligible for rendered fallback."""

    def __init__(
        self, message: str, *, reason: Literal["parse", "unsupported"]
    ) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SentMessage:
    message_id: int
    chat_id: int
    delivery_format: str = "rich_markdown_v1"


class TelegramApi:
    def __init__(self, *, token: str, client: httpx.AsyncClient) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._client = client

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
        self,
        chat_id: int,
        text: str,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SentMessage:
        data: dict[str, object] = {
            "chat_id": chat_id,
            "rich_message": json.dumps(
                {"markdown": text}, ensure_ascii=False, separators=(",", ":")
            ),
        }
        self._add_reply_context(data, reply_to_message_id, message_thread_id)
        try:
            result = await self._call("sendRichMessage", data)
            delivery_format = "rich_markdown_v1"
            method = "sendRichMessage"
        except TelegramRichMessageRejected as error:
            if error.reason == "parse":
                data = {
                    "chat_id": chat_id,
                    "rich_message": json.dumps(
                        {"html": escape(text, quote=False)},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
                self._add_reply_context(data, reply_to_message_id, message_thread_id)
                result = await self._call("sendRichMessage", data)
                delivery_format = "rich_html_fallback_v1"
                method = "sendRichMessage"
            else:
                if len(text) > _LEGACY_TEXT_LIMIT:
                    raise TelegramApiError(
                        "Telegram Rich Messages are unavailable for oversized content"
                    ) from None
                data = {"chat_id": chat_id, "text": text}
                self._add_reply_context(data, reply_to_message_id, message_thread_id)
                result = await self._call("sendMessage", data)
                delivery_format = "plain_text_fallback_v1"
                method = "sendMessage"
        return self._sent_message(result, method=method, delivery_format=delivery_format)

    async def send_document(
        self,
        chat_id: int,
        *,
        filename: str,
        document: bytes,
        caption: str | None = None,
        reply_to_message_id: int | None = None,
        message_thread_id: int | None = None,
    ) -> SentMessage:
        data: dict[str, object] = {"chat_id": chat_id}
        if caption is not None:
            data["caption"] = caption
        self._add_reply_context(data, reply_to_message_id, message_thread_id)
        result = await self._call(
            "sendDocument",
            data,
            files={"document": (filename, document, "application/json")},
        )
        return self._sent_message(
            result,
            method="sendDocument",
            delivery_format="document_v1",
        )

    @staticmethod
    def _add_reply_context(
        data: dict[str, object],
        reply_to_message_id: int | None,
        message_thread_id: int | None,
    ) -> None:
        if message_thread_id is not None:
            data["message_thread_id"] = message_thread_id
        if reply_to_message_id is not None:
            data["reply_parameters"] = json.dumps(
                {"message_id": reply_to_message_id}, separators=(",", ":")
            )

    @staticmethod
    def _sent_message(
        result: list[dict[str, Any]], *, method: str, delivery_format: str
    ) -> SentMessage:
        if len(result) != 1:
            raise TelegramApiAmbiguousError(f"Telegram {method} returned an invalid result")
        message = result[0]
        message_id = message.get("message_id")
        chat = message.get("chat")
        returned_chat_id = chat.get("id") if isinstance(chat, dict) else None
        if not isinstance(message_id, int) or not isinstance(returned_chat_id, int):
            raise TelegramApiAmbiguousError(f"Telegram {method} returned an invalid message")
        return SentMessage(
            message_id=message_id,
            chat_id=returned_chat_id,
            delivery_format=delivery_format,
        )

    async def _call(
        self,
        method: str,
        data: dict[str, object],
        *,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            response = await self._client.post(
                f"{self._base_url}/{method}",
                data=data,
                files=files,
            )
        except httpx.HTTPError:
            raise TelegramApiAmbiguousError("Telegram request outcome is unknown") from None
        try:
            payload = response.json()
        except ValueError:
            if response.is_success or response.status_code >= 500:
                raise TelegramApiAmbiguousError(
                    f"Telegram {method} returned an invalid response"
                ) from None
            raise TelegramApiError(f"Telegram {method} rejected the request") from None
        if not isinstance(payload, dict):
            if response.is_success:
                raise TelegramApiAmbiguousError(
                    f"Telegram {method} returned an invalid response"
                )
            raise TelegramApiError(f"Telegram {method} rejected the request")
        if not response.is_success or payload.get("ok") is not True:
            fallback_reason = self._fallback_reason(response, payload)
            if method == "sendRichMessage" and fallback_reason is not None:
                raise TelegramRichMessageRejected(
                    "Telegram rejected Rich Markdown",
                    reason=fallback_reason,
                )
            if response.status_code >= 500:
                raise TelegramApiAmbiguousError("Telegram request outcome is unknown")
            raise TelegramApiError(f"Telegram {method} rejected the request")
        if not isinstance(payload.get("result"), list | dict):
            raise TelegramApiAmbiguousError(f"Telegram {method} returned an invalid response")
        result = payload["result"]
        if isinstance(result, dict):
            return [result]
        if not all(isinstance(item, dict) for item in result):
            raise TelegramApiAmbiguousError(f"Telegram {method} returned invalid items")
        return cast(list[dict[str, Any]], result)

    @staticmethod
    def _fallback_reason(
        response: httpx.Response, payload: dict[str, Any]
    ) -> Literal["parse", "unsupported"] | None:
        if response.status_code == 404:
            return "unsupported"
        if response.status_code != 400:
            return None
        description = payload.get("description")
        if not isinstance(description, str):
            return None
        normalized = description.casefold()
        if "can't parse" in normalized:
            return "parse"
        if (
            "rich message" in normalized
            and ("not supported" in normalized or "unsupported" in normalized)
        ):
            return "unsupported"
        return None

    @staticmethod
    def _update_id(update: dict[str, Any]) -> int:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            raise TelegramApiError("Telegram update has no integer update_id")
        return update_id
