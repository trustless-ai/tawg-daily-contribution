# TAWG Live Evidence Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for every implementation task, superpowers:writing-skills for Task 10, and superpowers:verification-before-completion before claiming completion.

**Goal:** Replace committed external full-text mirrors and flat ERC retrieval with controller-owned, allowlisted live-evidence tools that produce current, evidence-classed answers and refresh locally generated knowledge without persisting external response bodies.

**Architecture:** Keep sanitized Telegram history and generated Obsidian knowledge in Git. Route explicit ERC questions through a deterministic planner, curated source registry, restricted transient fetcher, fixed evidence ranker, and exact citation allowlist before the tool-less AI runs. Persist only source observations, gaps, refresh jobs, generated synthesis, and delivery state; collect GitHub and Ethereum Magicians activity transiently for the 23:00 UTC Daily.

**Tech Stack:** Python 3.12, Pydantic 2, httpx, PyYAML, jsonschema, pytest/pytest-asyncio/respx, Ruff, mypy, Forge/Claude Code CLI adapter.

**Spec:** `docs/superpowers/specs/2026-08-23-tawg-live-evidence-tools-design.md`

## Global Constraints

- Work only in the local `feature/tawg-knowledge-bot` worktree until the user separately approves a GitHub write.
- Do not edit, stage, commit, or push the untracked `.github/` directory.
- Do not run compiled repository or virtual-environment binaries directly. Run source through the installed Python runtime and use only tools installed in `/usr/local/bin` when a compiled executable is required.
- External ERC/EIP, GitHub, and Ethereum Magicians bodies may exist only in memory during an operation. They must never be staged below the repository root, included in state files, logged, or committed.
- Unit and integration tests use fixtures or mocked transports; they never require live Internet.
- Only operator-reviewed registry entries can be fetched. A URL suggested in the current Telegram message remains an unfetched candidate.
- The AI remains tool-less, single-turn, schema-bound, and unable to choose URLs or mutate source authority.
- Explicit ERC answers cite only URLs successfully fetched for that operation. A missing normative source forces a partial/not-verified result and a deduplicated gap.
- The Daily window remains exactly `[previous day 23:00 UTC, current day 23:00 UTC)` and is assembled from evidence fetched immediately before generation.

## File Structure

### New production files

- `src/tawg_bot/source_registry.py` — validated registry, source kinds, authority, URL policy, and observations.
- `src/tawg_bot/erc_query.py` — explicit ERC extraction and intent planning.
- `src/tawg_bot/evidence_fetch.py` — HTTPS/DNS/redirect/MIME/size/time confined transient fetching.
- `src/tawg_bot/live_evidence.py` — evidence requirements, resolution, ranking, pack building, and change reporting.
- `src/tawg_bot/knowledge_jobs.py` — deduplicated gaps, refresh jobs, candidates, and deterministic state writes.
- `src/tawg_bot/daily_evidence.py` — in-memory GitHub/Magicians collection for one Daily window.
- `src/tawg_bot/persistence_guard.py` — repository/state/transaction external-body boundary checks.
- `src/tawg_bot/schemas/reply-result.v2.json` — reply evidence status and URL citations.
- `src/tawg_bot/schemas/knowledge-result.v2.json` — live-evidence knowledge transaction contract.
- `knowledge/meta/sources.yml` — reviewed metadata-only source registry.
- `data/state/knowledge-gaps.json` — current deduplicated gaps.
- `data/state/pending-knowledge-refresh.json` — current deduplicated refresh work.
- `data/state/source-candidates.json` — unfetched user-suggested URLs.

### New test files

- `tests/unit/test_source_registry.py`
- `tests/unit/test_erc_query.py`
- `tests/unit/test_evidence_fetch.py`
- `tests/unit/test_live_evidence.py`
- `tests/unit/test_knowledge_jobs.py`
- `tests/unit/test_persistence_guard.py`
- `tests/integration/test_live_erc_replies.py`
- `tests/integration/test_live_knowledge_refresh.py`
- `tests/integration/test_daily_live_evidence.py`
- `tests/e2e/test_live_evidence_boundary.py`
- `tests/eval/test_erc_answer_contract.py`
- `tests/fixtures/live_evidence/` — synthetic specs, repositories, tests, discussions, redirects, and injection text.
- `tests/fixtures/skill/` — baseline and revised-skill scenario results.

### Existing files changed

- `src/tawg_bot/bot_router.py`, `context.py`, `runtime.py`, `scheduler.py`, `daily.py`, `knowledge_refresh.py`, `vault_transaction.py`, `vault.py`, `retrieval.py`, `query.py`, `models.py`, `cli.py`
- `src/tawg_bot/schemas/daily-result.v1.json`
- `bot-skill/SKILL.md`, `prompts/reply-system.md`, `prompts/knowledge-system.md`, `prompts/daily-system.md`
- `config/sources.yml`, `config/bot-policy.yml`
- `knowledge/meta/source-ledger.json`, `knowledge/meta/claim-ledger.json`, generated knowledge pages
- relevant existing unit/integration/e2e tests and operator documentation

### Removed after migration

- `data/github/**`
- `data/magicians/**`
- `data/state/magicians-candidates.json`
- GitHub/Magicians full-text cursor fields and persisted sync paths

---

## Task 1: Add the metadata-only source registry

**Files:**

- Create: `src/tawg_bot/source_registry.py`
- Create: `tests/unit/test_source_registry.py`
- Create: `knowledge/meta/sources.yml`
- Modify: `tests/unit/test_vault_scaffold.py`
- Modify: `tests/unit/test_vault_lint.py`

**Interfaces:**

Define `EvidenceKind` values `normative_spec`, `implementation`, `test`, `example`, and
`discussion`; define `EvidenceAuthority` values `canonical`, `official_org`, `maintainer`,
and `community`. `SourceRegistry` exposes these exact methods:

