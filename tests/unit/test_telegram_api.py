import json
from urllib.parse import parse_qs

import httpx
import pytest

from tawg_bot.telegram_api import TelegramApi, TelegramApiError


@pytest.mark.asyncio
async def test_get_all_updates_pages_until_empty_without_leaking_token() -> None:
    offsets: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        offset = int(form["offset"][0])
        offsets.append(offset)
        result = [{"update_id": 10}, {"update_id": 11}] if offset == 10 else []
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:super-secret-token-value-abcdefgh", client=client)
        updates = await api.get_all_updates(10)

    assert [item["update_id"] for item in updates] == [10, 11]
    assert offsets == [10, 12]


@pytest.mark.asyncio
async def test_api_errors_are_token_safe() -> None:
    token = "123456:super-secret-token-value-abcdefgh"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="failed")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token=token, client=client)
        with pytest.raises(TelegramApiError) as captured:
            await api.get_all_updates(0)

    assert token not in str(captured.value)


@pytest.mark.asyncio
async def test_send_message_targets_the_forum_thread_and_replies_to_the_trigger() -> None:
    captured: dict[str, list[str]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 99, "chat": {"id": -10077}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        await api.send_message(
            -10077,
            "Threaded reply",
            reply_to_message_id=12,
            message_thread_id=700,
        )

    assert captured["message_thread_id"] == ["700"]
    assert json.loads(captured["reply_parameters"][0]) == {"message_id": 12}
