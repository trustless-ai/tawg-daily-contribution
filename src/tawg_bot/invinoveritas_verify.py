"""Client for invinoveritas's /review verification endpoint, plus independent proof
authenticity confirmation via /verify-proof.

Built 2026-08-31 in response to Jimmy Shi's invitation (Telegram, damon group, msg 3817):
"give it some data to verify correctness, then have it call Fede's verify service... PRs
welcome if you're interested." This module is the standalone, independently-testable piece
of that -- it only knows how to call invinoveritas and parse the result. It deliberately does
NOT decide when Trusty should invoke it (that's a routing/permission question for
bot_router.py and prompts/route-system.md, discussed in the PR description rather than
guessed at here).

EXTENDED same day (Telegram, damon group, msg 3823, Pavlo): "Trusty should not merely relay
/review and point at /verify-proof; it should independently verify the returned signed proof
first. The external judgment and proof authenticity are different claims." His proposed flow,
built here exactly as named: explicit verification request -> /review -> verify returned
proof via /verify-proof -> reply with external verdict + separate proof status. "A failed or
unresolvable proof should fail closed rather than leave Trusty repeating an unverifiable
verdict." -- see confirm_proof()/verify_and_confirm() and format_verification_reply()'s
fail-closed branch below.

invinoveritas's own thesis (api.babyblueviper.com): a pre-action verdict a caller can attach
to an artifact, plus a free, no-auth /verify-proof endpoint any THIRD party can use to confirm
the verdict is genuine without trusting the presenter or invinoveritas itself -- the same
"attach a proof, demand a proof" handshake this module exists to bring into Trusty.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from tawg_bot.http import SafeHttpError, SafeJsonHttpClient

DEFAULT_REVIEW_URL = "https://api.babyblueviper.com/review"
DEFAULT_VERIFY_PROOF_URL = "https://api.babyblueviper.com/verify-proof"

# Pavlo (damon group, msg 3825): the exact pinned contract of load-bearing /verify-proof checks
# confirm_proof() requires -- every one of these must be present in the response AND `is True`,
# independent of what the top-level `valid` field says. artifact_hash_matches is included because
# confirm_proof() always sends expect_artifact_hash (see below); a caller that changes that must
# also update this tuple.
_REQUIRED_VERIFY_PROOF_CHECKS: tuple[str, ...] = (
    "signature_valid",
    "id_integrity",
    "issued_by_invinoveritas",
    "artifact_hash_matches",
)


class VerificationRejected(ValueError):
    """A verification request could not be completed or the response was untrustworthy."""


class VerificationResult:
    """A parsed, minimal view of an invinoveritas /review response -- only the fields a
    Telegram reply needs, not the full raw payload (callers who want more can use `raw`)."""

    __slots__ = (
        "confidence",
        "decision_ref",
        "raw",
        "summary",
        "verdict",
        "verify_proof_url",
    )

    def __init__(
        self,
        *,
        verdict: str,
        confidence: float,
        summary: str,
        verify_proof_url: str | None,
        decision_ref: str | None,
        raw: dict[str, Any],
    ) -> None:
        self.verdict = verdict
        self.confidence = confidence
        self.summary = summary
        self.verify_proof_url = verify_proof_url
        self.decision_ref = decision_ref
        self.raw = raw


class ProofStatus:
    """Whether the signed proof attached to a VerificationResult is genuinely, independently
    verifiable -- a SEPARATE claim from the verdict itself (Pavlo, msg 3823: "the external
    judgment and proof authenticity are different claims"). Never inferred from the /review
    response alone -- always the result of an actual /verify-proof round trip."""

    __slots__ = ("checks", "error", "verified")

    def __init__(self, *, verified: bool, checks: dict[str, Any], error: str | None = None) -> None:
        self.verified = verified
        self.checks = checks
        self.error = error


def _extract_decision_ref(payload: dict[str, Any]) -> str | None:
    proof = payload.get("proof")
    if not isinstance(proof, dict):
        return None
    event = proof.get("event")
    if not isinstance(event, dict):
        return None
    content = event.get("content")
    if not isinstance(content, str):
        return None
    # decision_ref lives inside the signed event's JSON content, not top-level -- avoid a
    # second network round trip just to surface it in the reply, since it is already
    # present in the response we already have.
    try:
        inner = json.loads(content)
    except ValueError:
        return None
    ref = inner.get("decision_ref")
    return ref if isinstance(ref, str) else None


async def verify_artifact(
    client: SafeJsonHttpClient,
    *,
    artifact: str,
    artifact_type: str = "general",
    api_key: str | None = None,
    review_url: str = DEFAULT_REVIEW_URL,
    verify_proof_url: str = DEFAULT_VERIFY_PROOF_URL,
) -> VerificationResult:
    """Call invinoveritas /review with sign=true and return a parsed result.

    `api_key` is optional (a Bearer token from POST /register) -- REAL GAP FOUND WHILE
    SMOKE-TESTING THIS AGAINST THE LIVE API, not assumed: an anonymous call genuinely 402s
    once free calls run out, confirmed live (not guessed) before adding this parameter.
    Without a registered key, this only works for a handful of free calls per caller --
    fine for a demo, not for sustained production use. Whether Trusty gets its own
    registered key (and who funds any paid usage past the free tier) is a real question
    for the PR discussion, not decided here.

    Raises VerificationRejected (never httpx exceptions directly, matching the
    SafeJsonHttpClient/SafeHttpError convention already used by
    github_announcements.py/evidence_fetch.py) if the call fails, the response is
    malformed, or required fields are missing. Never silently returns a partial or
    guessed verdict -- an untrustworthy response is a rejection, not an approximation.

    IMPORTANT: this function alone establishes what invinoveritas SAID, not that the reply
    it gave us is genuinely, verifiably from invinoveritas. Callers who will surface the
    verdict to a human (rather than just inspecting it themselves) should call
    verify_and_confirm() instead, or call confirm_proof() on this result before relaying
    anything -- see that function's docstring for why this distinction is load-bearing.
    """
    if not artifact.strip():
        raise VerificationRejected("artifact must be non-empty")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    try:
        payload = await client.post_json(
            review_url,
            {"artifact": artifact, "artifact_type": artifact_type, "sign": True},
            headers=headers,
        )
    except SafeHttpError as exc:
        raise VerificationRejected(f"invinoveritas request failed: {exc}") from None

    verdict = payload.get("verdict")
    confidence = payload.get("confidence")
    summary = payload.get("summary")
    if not isinstance(verdict, str) or not verdict:
        raise VerificationRejected("invinoveritas response missing verdict")
    if not isinstance(confidence, int | float):
        raise VerificationRejected("invinoveritas response missing confidence")
    if not isinstance(summary, str):
        summary = ""

    return VerificationResult(
        verdict=verdict,
        confidence=float(confidence),
        summary=summary,
        verify_proof_url=verify_proof_url,
        decision_ref=_extract_decision_ref(payload),
        raw=payload,
    )


async def confirm_proof(
    client: SafeJsonHttpClient,
    result: VerificationResult,
    *,
    artifact: str,
    verify_proof_url: str = DEFAULT_VERIFY_PROOF_URL,
) -> ProofStatus:
    """Independently confirm `result`'s signed proof is authentic AND covers `artifact`
    exactly -- the boundary Pavlo named (damon group, msg 3823): "the external judgment and
    proof authenticity are different claims." Never trusts /review's own verdict/signature
    claims at face value; calls the free, no-auth /verify-proof endpoint (schnorr signature +
    published-pubkey pin -- the same NIP-01 check any third party could run themselves) and
    additionally asserts `expect_artifact_hash` (a locally-computed sha256 of `artifact`, not
    taken from the /review response) so a proof that doesn't actually cover the artifact we
    submitted flips `valid` to False server-side, not just something we'd have to notice
    ourselves.

    FAILS CLOSED (verified=False), never assumes-valid, on: no proof/event in the /review
    response at all, an unreachable /verify-proof endpoint, a genuine valid=False from the
    verify call, OR (msg 3825, same day) any of _REQUIRED_VERIFY_PROOF_CHECKS missing or not
    exactly `True` in the response's `checks` object -- the top-level `valid` field is NOT
    trusted alone; every load-bearing check is independently pinned and required, so a
    malformed or incomplete response fails closed here even if `valid` itself claims true.
    `error` always carries a real, specific reason -- "unresolvable" per Pavlo's framing is not
    a separate silent-pass state, it is verified=False with an explanatory error, same as any
    other failure here.
    """
    proof = (result.raw or {}).get("proof")
    event = proof.get("event") if isinstance(proof, dict) else None
    if not isinstance(event, dict):
        return ProofStatus(
            verified=False,
            checks={},
            error="no signed proof event present in the /review response -- nothing to verify",
        )
    artifact_hash = hashlib.sha256(artifact.encode("utf-8")).hexdigest()
    try:
        payload = await client.post_json(
            verify_proof_url,
            {"event": event, "expect_artifact_hash": artifact_hash},
        )
    except SafeHttpError as exc:
        return ProofStatus(verified=False, checks={}, error=f"verify-proof request failed: {exc}")

    checks = payload.get("checks")
    if not isinstance(checks, dict):
        checks = {}
    # Pavlo (damon group, msg 3825, same day): "Do not accept top-level valid: true as
    # sufficient by itself. Pin the exact required /verify-proof check contract and require
    # every load-bearing check to be present and exactly true ... Missing or contradictory
    # checks must fail closed even when the server reports valid: true." Implemented exactly:
    # independently require every named check in _REQUIRED_VERIFY_PROOF_CHECKS to be present
    # AND `is True` (not just truthy, not just absent-and-assumed-fine) -- a malformed or
    # incomplete response fails closed here even if `valid` itself says true. artifact_hash_matches
    # is required because this function always sends expect_artifact_hash above; a caller that
    # never asks for that check should not get an unearned pass on it.
    missing_or_failed = [
        name for name in _REQUIRED_VERIFY_PROOF_CHECKS if checks.get(name) is not True
    ]
    if payload.get("valid") is not True or missing_or_failed:
        error = payload.get("error")
        if not error and missing_or_failed:
            error = f"required check(s) not confirmed true: {', '.join(missing_or_failed)}"
        return ProofStatus(
            verified=False,
            checks=checks,
            error=error or "proof did not independently verify (valid=false)",
        )
    return ProofStatus(verified=True, checks=checks, error=None)


async def verify_and_confirm(
    client: SafeJsonHttpClient,
    *,
    artifact: str,
    artifact_type: str = "general",
    api_key: str | None = None,
    review_url: str = DEFAULT_REVIEW_URL,
    verify_proof_url: str = DEFAULT_VERIFY_PROOF_URL,
) -> tuple[VerificationResult, ProofStatus]:
    """The full flow Pavlo named (msg 3823): /review -> independently verify the returned
    proof via /verify-proof. Returns (result, proof_status) as a pair rather than folding
    them into one object -- they ARE separate claims, and a caller (bot_router.py's eventual
    VERIFICATION route handler) should have to look at proof_status explicitly rather than
    assume a VerificationResult is trustworthy just because it exists.

    Raises VerificationRejected only for the /review leg itself (matching verify_artifact's
    existing contract -- a failed /review call has nothing to report). A failed proof
    CONFIRMATION is not raised, it is returned as proof_status.verified=False: that is real,
    reportable information (see format_verification_reply's fail-closed branch), not an
    error condition to propagate as an exception.
    """
    result = await verify_artifact(
        client,
        artifact=artifact,
        artifact_type=artifact_type,
        api_key=api_key,
        review_url=review_url,
        verify_proof_url=verify_proof_url,
    )
    proof_status = await confirm_proof(
        client, result, artifact=artifact, verify_proof_url=verify_proof_url
    )
    return result, proof_status


def format_verification_reply(result: VerificationResult, proof_status: ProofStatus) -> str:
    """Render a (VerificationResult, ProofStatus) pair as Telegram reply text. Separated from
    verify_and_confirm so the formatting can be unit-tested without a network call, and so
    bot_router.py's eventual integration can reuse this without re-deriving the format.

    FAIL CLOSED (Pavlo, msg 3823, verbatim): "A failed or unresolvable proof should fail
    closed rather than leave Trusty repeating an unverifiable verdict." When
    proof_status.verified is False, the verdict/summary/confidence are NOT surfaced at all --
    only the fact that verification failed and why, so a reader never mistakes an
    unauthenticated /review response for something Trusty is vouching for.
    """
    if not proof_status.verified:
        lines = [
            "invinoveritas verdict withheld -- the signed proof did not independently verify.",
        ]
        if proof_status.error:
            lines.append(f"reason: {proof_status.error}")
        lines.append(
            "This means the verdict cannot be confirmed as genuinely issued by invinoveritas, "
            "so it is not being relayed."
        )
        return "\n".join(lines)

    lines = [
        f"invinoveritas verdict: {result.verdict} (confidence {result.confidence:.2f})",
        "proof authenticity: independently confirmed via /verify-proof (signature + artifact "
        "hash both checked, not just relayed)",
    ]
    if result.summary:
        lines.append(result.summary)
    if result.decision_ref:
        lines.append(f"decision_ref: {result.decision_ref}")
    if result.verify_proof_url:
        lines.append(
            f"Independently checkable yourself, no trust required: {result.verify_proof_url}"
        )
    return "\n".join(lines)