- `from_yaml(path: Path) -> SourceRegistry`
- `resolve(erc_number: int, kinds: frozenset[EvidenceKind]) -> tuple[RegisteredSource, ...]`
- `source(source_key: str) -> RegisteredSource`
- `render_with_observations(observations: Mapping[str, SourceObservation]) -> str`

### Step 1: Write the failing registry tests

Add tests that load a temporary `tawg.sources.v2` file and assert deterministic resolution order, unique `source_key`, at most one active canonical source per ERC, required canonical sources for ERC-8004 and ERC-8183, exact HTTPS host/path confinement, no credentials/query fragments, and rejection of the illustrative `example.invalid` URL. Other ERCs may deliberately lack a canonical entry so the runtime can surface a normative-evidence gap.

```python
def test_registry_resolves_by_kind_then_authority(tmp_path: Path) -> None:
    path = write_registry_fixture(tmp_path)
    registry = SourceRegistry.from_yaml(path)
    sources = registry.resolve(8004, frozenset(EvidenceKind))
    assert [item.source_key for item in sources] == [
        "erc-8004-canonical",
        "agent-ercs-8004-implementation",
        "agent-ercs-8004-tests",
        "magicians-8004",
    ]

@pytest.mark.parametrize(
    "url",
    [
        "http://eips.ethereum.org/EIPS/eip-8004",
        "https://user:pass@eips.ethereum.org/EIPS/eip-8004",
        "https://example.invalid/current",
        "https://eips.ethereum.org/EIPS/eip-8004#fragment",
    ],
)
def test_registry_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    with pytest.raises(RegistryRejected):
        SourceRegistry.from_yaml(write_registry_fixture(tmp_path, canonical_url=url))
```

Run: `python3.12 -m pytest tests/unit/test_source_registry.py -q`

Expected: FAIL because `tawg_bot.source_registry` does not exist.

### Step 2: Implement the registry models and loader

Use strict Pydantic models for `RegisteredSource`, `FetchPolicy`, and `SourceObservation`. Normalize topics to `erc-N`, parse URLs with `urllib.parse.urlsplit`, require `status in {active,candidate,disabled}`, and sort resolved sources with these immutable ranks:

```python
_KIND_RANK = {
    EvidenceKind.NORMATIVE_SPEC: 0,
    EvidenceKind.IMPLEMENTATION: 1,
    EvidenceKind.TEST: 2,
    EvidenceKind.EXAMPLE: 2,
    EvidenceKind.DISCUSSION: 3,
}
_AUTHORITY_RANK = {
    EvidenceAuthority.CANONICAL: 0,
    EvidenceAuthority.OFFICIAL_ORG: 1,
    EvidenceAuthority.MAINTAINER: 2,
    EvidenceAuthority.COMMUNITY: 3,
}
```

The YAML fetch policy contains `allowed_hosts`, `allowed_path_prefixes`, `max_bytes`, and `mime_types`. `canonical_url` and optional `immutable_url` must satisfy their own policy. The loader must never read or accept a `body`, `text`, `content`, or `excerpt` key.

### Step 3: Add a schema-valid registry scaffold

Create `knowledge/meta/sources.yml` with schema `tawg.sources.v2` and metadata entries for every currently in-scope ERC, required ERC-8004/8183 canonical pages, `trustless-ai/agent-ercs` implementation/test paths, and the already curated Magicians topic URLs. Use exact reviewed URLs; do not copy source text into the file. Task 11 performs the live authority audit before these values are accepted as rollout-ready.

### Step 4: Run focused validation

Run:

```bash
python3.12 -m pytest tests/unit/test_source_registry.py tests/unit/test_vault_scaffold.py tests/unit/test_vault_lint.py -q
python3.12 -m mypy src/tawg_bot/source_registry.py
```

Expected: PASS.

### Step 5: Commit

```bash
git add src/tawg_bot/source_registry.py tests/unit/test_source_registry.py tests/unit/test_vault_scaffold.py tests/unit/test_vault_lint.py knowledge/meta/sources.yml
git commit -m "feat: add metadata-only evidence registry"
```

## Task 2: Add explicit ERC intent planning

**Files:**

- Create: `src/tawg_bot/erc_query.py`
- Create: `tests/unit/test_erc_query.py`
- Modify: `src/tawg_bot/bot_router.py`
- Modify: `tests/unit/test_bot_router.py`

**Interfaces:**

```python
class ErcIntent(StrEnum):
    OVERVIEW = "overview"
    IMPLEMENTATION = "implementation"
    INTERFACES = "interfaces"
    STATE_MACHINE = "state_machine"
    SECURITY = "security"
    STATUS = "status"
    COMPARISON = "comparison"
    DISCUSSION = "discussion"

class ErcQuery(StrictModel):
    erc_numbers: tuple[int, ...]
    intent: ErcIntent
```

`ErcQueryPlanner.plan(text: str) -> ErcQuery | None` is the only public planning method.

### Step 1: Write the failing parser matrix

```python
@pytest.mark.parametrize(
    ("text", "numbers", "intent"),
    [
        ("How is ERC-8004 implemented?", (8004,), ErcIntent.IMPLEMENTATION),
        ("ERC 8183 interfaces", (8183,), ErcIntent.INTERFACES),
        ("compare eip-8004 and ERC-8183", (8004, 8183), ErcIntent.COMPARISON),
        ("ERC-8004 security assumptions", (8004,), ErcIntent.SECURITY),
        ("8004 implementation", (), None),
    ],
)
def test_plan_explicit_erc_questions(
    text: str,
    numbers: tuple[int, ...],
    intent: ErcIntent | None,
) -> None:
    result = ErcQueryPlanner().plan(text)
    assert (() if result is None else result.erc_numbers) == numbers
    assert (None if result is None else result.intent) is intent
```

Also assert a maximum of four distinct ERCs, range `1..99999`, stable numeric ordering by first occurrence, and no accidental match inside URLs/usernames.

