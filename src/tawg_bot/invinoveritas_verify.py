"""Client for invinoveritas's /review verification endpoint.

Built 2026-08-31 in response to Jimmy Shi's invitation (Telegram, damon group, msg 3817):
"give it some data to verify correctness, then have it call Fede's verify service... PRs
welcome if you're interested." This module is the standalone, independently-testable piece
of that -- it only knows how to call invinoveritas and parse the result. It deliberately does
NOT decide when Trusty should invoke it (that's a routing/permission question for
bot_router.py and prompts/route-system.md, discussed in the PR description rather than
guessed at here).

invinoveritas's own thesis (api.babyblueviper.com): a pre-action verdict a caller can attach
to an artifact, plus a free, no-auth /verify-proof endpoint any THIRD party can use to confirm
the verdict is genuine without trusting the presenter or invinoveritas itself -- the same
"attach a proof, demand a proof" handshake this module exists to bring into Trusty.
"""

from __future__ import annotations

import json
from typing import Any

from tawg_bot.http import SafeHttpError, SafeJsonHttpClient

DEFAULT_REVIEW_URL = "https://api.babyblueviper.com/review"


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
    verify_proof_url: str = "https://api.babyblueviper.com/verify-proof",
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


def format_verification_reply(result: VerificationResult) -> str:
    """Render a VerificationResult as Telegram reply text. Separated from verify_artifact
    so the formatting can be unit-tested without a network call, and so bot_router.py's
    eventual integration can reuse this without re-deriving the format."""
    lines = [
        f"invinoveritas verdict: {result.verdict} (confidence {result.confidence:.2f})",
    ]
    if result.summary:
        lines.append(result.summary)
    if result.decision_ref:
        lines.append(f"decision_ref: {result.decision_ref}")
    if result.verify_proof_url:
        lines.append(f"Independently checkable, no trust required: {result.verify_proof_url}")
    return "\n".join(lines)
