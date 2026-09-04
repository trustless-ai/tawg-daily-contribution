from __future__ import annotations

import asyncio
import hashlib
import json

import httpx
import pytest

from tawg_bot.http import SafeJsonHttpClient
from tawg_bot.invinoveritas_verify import (
    ProofStatus,
    VerificationRejected,
    build_verification_proof_attachment,
    confirm_proof,
    format_verification_reply,
    verify_and_confirm,
    verify_artifact,
)

REVIEW_URL = "https://api.babyblueviper.com/review"
VERIFY_PROOF_URL = "https://api.babyblueviper.com/verify-proof"


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


def test_boolean_confidence_is_rejected_not_silently_coerced() -> None:
    """Pavlo (damon msg 3830): bool is a subtype of int in Python, so a plain
    isinstance(x, int | float) check silently accepts confidence: true/false as if it were a
    real 1.0/0.0 score -- a wrong-typed field masquerading as valid data. Must be rejected."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"verdict": "approve", "confidence": True, "summary": "ok"})

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


# --- confirm_proof / verify_and_confirm / fail-closed formatting ---------------------------
# Added same day (Telegram, damon group, msg 3823, Pavlo): "Trusty should not merely relay
# /review and point at /verify-proof; it should independently verify the returned signed
# proof first. The external judgment and proof authenticity are different claims." These
# tests cover that new boundary specifically, distinct from the verify_artifact tests above
# which only ever tested the /review leg.


def test_confirm_proof_calls_verify_proof_with_event_and_artifact_hash() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == VERIFY_PROOF_URL
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "valid": True,
                "checks": {
                    "id_integrity": True,
                    "signature_valid": True,
                    "issued_by_invinoveritas": True,
                    "artifact_hash_matches": True,
                },
            },
        )

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="the real artifact")

    status = asyncio.run(run())
    assert status.verified is True
    assert captured["body"]["event"] == {"id": "abc123"}
    assert (
        captured["body"]["expect_artifact_hash"] == hashlib.sha256(b"the real artifact").hexdigest()
    )


def test_confirm_proof_fails_closed_on_valid_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": False,
                "checks": {"signature_valid": False},
                "error": "signature mismatch",
            },
        )

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert status.error == "signature mismatch"


def test_confirm_proof_fails_closed_on_valid_true_with_missing_required_check() -> None:
    """Pavlo (damon group, msg 3825, verbatim): "Do not accept top-level valid: true as
    sufficient by itself. Pin the exact required /verify-proof check contract and require
    every load-bearing check to be present and exactly true ... Missing or contradictory
    checks must fail closed even when the server reports valid: true." A response that
    claims valid: true but omits artifact_hash_matches (the exact class of malformed/buggy
    response this guards against) must still fail closed."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": True,
                "checks": {
                    "signature_valid": True,
                    "id_integrity": True,
                    "issued_by_invinoveritas": True,
                    # artifact_hash_matches deliberately absent.
                },
            },
        )

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert "artifact_hash_matches" in status.error


def test_confirm_proof_fails_closed_on_valid_true_with_signature_valid_false() -> None:
    """Pavlo's own named negative vector (msg 3825, verbatim): "Add one negative vector:
    valid=true + signature_valid=false -> verified=false." Tested by exact field name, not
    just a generic stand-in check, since he asked for this one specifically."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": True,
                "checks": {
                    "signature_valid": False,
                    "id_integrity": True,
                    "issued_by_invinoveritas": True,
                    "artifact_hash_matches": True,
                },
            },
        )

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert "signature_valid" in status.error


def test_confirm_proof_fails_closed_on_valid_true_with_a_false_required_check() -> None:
    """Same guard, a DIFFERENT check false (issued_by_invinoveritas) -- confirms the pinned
    contract is enforced generically across every named field, not just signature_valid."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "valid": True,
                "checks": {
                    "signature_valid": True,
                    "id_integrity": True,
                    "issued_by_invinoveritas": False,
                    "artifact_hash_matches": True,
                },
            },
        )

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert "issued_by_invinoveritas" in status.error


def test_confirm_proof_fails_closed_when_no_proof_event_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call the network when there is no event to verify")

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={},  # no "proof" key at all
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert "no signed proof event" in status.error


def test_confirm_proof_fails_closed_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async def run() -> ProofStatus:
        from tawg_bot.invinoveritas_verify import VerificationResult

        result = VerificationResult(
            verdict="approve",
            confidence=0.9,
            summary="",
            verify_proof_url=VERIFY_PROOF_URL,
            decision_ref=None,
            raw={"proof": {"event": {"id": "abc123"}}},
        )
        return await confirm_proof(_client(handler), result, artifact="claim")

    status = asyncio.run(run())
    assert status.verified is False
    assert "verify-proof request failed" in status.error