Run: `python3.12 -m pytest tests/unit/test_erc_query.py -q`

Expected: FAIL because the planner is absent.

### Step 2: Implement deterministic extraction and intent priority

Use `(?<![A-Za-z0-9])(?:ERC|EIP)[-\s]?(\d{1,5})(?!\d)` and a fixed phrase table. Comparison wins when two ERCs are present or comparison language occurs; otherwise priority is interfaces, state machine, security, implementation, status, discussion, overview. No model call participates in planning.

### Step 3: Make routing expose the planned query

Keep the current permission-first `BotRoute` decision, but add:

```python
def erc_query(self, text: str) -> ErcQuery | None:
    if self.classify(text) is not BotRoute.KNOWLEDGE_QUESTION:
        return None
    return self._erc_planner.plan(text)
```

Forbidden/out-of-scope requests must remain refused even if they contain an ERC number.

### Step 4: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_erc_query.py tests/unit/test_bot_router.py -q
python3.12 -m mypy src/tawg_bot/erc_query.py src/tawg_bot/bot_router.py
```

Expected: PASS.

Commit: `git commit -m "feat: plan explicit ERC evidence queries"`

## Task 3: Build the restricted transient evidence fetcher

**Files:**

- Create: `src/tawg_bot/evidence_fetch.py`
- Create: `tests/unit/test_evidence_fetch.py`
- Create: `tests/fixtures/live_evidence/normative-8004.html`
- Create: `tests/fixtures/live_evidence/injection.md`
- Modify: `src/tawg_bot/http.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class FetchedEvidence:
    source_key: str
    canonical_url: str
    citation_url: str
    media_type: str
    observed_at: datetime
    version: str | None
    content_sha256: str
    text: str
```

`HostResolver.resolve(host: str, port: int)` returns a tuple of parsed IPv4/IPv6 addresses.
`RestrictedEvidenceFetcher.fetch(source: RegisteredSource, *, now: datetime)` returns one
`FetchedEvidence` or raises `EvidenceFetchRejected` with a safe code.

`FetchedEvidence` is a frozen dataclass, not a Pydantic persistence model. State objects receive only `source_key`, URLs, version, hash, size, and timestamps.

### Step 1: Write failing security tests

Cover:

- HTTPS only and exact registry source URL.
- DNS returning private, loopback, link-local, multicast, reserved, or unspecified IP.
- redirect within policy succeeds; cross-host/path redirect is rejected before following.
- maximum three redirects.
- `text/html`, `text/plain`, `text/markdown`, and allowlisted JSON only.
- missing/oversized `Content-Length` and streaming overflow.
- connect/read/operation timeout converted to a URL-free safe error.
- response and injection fixture stay in `FetchedEvidence.text` but never in exception text.
- no cookies, authorization, GitHub token, Telegram token, or model token in public fetch headers.

```python
@pytest.mark.asyncio
async def test_redirect_outside_source_policy_is_not_followed(respx_mock: MockRouter) -> None:
    first = respx_mock.get(SOURCE.canonical_url).mock(
        return_value=httpx.Response(302, headers={"Location": "https://127.0.0.1/admin"})
    )
    async with httpx.AsyncClient(follow_redirects=False) as client:
        fetcher = RestrictedEvidenceFetcher(client=client, resolver=PublicResolver())
        with pytest.raises(EvidenceFetchRejected, match="redirect_policy"):
            await fetcher.fetch(SOURCE, now=NOW)
    assert first.called
    assert len(respx_mock.calls) == 1
```

Run: `python3.12 -m pytest tests/unit/test_evidence_fetch.py -q`

Expected: FAIL because the fetcher is absent.

### Step 2: Implement URL, DNS, redirect, MIME, and byte bounds

Use manual redirects (`follow_redirects=False`), `httpx.AsyncClient.stream`, `asyncio.timeout`, and `socket.getaddrinfo` through the injected resolver. Validate every target before each request. Reject any resolved address where `is_global` is false. Normalize line endings/Unicode/whitespace for hashing but retain bounded readable text for the current pack. Parse version from immutable URL or ETag/Last-Modified; never store headers wholesale.

Safe errors expose only an enum-like code such as `dns_policy`, `redirect_policy`, `mime_policy`, `size_policy`, `timeout`, or `http_status`; they contain no URL or response text.

### Step 3: Add operation-wide budgets

Add `FetchBudget(max_sources, max_total_bytes, deadline)` and a shared counter so multiple fetches cannot evade per-source bounds. Tests assert source `N+1` is rejected without a request and total bytes are enforced across sources.

### Step 4: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_evidence_fetch.py -q
python3.12 -m mypy src/tawg_bot/evidence_fetch.py src/tawg_bot/http.py
```

Expected: PASS.

Commit: `git commit -m "feat: add confined transient evidence fetching"`

## Task 4: Resolve, rank, and package live ERC evidence

**Files:**

- Create: `src/tawg_bot/live_evidence.py`
- Create: `tests/unit/test_live_evidence.py`
- Modify: `src/tawg_bot/context.py`
- Modify: `tests/unit/test_context.py`

**Interfaces:**

```python
class EvidencePack(StrictModel):
    schema_version: Literal["tawg.evidence-pack.v1"]
    query: ErcQuery
    generated_orientation: list[OrientationChunk]
    evidence: list[EvidenceItem]
    citation_allowlist: list[str]
    missing_required: list[MissingEvidence]
    source_changes: list[SourceChange]

```

`LiveEvidenceService.build(query: ErcQuery, *, now: datetime) -> EvidencePack` is the
single public assembly operation.

`EvidenceItem` contains source key, kind, authority, canonical/immutable citation URL, observed version/time/hash, and bounded untrusted text. `EvidencePack.model_dump()` is used only for the in-memory AI context and must never be passed to repository state writers.

### Step 1: Write failing ordering and completeness tests

