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
async def test_send_message_uses_rich_markdown_and_preserves_reply_context() -> None:
    captured: dict[str, list[str]] = {}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
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

    assert paths == ["/bot123456:token-value-abcdefgh/sendRichMessage"]
    assert json.loads(captured["rich_message"][0]) == {
        "markdown": "Threaded reply",
    }
    assert "text" not in captured
    assert captured["message_thread_id"] == ["700"]
    assert json.loads(captured["reply_parameters"][0]) == {"message_id": 12}


@pytest.mark.asyncio
async def test_send_message_falls_back_only_after_explicit_rich_markdown_rejection() -> None:
    paths: list[str] = []
    forms: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        forms.append(parse_qs(request.content.decode()))
        if len(paths) == 1:
            return httpx.Response(
                400,
                json={
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: can't parse rich message markdown",
                },
            )
        return httpx.Response(
            200,
            json={"ok": True, "result": {"message_id": 101, "chat": {"id": -10077}}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        sent = await api.send_message(
            -10077,
            "Broken <tag> & **markdown",
            reply_to_message_id=12,
            message_thread_id=700,
        )

    assert paths == [
        "/bot123456:token-value-abcdefgh/sendRichMessage",
        "/bot123456:token-value-abcdefgh/sendRichMessage",
    ]
    assert json.loads(forms[1]["rich_message"][0]) == {
        "html": "Broken &lt;tag&gt; &amp; **markdown",
    }
    assert forms[1]["message_thread_id"] == ["700"]
    assert json.loads(forms[1]["reply_parameters"][0]) == {"message_id": 12}
    assert sent.delivery_format == "rich_html_fallback_v1"


@pytest.mark.asyncio
async def test_send_message_does_not_fallback_after_an_unrelated_bad_request() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: chat not found",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        with pytest.raises(TelegramApiError):
            await api.send_message(-10077, "Hello")

    assert paths == ["/bot123456:token-value-abcdefgh/sendRichMessage"]


@pytest.mark.asyncio
async def test_transport_error_is_reported_as_an_ambiguous_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("response lost", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        with pytest.raises(TelegramApiError) as captured:
            await api.send_message(-10077, "Hello")

    assert captured.value.ambiguous is True


@pytest.mark.asyncio
async def test_server_error_never_triggers_rich_message_fallback() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            500,
            json={
                "ok": False,
                "error_code": 500,
                "description": "can't parse rich message markdown",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        with pytest.raises(TelegramApiError) as captured:
            await api.send_message(-10077, "Hello")

    assert captured.value.ambiguous is True
    assert paths == ["/bot123456:token-value-abcdefgh/sendRichMessage"]


@pytest.mark.asyncio
async def test_missing_rich_endpoint_does_not_fallback_long_content_to_send_message() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            404,
            json={"ok": False, "error_code": 404, "description": "Not Found"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        api = TelegramApi(token="123456:token-value-abcdefgh", client=client)
        with pytest.raises(TelegramApiError):
            await api.send_message(-10077, "x" * 4097)

    assert paths == ["/bot123456:token-value-abcdefgh/sendRichMessage"]