def test_verify_and_confirm_calls_both_endpoints_in_order() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url == REVIEW_URL:
            return httpx.Response(200, json=_signed_payload())
        return httpx.Response(
            200,
            json={
                "valid": True,
                "checks": {
                    "signature_valid": True,
                    "id_integrity": True,
                    "issued_by_invinoveritas": True,
                    "artifact_hash_matches": True,
                },
            },
        )

    async def run():
        return await verify_and_confirm(_client(handler), artifact="1+1=2")

    result, status = asyncio.run(run())
    assert calls == [REVIEW_URL, VERIFY_PROOF_URL]
    assert result.verdict == "approve"
    assert status.verified is True


def test_verify_and_confirm_still_raises_on_review_failure_not_swallowed() -> None:
    """A failed /review call has nothing to confirm a proof for -- must still raise, not
    silently degrade to an unverified ProofStatus."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "payment required"})

    async def run():
        return await verify_and_confirm(_client(handler), artifact="claim")

    with pytest.raises(VerificationRejected):
        asyncio.run(run())


def test_format_verification_reply_states_claim_proof_and_independent_check() -> None:
    """Follow-up (Fede): a verified reply must state WHAT was verified, WHAT the proof
    artifact is, and HOW to re-run the check independently -- not just relay the verdict."""
    result_dict = _signed_payload(verdict="approve_with_concerns", confidence=0.75)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result_dict)

    async def run() -> str:
        result = await verify_artifact(_client(handler), artifact="1+1=2")
        status = ProofStatus(verified=True, checks={"signature_valid": True})
        return format_verification_reply(
            result,
            status,
            artifact="1+1=2",
            proof_filename="invinoveritas-proof-abc123.json",
        )

    text = asyncio.run(run())
    artifact_hash = hashlib.sha256(b"1+1=2").hexdigest()
    assert "approve\\_with\\_concerns" in text
    assert "0.75" in text
    assert "**Claim verified:**" in text
    assert "1+1=2" in text
    assert artifact_hash in text
    assert "sha256:deadbeef" in text
    assert "https://api.babyblueviper.com/verify-proof" in text
    assert "invinoveritas-proof-abc123.json" in text
    assert "curl -sS -X POST" in text
    assert "-d @invinoveritas-proof-abc123.json" in text
    assert "**Signed proof event:**" not in text
    assert "Note:" not in text
    assert "not yet independently recomputed" not in text


def test_format_verification_reply_escapes_claim_markdown() -> None:
    """A claim containing Markdown-special characters must render literally, not break the
    fixed reply layout or be reinterpreted as formatting."""
    result_dict = _signed_payload(verdict="approve", confidence=0.9)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result_dict)

    async def run() -> str:
        result = await verify_artifact(_client(handler), artifact="a*b [c]")
        status = ProofStatus(verified=True, checks={"signature_valid": True})
        return format_verification_reply(result, status, artifact="a*b [c]")

    text = asyncio.run(run())
    assert "a\\*b \\[c\\]" in text
    assert "a*b [c]" not in text


def test_build_verification_proof_attachment_bundles_event_and_artifact_hash() -> None:
    """The proof attachment is a self-contained /verify-proof request body, so the reader can
    download one file and POST it without reconstructing anything."""
    result_dict = _signed_payload(verdict="approve", confidence=0.9)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=result_dict)

    async def run():
        result = await verify_artifact(_client(handler), artifact="1+1=2")
        return build_verification_proof_attachment(result, artifact="1+1=2")

    attachment = asyncio.run(run())
    assert attachment is not None
    filename, content = attachment
    assert filename.endswith(".json")
    payload = json.loads(content)
    assert "verify_url" not in payload
    assert payload["expect_artifact_hash"] == hashlib.sha256(b"1+1=2").hexdigest()
    assert payload["event"]["content"] == result_dict["proof"]["event"]["content"]


def test_build_verification_proof_attachment_none_without_event() -> None:
    from tawg_bot.invinoveritas_verify import VerificationResult

    result = VerificationResult(
        verdict="approve",
        confidence=0.9,
        summary="",
        verify_proof_url=REVIEW_URL,
        decision_ref=None,
        raw={},
    )
    assert build_verification_proof_attachment(result, artifact="claim") is None


def test_format_verification_reply_withholds_verdict_when_proof_unverified() -> None:
    """FAIL CLOSED (Pavlo, msg 3823, verbatim): 'A failed or unresolvable proof should fail
    closed rather than leave Trusty repeating an unverifiable verdict.' The verdict/summary/
    decision_ref must NOT appear anywhere in the withheld reply."""
    from tawg_bot.invinoveritas_verify import VerificationResult

    result = VerificationResult(
        verdict="approve",
        confidence=0.9,
        summary="a summary that must not leak",
        verify_proof_url=VERIFY_PROOF_URL,
        decision_ref="sha256:deadbeef",
        raw={},
    )
    status = ProofStatus(verified=False, checks={}, error="signature mismatch")

    text = format_verification_reply(result, status, artifact="the claim")
    assert "withheld" in text
    assert "signature mismatch" in text
    assert "the claim" in text
    assert "approve" not in text
    assert "a summary that must not leak" not in text
    assert "sha256:deadbeef" not in text