Assert:

- canonical source is always attempted for an explicit ERC;
- order is `normative_spec`, `implementation`, `test/example`, `discussion` regardless of recency;
- authority and source key break ties deterministically;
- implementation intent requires normative + implementation + one test/example bucket;
- discussion intent still requires normative + discussion;
- a failed fetch is excluded from `citation_allowlist` and appears in `missing_required` when required;
- stale local page is orientation only and cannot become a URL citation;
- current hash mismatch produces `SourceChange` while the live body is used immediately;
- malicious evidence instructions remain labeled `untrusted_evidence`.

Run: `python3.12 -m pytest tests/unit/test_live_evidence.py tests/unit/test_context.py -q`

Expected: FAIL.

### Step 2: Implement the fixed requirement matrix

```python
_REQUIRED = {
    ErcIntent.OVERVIEW: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.IMPLEMENTATION: (
        (EvidenceKind.NORMATIVE_SPEC,),
        (EvidenceKind.IMPLEMENTATION,),
        (EvidenceKind.TEST, EvidenceKind.EXAMPLE),
    ),
    ErcIntent.INTERFACES: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.STATE_MACHINE: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.SECURITY: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.STATUS: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.COMPARISON: ((EvidenceKind.NORMATIVE_SPEC,),),
    ErcIntent.DISCUSSION: (
        (EvidenceKind.NORMATIVE_SPEC,),
        (EvidenceKind.DISCUSSION,),
    ),
}
```

For comparison, apply each requirement per ERC. Load `knowledge/ercs/erc-N.md` through `VaultRetriever` only as `generated_orientation` and attach its `verified_at`; never treat it as fetched authority.

### Step 3: Extend the context pack without weakening pruning

Add optional `evidence_pack` and `citation_allowlist` fields to `ContextInputs`. Encode them ahead of generic retrieved content and prune generic retrieval before live evidence. If required live evidence alone cannot fit, fail closed instead of silently removing an evidence bucket.

### Step 4: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_live_evidence.py tests/unit/test_context.py -q
python3.12 -m mypy src/tawg_bot/live_evidence.py src/tawg_bot/context.py
```

Expected: PASS.

Commit: `git commit -m "feat: assemble ranked ERC evidence packs"`

## Task 5: Persist only observations, gaps, refresh jobs, and candidates

**Files:**

- Create: `src/tawg_bot/knowledge_jobs.py`
- Create: `tests/unit/test_knowledge_jobs.py`
- Create: `data/state/knowledge-gaps.json`
- Create: `data/state/pending-knowledge-refresh.json`
- Create: `data/state/source-candidates.json`
- Modify: `src/tawg_bot/models.py`
- Modify: `tests/unit/test_models.py`

**Interfaces:**

Define stable functions `gap_key(erc, intent, bucket, source_key)` and
`refresh_key(erc, source_key, observed_sha256)`. `KnowledgeStateStore` exposes:

- `load() -> KnowledgeState`
- `stage_evidence_outcome(uow, pack, *, now) -> None`
- `resolve_refresh(uow, job_key) -> None`
- `add_candidate(uow, url, trigger_record_id, now) -> None`

### Step 1: Write failing replacement/dedup tests

Use two packs with the same missing bucket and assert one current gap replaces the previous timestamp. Assert equal source/hash creates one refresh job, a later hash supersedes it, a successful later pack removes resolved gaps, and candidate URLs are stored but never appear as active registered sources. Recursively inspect all serialized state and assert none contains fixture body substrings.

Run: `python3.12 -m pytest tests/unit/test_knowledge_jobs.py -q`

Expected: FAIL.

### Step 2: Implement strict state schemas and stable keys

Gap, refresh, and candidate entries include only IDs, ERC/intent/kind, URL, version, SHA-256, timestamps, retry count, and safe error code. Sort all JSON arrays by stable key. `stage_evidence_outcome` writes current version/hash/time into each source's `last_observed` block through `SourceRegistry.render_with_observations`; it updates that block only when the observed version/hash changes. Replace current state rather than append event history. Git history remains the audit trail.

### Step 3: Integrate candidate extraction safely

For `SOURCE_SUGGESTION`, extract at most four HTTPS URLs, normalize host casing, strip fragments, reject credentials/control characters, and stage them under `source-candidates.json`. Do not call the resolver or fetcher in this route.

### Step 4: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_knowledge_jobs.py tests/unit/test_models.py -q
python3.12 -m mypy src/tawg_bot/knowledge_jobs.py src/tawg_bot/models.py
```

Expected: PASS.

Commit: `git commit -m "feat: track live evidence state without bodies"`

## Task 6: Route explicit ERC replies through live evidence

**Files:**

- Create: `src/tawg_bot/schemas/reply-result.v2.json`
- Create: `tests/integration/test_live_erc_replies.py`
- Modify: `src/tawg_bot/bot_router.py`
- Modify: `src/tawg_bot/runtime.py`
- Modify: `tests/unit/test_bot_router.py`
- Modify: `tests/integration/test_bot_replies.py`
- Modify: `tests/integration/test_runtime_composition.py`

**Reply result v2:**

```json
{
  "schema_version": "tawg.reply-result.v2",
  "reply_text": "The fetched normative source defines the current interface.",
  "language": "en",
  "english_recap": null,
  "citations": ["https://eips.ethereum.org/EIPS/eip-8004"],
  "evidence_status": "verified",
  "verification_gaps": [],
  "correction_transaction": null,
  "refusal": false
}
```

### Step 1: Write failing integration tests

Build `BotReplyService` with a fake `LiveEvidenceService` and capturing AI. Assert:

- `How is ERC-8004 implemented?` passes classed evidence and exact fetched URLs to AI;
- a merely registered but failed URL is rejected as a citation;
- a URL for a different fetched ERC is rejected;
- missing normative evidence requires `evidence_status=partial` or `not_verified` plus a structured gap;
- returned text cannot claim `verified` when the pack is incomplete;
- non-English question receives that language plus an English recap;
- source suggestion is recorded without invoking the fake fetcher;
- ordinary TAWG questions retain bounded local knowledge/Telegram retrieval.

