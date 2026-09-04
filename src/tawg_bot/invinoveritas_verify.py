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
    """Whether the signed proof attached to a VerificationResult passed invinoveritas's own
    /verify-proof check -- a SEPARATE claim from the verdict itself (Pavlo, msg 3823: "the
    external judgment and proof authenticity are different claims"). Never inferred from the
    /review response alone -- always the result of an actual /verify-proof round trip. This is
    a server-confirmed check, not yet a local NIP-01/BIP-340 recomputation by Trusty."""

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
    # Pavlo (damon msg 3830): bool is a subtype of int in Python, so a plain `isinstance(x, int
    # | float)` silently accepts a JSON `true`/`false` as a "valid" confidence and coerces it to
    # 1.0/0.0 -- a real, wrong-typed field masquerading as a legitimate value. Reject bool
    # explicitly before the int check.
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
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
    """Confirm `result`'s signed proof is authentic AND covers `artifact` exactly, via
    invinoveritas's OWN /verify-proof check -- server-confirmed (invinoveritas checking its own
    signature), not yet Trusty independently recomputing the NIP-01/BIP-340 signature locally.
    This is the boundary Pavlo named (damon group, msg 3823): "the external judgment and proof
    authenticity are different claims." Never trusts /review's own verdict/signature claims at
    face value; calls the free, no-auth /verify-proof endpoint (schnorr signature +
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
    """The full flow Pavlo named (msg 3823): /review -> confirm the returned proof via
    invinoveritas's own /verify-proof check. Returns (result, proof_status) as a pair rather
    than folding them into one object -- they ARE separate claims, and a caller
    (bot_router.py's eventual VERIFICATION route handler) should have to look at proof_status
    explicitly rather than assume a VerificationResult is trustworthy just because it exists.

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


def _markdown_escape(value: str) -> str:
    """Escape Markdown-special characters so supplied text renders literally.

    Mirrors the Rich Markdown escaping convention already used for GitHub announcement labels,
    so a claim containing `*`, `_`, `[`, backticks, etc. cannot break out of the fixed reply
    layout or be misread as formatting.
    """
    escaped = value.replace("\\", "\\\\")
    for character in "[]()_*`~<>&":
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _proof_event(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return the signed proof event from a raw /review payload, or None when absent."""
    proof = (raw or {}).get("proof")
    event = proof.get("event") if isinstance(proof, dict) else None
    return event if isinstance(event, dict) else None


def format_verification_reply(
    result: VerificationResult,
    proof_status: ProofStatus,
    *,
    artifact: str,
    proof_filename: str | None = None,
) -> str:
    """Render a (VerificationResult, ProofStatus) pair as a fixed Rich Markdown reply.

    Follow-up (Fede): the reply should explicitly state WHAT was verified, WHAT the proof
    artifact is, and HOW to use that proof object to call the endpoint for independent
    validation -- not merely relay the verdict plus a bare link. The artifact is therefore now
    a required argument, echoed back (escaped) so the reader sees the exact claim that was
    checked, alongside the verdict, proof identity, and the exact /verify-proof request shape.

    FAIL CLOSED (Pavlo, msg 3823, verbatim): "A failed or unresolvable proof should fail
    closed rather than leave Trusty repeating an unverifiable verdict." When
    proof_status.verified is False, the verdict/summary/confidence/decision_ref are NOT
    surfaced at all -- only the fact that verification failed, why, and which claim could not
    be confirmed.

    HONEST WORDING (Pavlo, msg 3830): the prior wording said "independently confirmed" / "no
    trust required," but confirm_proof() calls invinoveritas's OWN /verify-proof endpoint --
    that is invinoveritas checking its own signature server-side, not Trusty recomputing the
    NIP-01 event id / BIP-340 schnorr signature locally against the pinned pubkey with no
    network round-trip back to invinoveritas. This project has no crypto dependency today, so
    real local recomputation is a genuine future step, not something to claim now. The reply
    below keeps that caveat rather than overclaiming a trust boundary this code does not yet
    cross.
    """
    claim = _markdown_escape(artifact)
    artifact_hash = hashlib.sha256(artifact.encode("utf-8")).hexdigest()

    if not proof_status.verified:
        lines = [
            "🔒 **Verification withheld**",
            "",
            "The signed proof did not pass invinoveritas's own /verify-proof check, so the "
            "verdict was not relayed.",
        ]
        if proof_status.error:
            lines.append(f"reason: {_markdown_escape(proof_status.error)}")
        lines.extend(
            [
                "",
                "**Claim not verified:**",
                f"> {claim}",
            ]
        )
        return "\n".join(lines)

    lines = [
        "🔍 **Verification result**",
        "",
        "**Claim verified:**",
        f"> {claim}",
        "",
        f"**Verdict:** {_markdown_escape(result.verdict)} (confidence {result.confidence:.2f})",
    ]
    if result.summary:
        lines.extend(["", _markdown_escape(result.summary)])
    proof_lines = [
        "",
        "**Proof artifact:**",
    ]
    if result.decision_ref:
        proof_lines.append(f"- decision_ref: `{_markdown_escape(result.decision_ref)}`")
    proof_lines.append(f"- artifact sha256: `{artifact_hash}`")
    lines.extend(proof_lines)
    if result.verify_proof_url and proof_filename:
        lines.extend(
            [
                "",
                "**Verify it yourself (free, no auth):**",
                f"Download the attached proof file `{_markdown_escape(proof_filename)}` and "
                "run this command to re-check the signed proof against this exact claim:",
                "",
                "```bash",
                f"curl -sS -X POST '{result.verify_proof_url}' \\",
                "  -H 'Content-Type: application/json' \\",
                f"  -d @{proof_filename}",
                "```",
            ]
        )
    return "\n".join(lines)


def build_verification_proof_attachment(
    result: VerificationResult,
    *,
    artifact: str,
) -> tuple[str, str] | None:
    """Build a self-contained proof file for independent re-verification.

    Returns ``(filename, json_content)``, or ``None`` when the /review response carried no
    signed proof event. The file holds the exact /verify-proof request body (event plus the
    locally computed artifact hash) so a reader can download it and POST it to the endpoint
    without reconstructing anything.
    """
    event = _proof_event(result.raw)
    if event is None:
        return None
    artifact_hash = hashlib.sha256(artifact.encode("utf-8")).hexdigest()
    payload = {
        "expect_artifact_hash": artifact_hash,
        "event": event,
    }
    suffix = (result.decision_ref or artifact_hash).split(":")[-1][:12]
    filename = f"invinoveritas-proof-{suffix}.json"
    return filename, json.dumps(payload, ensure_ascii=False, indent=2)
