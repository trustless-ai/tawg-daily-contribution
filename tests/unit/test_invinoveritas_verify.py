from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tawg_bot.http import SafeJsonHttpClient
from tawg_bot.invinoveritas_verify import (
    VerificationRejected,
    format_verification_reply,
    verify_artifact,
)

REVIEW_URL = "https://api.babyblueviper.com/review"


def _client(handler) -> SafeJsonHttpClient:
    transport = httpx.MockTransport(handler)
    return SafeJsonHttpClient(httpx.AsyncClient(transport=transport))


def _signed_payload(*, verdict: str = "approve", confidence: float = 0.9) -> dict:
    content = json.dumps({"decision_ref": "sha256:deadbeef", "verdict": verdict})
    return {
        "verdict": verdict,
        "confidence": confidence,
        "summary": "clean, no concerns found",
        "proof": {"event": {"content": content}},
    }


def test_genuine_verification_result_parses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == REVIEW_URL
        body = json.loads(request.content)
        assert body == {"artifact": "1+1=2", "artifact_type": "general", "sign": True}
        return httpx.Response(200, json=_signed_payload())

    async def run() -> None:
        result = await verify_artifact(_client(handler), artifact="1+1=2")
        assert result.verdict == "approve"
        assert result.confidence == 0.9
        assert result.decision_ref == "sha256:deadbeef"
        assert result.verify_proof_url == "https://api.babyblueviper.com/verify-proof"

    asyncio.run(run())


def test_empty_artifact_rejected_without_network_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the network for an empty artifact")

    async def run() -> None:
        with pytest.raises(VerificationRejected):
            await verify_artifact(_client(handler), artifact="   ")

    asyncio.run(run())


def test_api_key_sent_as_bearer_header_when_provided() -> None:
    # REAL bug this test exists to catch: a live smoke test against the actual API
    # (during PR development) hit a genuine 402 with no api_key, and separately hit our
    # own platform's registration rate limit trying to get a fresh key for a live
    # success-path check -- this test verifies the header wiring is correct without
    # needing another live call.
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_signed_payload())

    async def run() -> None:
        await verify_artifact(_client(handler), artifact="claim", api_key="ivv_testkey123")

    asyncio.run(run())
    assert captured["auth"] == "Bearer ivv_testkey123"


def test_no_auth_header_sent_when_api_key_omitted() -> None:
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_signed_payload())

    async def run() -> None:
        await verify_artifact(_client(handler), artifact="claim")

    asyncio.run(run())
    assert captured["auth"] is None


def test_missing_verdict_field_is_rejected_not_guessed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"confidence": 0.5})

    async def run() -> None:
        with pytest.raises(VerificationRejected):
            await verify_artifact(_client(handler), artifact="claim")

    asyncio.run(run())


def test_non_2xx_status_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "payment required"})

    async def run() -> None:
        with pytest.raises(VerificationRejected):
            await verify_artifact(_client(handler), artifact="claim")

    asyncio.run(run())


def test_malformed_json_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json")

    async def run() -> None:
        with pytest.raises(VerificationRejected):
            await verify_artifact(_client(handler), artifact="claim")

    asyncio.run(run())


def test_missing_decision_ref_degrades_gracefully_not_rejected() -> None:
    # proof/event shape absent entirely -- a real invinoveritas response always has this,
    # but the client should not hard-fail on a field it only uses for a nicer reply.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"verdict": "reject", "confidence": 0.2, "summary": "unsafe"}
        )

    async def run() -> None:
        result = await verify_artifact(_client(handler), artifact="claim")
        assert result.verdict == "reject"
        assert result.decision_ref is None

    asyncio.run(run())


def test_format_verification_reply_includes_proof_link() -> None:
    result_dict = _signed_payload(verdict="approve_with_concerns", confidence=0.75)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result_dict)

    async def run() -> str:
        result = await verify_artifact(_client(handler), artifact="claim")
        return format_verification_reply(result)

    text = asyncio.run(run())
    assert "approve_with_concerns" in text
    assert "0.75" in text
    assert "decision_ref: sha256:deadbeef" in text
    assert "https://api.babyblueviper.com/verify-proof" in text