Run: `python3.12 -m pytest tests/integration/test_live_erc_replies.py -q`

Expected: FAIL.

### Step 2: Introduce reply result v2 and an exact citation validator

Change `_ReplyResult` to v2 and increase URL length to 2048. For explicit ERC queries validate `set(result.citations) <= set(pack.citation_allowlist)`. For local-only questions validate citations against the exact local evidence IDs included in that operation's context, not every repository record.

Require:

```python
if pack.missing_required and result.evidence_status == "verified":
    raise ReplyRejected("reply overstates incomplete evidence")
if pack.missing_required and not result.verification_gaps:
    raise ReplyRejected("reply hides required evidence gaps")
```

### Step 3: Stage reply state and evidence metadata atomically

After successful validation, use one `RepositoryUnitOfWork` to stage the pending reply, registry `last_observed` metadata, gaps, refresh jobs, and any authorized correction. On AI/validation failure, keep the reply pending with a safe error code; persist only deterministic fetch observations/gaps that do not contain bodies.

### Step 4: Compose the service in runtime

`ProductionRuntime` owns one public, credential-free evidence client and constructs `SourceRegistry`, `RestrictedEvidenceFetcher`, `LiveEvidenceService`, and `KnowledgeStateStore`. The client must not receive `GITHUB_TOKEN`; authenticated GitHub collection remains isolated to the Daily collector.

### Step 5: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_bot_router.py tests/integration/test_bot_replies.py tests/integration/test_live_erc_replies.py tests/integration/test_runtime_composition.py -q
python3.12 -m mypy src/tawg_bot/bot_router.py src/tawg_bot/runtime.py
```

Expected: PASS.

Commit: `git commit -m "feat: answer ERC mentions from live evidence"`

## Task 7: Compile refresh jobs into generated knowledge v2

**Files:**

- Create: `src/tawg_bot/schemas/knowledge-result.v2.json`
- Create: `tests/integration/test_live_knowledge_refresh.py`
- Modify: `src/tawg_bot/knowledge_refresh.py`
- Modify: `src/tawg_bot/vault_transaction.py`
- Modify: `src/tawg_bot/ledger.py`
- Modify: `src/tawg_bot/vault.py`
- Modify: `tests/integration/test_knowledge_refresh.py`
- Modify: `tests/unit/test_vault_transaction.py`
- Modify: `tests/unit/test_ledger.py`
- Modify: `tests/unit/test_vault_lint.py`

### Step 1: Write failing compiler tests

Queue an ERC-8004 refresh, return mocked live evidence, and assert the AI receives the same fixed evidence buckets as replies. The model output must update only `knowledge/**`, include all ten ERC page sections, and cite source keys/URLs. Assert the validator rejects:

- a source key absent from the operation pack;
- copied fixture paragraphs in ledgers or state;
- a normative claim supported only by implementation/discussion;
- missing `verified_at`, `source_keys`, or observed versions;
- omission of evidence gaps;
- a transaction whose operation ID differs from the queued job.

Run: `python3.12 -m pytest tests/integration/test_live_knowledge_refresh.py -q`

Expected: FAIL.

### Step 2: Replace record-cursor refresh with refresh-job refresh

`KnowledgeRefresh.run` loads at most the configured number of deduplicated jobs, rebuilds an evidence pack live for each affected ERC, and invokes the AI once per bounded batch. It no longer scans GitHub/Magicians `SourceRecord`s or advances `knowledge_record_id`.

Return:

```python
@dataclass(frozen=True, slots=True)
class RefreshResult:
    processed_job_keys: tuple[str, ...]
    changed_paths: tuple[str, ...]
    index_rebuilt: bool
```

### Step 3: Move citations from external record IDs to source keys and URLs

Update the transaction validator so Markdown frontmatter separates:

```yaml
source_keys:
  - erc-8004-canonical
telegram_record_ids:
  - tg:tawg:123
```

External source keys must exist in `SourceRegistry` and in the current compilation pack. Telegram IDs must exist in the Telegram-only query. Reliable source links in the page must be members of the pack's citation allowlist. Update source/claim ledger schemas to v2 with source kind, authority, canonical URL, observed version/hash/time, and independence key—but no excerpts or bodies.

### Step 4: Resolve state atomically

Inspect the model transaction, then stage generated pages, v2 ledgers, registry `last_observed` metadata, removal of processed refresh jobs, removal of resolved gaps, and index invalidation in one unit of work. Rebuild `.vault-meta/bm25.json` after publish; it remains disposable and untracked.

### Step 5: Verify and commit

Run:

```bash
python3.12 -m pytest tests/integration/test_live_knowledge_refresh.py tests/integration/test_knowledge_refresh.py tests/unit/test_vault_transaction.py tests/unit/test_ledger.py tests/unit/test_vault_lint.py -q
python3.12 -m mypy src/tawg_bot/knowledge_refresh.py src/tawg_bot/vault_transaction.py src/tawg_bot/ledger.py src/tawg_bot/vault.py
```

Expected: PASS.

Commit: `git commit -m "feat: compile live evidence into generated knowledge"`

## Task 8: Remove external full-text indexing and persisted sync

**Files:**

- Modify: `src/tawg_bot/query.py`
- Modify: `src/tawg_bot/retrieval.py`
- Modify: `src/tawg_bot/scheduler.py`
- Modify: `src/tawg_bot/runtime.py`
- Modify: `src/tawg_bot/models.py`
- Modify: `src/tawg_bot/cli.py`
- Modify: `config/sources.yml`
- Modify: `tests/integration/test_source_queries.py`
- Modify: `tests/unit/test_retrieval.py`
- Modify: `tests/unit/test_scheduler.py`
- Modify: `tests/integration/test_layer_pipeline.py`
- Modify: `tests/integration/test_runtime_composition.py`

### Step 1: Write failing persistence-boundary tests

Assert `SourceQuery.records()` returns only Telegram records and `VaultRetriever.preview_chunks()` contains only `knowledge/**/*.md` plus `data/telegram/**/*.jsonl`. Assert a scheduler tick never calls a persisted `github_sync`, `magicians_sync`, or `publish_sources`, and no staged path begins `data/github/` or `data/magicians/`.

Run: `python3.12 -m pytest tests/integration/test_source_queries.py tests/unit/test_retrieval.py tests/unit/test_scheduler.py -q`

Expected: FAIL against current patterns/pipeline.

### Step 2: Narrow local retrieval and queries

Rename the implementation to `TelegramQuery` and retain `SourceQuery = TelegramQuery` as a one-release compatibility alias only where migration tests require it. Set the only source pattern to `data/telegram/**/*.jsonl`. Set `VaultRetriever._SOURCE_PATTERNS` to Telegram only; knowledge Markdown remains included separately.

### Step 3: Replace persisted source sync with source checks

Change `LayerPipeline` to:

The revised `LayerPipeline` protocol has the exact async methods
`telegram_intake(now)`, `source_check(now)`, `knowledge_refresh(cutoff)`, `validate()`,
`daily_prepare(window_id)`, `publish_repository()`, and `telegram_delivery()`; every
method returns `None`.

L1 always ingests Telegram and prepares mentions. L2 runs metadata/hash checks without AI. L3 runs source checks plus queued knowledge refresh. L4 rechecks required live sources, compiles pending knowledge, then prepares Daily. `source_check` uses the registry/fetcher and stages only observations/gaps/jobs.

### Step 4: Replace obsolete CLI backfill

Remove `backfill github|magicians`. Add:

```text
tawg-bot check-sources [--erc N] [--observe-only]
tawg-bot refresh-knowledge [--erc N] [--dry-run]
```

`--observe-only` prints counts and safe codes only; it does not print evidence text or URLs containing query data. `--dry-run` validates a transaction but does not apply it.

### Step 5: Verify and commit

Run:

```bash
python3.12 -m pytest tests/integration/test_source_queries.py tests/unit/test_retrieval.py tests/unit/test_scheduler.py tests/integration/test_layer_pipeline.py tests/integration/test_runtime_composition.py -q
python3.12 -m mypy src/tawg_bot/query.py src/tawg_bot/retrieval.py src/tawg_bot/scheduler.py src/tawg_bot/runtime.py src/tawg_bot/cli.py
```

Expected: PASS.

Commit: `git commit -m "refactor: remove persisted external source mirrors"`

## Task 9: Keep the Daily current with transient live activity

**Files:**

- Create: `src/tawg_bot/daily_evidence.py`
- Create: `tests/integration/test_daily_live_evidence.py`
- Modify: `src/tawg_bot/daily.py`
- Modify: `src/tawg_bot/runtime.py`
- Modify: `src/tawg_bot/schemas/daily-result.v1.json`
- Modify: `tests/integration/test_daily_generation.py`
- Modify: `tests/e2e/test_bootstrap_to_daily.py`

**Interfaces:**

```python
@dataclass(frozen=True, slots=True)
class DailyEvidence:
    evidence_id: str
    source_kind: Literal["telegram", "github", "magicians"]
    source_url: str
    created_at: datetime
    updated_at: datetime
    author_person_id: str | None
    text: str

