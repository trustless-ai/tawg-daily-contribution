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