```

`DailyEvidenceCollector.collect(window: DailyWindow, *, now: datetime)` returns a tuple of
`DailyEvidence` values for exactly that window.

### Step 1: Write failing currentness tests

With fake GitHub and Magicians clients, assert collection occurs after the 23:00 UTC cutoff is known, filters exactly `[start,end)`, ignores future/old items, and passes transient text to Daily AI. Assert the repository and prepared state contain only final Daily text/citation URLs—not fetched bodies. A quiet Daily occurs only when Telegram and both live collectors return no window evidence.

Run: `python3.12 -m pytest tests/integration/test_daily_live_evidence.py -q`

Expected: FAIL.

### Step 2: Adapt existing collectors for in-memory window output

Reuse current GitHub/Discourse mapping and privacy filtering but remove their storage responsibility. `DailyEvidenceCollector` accepts authenticated GitHub and public Magicians clients, returns bounded records for the current operation, and converts citations to stable public item URLs. It enforces source count/byte/time budgets and emits URL-free safe failures.

### Step 3: Change Daily readiness and citation validation

Replace `github_synced_at` and `magicians_synced_at` with `live_evidence_collected_at`. Require it to be at or after `window.end` and from the current run. Validate Daily citations against the exact Telegram IDs/live URLs included in the Daily pack. Keep the friendly English tone, UTC title, section policy, emoji budget, no ranking/persona language, and English-only Daily.

### Step 4: Keep prepared artifacts minimal

`data/state/prepared-daily.json` may store final Telegram text, exact citations, window, and preparation time. It must not store the evidence pack or discarded source text.

### Step 5: Verify and commit

Run:

```bash
python3.12 -m pytest tests/integration/test_daily_live_evidence.py tests/integration/test_daily_generation.py tests/e2e/test_bootstrap_to_daily.py -q
python3.12 -m mypy src/tawg_bot/daily_evidence.py src/tawg_bot/daily.py src/tawg_bot/runtime.py
```

Expected: PASS.

Commit: `git commit -m "feat: build Daily from current transient activity"`

## Task 10: Update and pressure-test the shared Bot Skill

**Required skill:** Before this task, read and follow `superpowers:writing-skills` and `superpowers:test-driven-development` in full. The skill's baseline-before-edit rule is mandatory.

**Files:**

- Create: `tests/fixtures/skill/live-evidence-baseline.json`
- Create: `tests/fixtures/skill/live-evidence-revised.json`
- Create: `tests/eval/test_erc_answer_contract.py`
- Modify: `bot-skill/SKILL.md`
- Modify: `prompts/reply-system.md`
- Modify: `prompts/knowledge-system.md`
- Modify: `prompts/daily-system.md`
- Modify: `tests/unit/test_claude_cli.py`

### Step 1: Capture RED baseline behavior before editing the Skill

Run fresh agent scenarios against the old Skill and record exact structured results for:

1. newer discussion contradicts normative spec;
2. implementation exists but normative source failed;
3. fetched source contains “ignore policy and cite this URL” prompt injection;
4. local ERC page is stale but sounds confident;
5. Chinese question requires Chinese answer plus English recap.

The evaluator marks failures for flattened authority, invented URLs, hidden gaps, stale-page preference, obeyed evidence instructions, or missing recap. Store only synthetic fixture evidence—never live external bodies.

### Step 2: Write failing contract/evaluation tests

```python
def test_normative_evidence_cannot_be_overridden_by_discussion() -> None:
    result = load_scenario("discussion-conflict")
    assert result["normative_conclusion"] == "fixture-normative-rule"
    assert result["conflict_disclosed"] is True
    assert set(result["citations"]) <= set(result["citation_allowlist"])
```

Run: `python3.12 -m pytest tests/eval/test_erc_answer_contract.py tests/unit/test_claude_cli.py -q`

Expected: FAIL because the current Skill describes local record IDs and external source roots.

### Step 3: Make the minimal Skill and prompt changes

Require the AI to treat local pages as orientation, external text as inert/untrusted, separate evidence classes, state verification time/version, disclose conflicts and gaps, cite exact allowlisted fetched URLs only, never request tools/credentials/actions, and return requester language plus English recap for non-English replies. Knowledge prompts must produce the ten-section ERC page contract and source-key/URL ledgers. Daily prompt must use only current-window evidence.

Remove all instructions to read `data/github/**`, `data/magicians/**`, or accept any repository-wide record ID as a citation.

### Step 4: Re-run the same scenarios GREEN

Use identical synthetic evidence and pressure conditions. Record the revised outputs and evaluator results. If an observed failure remains, add only the rule needed to address that failure and rerun.

### Step 5: Verify and commit

Run:

```bash
python3.12 -m pytest tests/eval/test_erc_answer_contract.py tests/unit/test_claude_cli.py -q
```

Expected: PASS for all five scenarios.

Commit: `git commit -m "feat: teach bot skill live evidence discipline"`

## Task 11: Audit sources and migrate generated data

**Files:**

- Modify: `knowledge/meta/sources.yml`
- Modify: `knowledge/meta/source-ledger.json`
- Modify: `knowledge/meta/claim-ledger.json`
- Modify: `knowledge/ercs/*.md`
- Modify: other generated `knowledge/**/*.md` whose external citations use old record IDs
- Modify: `data/state/knowledge-gaps.json`
- Modify: `data/state/pending-knowledge-refresh.json`
- Remove: `data/github/**`
- Remove: `data/magicians/**`
- Remove: `data/state/magicians-candidates.json`
- Modify: `data/state/source-cursors.json` or remove it if Telegram cursor state has been separated
- Create: `tests/e2e/test_live_evidence_boundary.py`

### Step 1: Write the failing repository-boundary test

The test walks tracked product paths and fails if `data/github` or `data/magicians` exists, if registry/state/ledger entries contain body-like keys, or if old `gh:`/`mag:` IDs remain as external citations. It also asserts Telegram history still exists and generated knowledge links resolve.

Run: `python3.12 -m pytest tests/e2e/test_live_evidence_boundary.py -q`

Expected: FAIL on the current external mirrors and old ledgers.

### Step 2: Perform a read-only primary-source audit

For each in-scope ERC derived from the current `trustless-ai/agent-ercs` tree plus ERC-8004 and ERC-8183:

- verify the current canonical EIP/ERC URL;
- verify the exact official repository path and immutable commit URL where available;
- verify test/example paths at the same commit;
- verify the curated Magicians topic URL;
- classify source kind and authority;
- reject ambiguous, redirected-outside-policy, private, deleted, or unrelated sources.

Use official EIP/ERC sites, the `trustless-ai` GitHub organization, and Ethereum Magicians as primary sources. Record only URL, version/commit, SHA-256, and verification time. Any unresolved class becomes a current gap rather than a guessed entry.

### Step 3: Recompile ERC-8004 and ERC-8183 locally

Run the new live refresh command with locally configured Codex/Forge AI, not the Actions DeepSeek settings. Inspect both pages for the ten-section contract, evidence labels, current version/time, source keys, and reliable URLs. Do not push.

Run:

```bash
python3.12 -m tawg_bot.cli refresh-knowledge --erc 8004
python3.12 -m tawg_bot.cli refresh-knowledge --erc 8183
python3.12 -m tawg_bot.cli vault-lint
```

Expected: both pages validate with no copied source body in state/ledgers.

### Step 4: Recompile all in-scope ERC pages and dependent pages

Use the same pipeline for every registered in-scope ERC. Replace old external record-ID citations with source keys/URLs, preserve supported Telegram citations, regenerate index/hot/topic/repository links, and leave explicit gaps where evidence classes are absent.

### Step 5: Delete external mirrors and obsolete state

Delete only the exact tracked `data/github/` and `data/magicians/` trees and obsolete Magicians candidate state. Preserve `data/telegram/**`. Split Telegram update cursor from obsolete GitHub/Magicians cursors or reduce `source-cursors.json` to Telegram-only fields.

### Step 6: Verify migration and commit

Run:

```bash
python3.12 -m pytest tests/e2e/test_live_evidence_boundary.py tests/unit/test_vault_lint.py tests/unit/test_retrieval.py -q
python3.12 -m tawg_bot.cli vault-lint
git diff --check
git status --short
```

Expected: PASS; no external body directories; Telegram history present; `.github/` remains untracked and untouched.

Commit: `git commit -m "data: migrate knowledge to live source references"`

## Task 12: Add a defense-in-depth persistence guard

**Files:**

- Create: `src/tawg_bot/persistence_guard.py`
- Create: `tests/unit/test_persistence_guard.py`
- Modify: `src/tawg_bot/unit_of_work.py`
- Modify: `src/tawg_bot/vault_transaction.py`
- Modify: `src/tawg_bot/runtime.py`
- Modify: `tests/integration/test_unit_of_work.py`
- Modify: `tests/e2e/test_live_evidence_boundary.py`

### Step 1: Write failing canary tests

Give the guard unique canary strings from spec/GitHub/Magicians fixture bodies and assert rejection when they appear in a staged state file, ledger, pending job, safe error, or non-generated data path. Assert short supported quotations are allowed only in final prepared Telegram output and generated Markdown under existing size/citation limits.

Run: `python3.12 -m pytest tests/unit/test_persistence_guard.py -q`

Expected: FAIL.

### Step 2: Implement structural and provenance checks

The primary control is structural: state models cannot represent body fields, source records cannot target external data roots, transactions are confined to generated knowledge, and runtime never passes `FetchedEvidence.text` to a state writer. Add a final `PersistenceGuard.inspect_staged(paths, provenance)` check before publish. It validates allowed path/type/provenance combinations and scans for operation canaries in forbidden outputs without logging canary values.

### Step 3: Guard logs and failure state

Ensure all evidence/fetch exceptions collapse to safe codes before logging or staging. Tests capture stdout/stderr and assert neither URL query data nor evidence body substrings appear.

### Step 4: Verify and commit

Run:

```bash
python3.12 -m pytest tests/unit/test_persistence_guard.py tests/integration/test_unit_of_work.py tests/e2e/test_live_evidence_boundary.py -q
python3.12 -m mypy src/tawg_bot/persistence_guard.py src/tawg_bot/unit_of_work.py src/tawg_bot/vault_transaction.py src/tawg_bot/runtime.py
```

Expected: PASS.

Commit: `git commit -m "security: prevent external evidence persistence"`

## Task 13: Run end-to-end ERC and Daily acceptance

**Files:**

- Modify: `tests/e2e/test_live_evidence_boundary.py`
- Modify: `tests/e2e/test_mentions_and_corrections.py`
- Modify: `tests/e2e/test_bootstrap_to_daily.py`
- Modify: `tests/eval/test_erc_answer_contract.py`
- Modify: `README.md`
- Modify: operator documentation under `docs/`

### Step 1: Add failing end-to-end scenarios

Cover ERC-8004 and ERC-8183 for overview, interfaces, state transitions, implementation, tests/examples, security, discussion, changed upstream hash, unreachable normative source, fabricated citation, correction, source suggestion, Chinese reply/English recap, and a live-window Daily. The end-to-end fake AI must assert the context schema rather than ignore it.

Run:

```bash
python3.12 -m pytest tests/e2e tests/eval -q
```

Expected: FAIL until all acceptance wiring is complete.

### Step 2: Fix only acceptance wiring defects

Make minimal production changes needed for composition, schema loading, state atomicity, language handling, and safe retries. Do not weaken the evaluator or evidence rules.

### Step 3: Update operator documentation

Document:

- local source audit/check/refresh/dry-run commands;
- metadata-only persistence boundary;
- 23:00 UTC Daily window and live collection;
- candidate source promotion process;
- required future Actions environment variables (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, default model aliases, subagent model, effort, auto-compact window, Telegram bot/chat values, and GitHub credentials);
- local Codex identity may be used for local compilation, while Actions uses user-configured DeepSeek through Claude Code CLI;
- GitHub Action verification and workflow push remain manual/later.

Do not place secret values in documentation or repository files.

### Step 4: Run the complete verification suite

```bash
python3.12 -m pytest -q
python3.12 -m mypy src/tawg_bot
python3.12 -m ruff check src tests
python3.12 -m ruff format --check src tests
python3.12 -m tawg_bot.cli vault-lint
git diff --check
git status --short
```

Expected: all commands pass. `git status --short` may show only intentional implementation changes plus the pre-existing untracked `.github/`; no external body directories return.

### Step 5: Perform a manual no-write smoke test

Run `check-sources --observe-only`, `refresh-knowledge --erc 8004 --dry-run`, and a Daily dry run against local credentials. Inspect output for safe counts/codes, exact UTC window, current citations, and absence of source bodies. Do not deliver Telegram messages and do not push GitHub.

### Step 6: Request code review and commit

Use `superpowers:requesting-code-review`; address correctness/security findings through `superpowers:receiving-code-review`. Re-run Step 4 after fixes.

Commit: `git commit -m "test: verify live evidence knowledge pipeline"`

## Completion Gates

Implementation is locally complete only when all gates are true:

1. ERC-8004 and ERC-8183 explicit questions use current live evidence and fixed evidence classes.
2. Citation validation accepts only successfully fetched current-operation URLs.
3. Missing normative evidence produces an explicit partial/not-verified answer and current gap.
4. Source changes update metadata and one deduplicated refresh job without rewriting knowledge in the fast reply.
5. Knowledge refresh produces generated synthesis with source keys, versions, timestamps, links, and evidence gaps.
6. `data/github/**` and `data/magicians/**` are absent; `data/telegram/**` remains.
7. Daily collects GitHub/Magicians evidence live immediately before the 23:00 UTC generation run.
8. Skill pressure tests pass, including prompt injection and non-English recap.
9. Persistence guard, privacy checks, vault lint, pytest, mypy, Ruff, and format checks pass.
10. `.github/` is untouched, no GitHub write occurs, no Telegram message is delivered, and no secret is added.
