# Open Knowledge Mutation and Scoped Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow evidence-backed interactive storage of any knowledge while limiting recurring discovery to every public `trustless-ai` repository and validated ERC Magicians/optional proposal-PR targets.

**Architecture:** Separate interactive mutation capability, recurring scan registration, and retrieval scope into controller-owned types. The model proposes structured effects; the controller validates and stages them; one repository unit of work publishes valid knowledge and scan registration before Telegram delivery. Replace the broad scheduled ERC refresh with a metadata-only scoped scanner and an idempotent migration that preserves raw records and audit history.

**Tech Stack:** Python 3.13, Pydantic v2, asyncio/httpx, PyYAML, pytest, Ruff, mypy, repository unit-of-work persistence.

**Spec:** `docs/superpowers/specs/2026-08-28-open-knowledge-mutation-and-scoped-scanning-design.md`

## Global Constraints

- Knowledge admission has no noun, verb, domain, TAWG, or ERC whitelist.
- Self-authorship requires explicit audited Telegram evidence; ambiguous authorship is external.
- External knowledge requires an allowlisted original HTTPS URL and at most 2,000 descriptive characters outside frontmatter and the source-link section.
- New pages stay under `knowledge/topics/` or `knowledge/repos/`; exact existing revisions may replace other non-meta knowledge pages.
- At most three exact revisions totaling 60,000 characters enter one correction context.
- The model never edits scan configuration, registries, ledgers, aliases, indexes, workflows, or runtime policy directly.
- Recurring organization scope is every public `trustless-ai` repository, including archived repositories.
- Recurring ERC scope is one validated Magicians topic plus an optional validated `github.com/ethereum/ERCs` proposal PR.
- Existing Telegram records, knowledge text, delivery audit, and pending reply jobs are preserved.
- External source bodies remain untrusted and are not persisted by the scoped scanner.
- Do not modify `.github/workflows/`.
- Use `/usr/local/bin/ruff`; do not invoke paid models locally.
- GitHub writes use GitHub MCP only; never run local `git push`, `gh`, or direct GitHub API writes.

---

### Task 1: Controller-Owned Scan Target Registry

**Files:**
- Create: `src/tawg_bot/scan_targets.py`
- Create: `knowledge/meta/scan-targets.yml`
- Create: `tests/unit/test_scan_targets.py`
- Modify: `tests/support/runtime_repository.py`
- Modify: `src/tawg_bot/persistence_guard.py`

**Interfaces:**
- Consumes: `StrictModel`, `RepositoryUnitOfWork`, flat YAML repository conventions.
- Produces: `ScanRegistrationProposal`, `ErcScanTarget`, `ScanTargetRegistry`, and `ScanTargetStore`.

- [ ] **Step 1: Write failing parsing, uniqueness, and path-confinement tests**

```python
def test_scan_registry_separates_org_and_erc_targets(tmp_path: Path) -> None:
    write_scan_registry(
        tmp_path,
        {
            "schema": "tawg.scan-targets.v1",
            "github_organization": "trustless-ai",
            "include_public_archived_repositories": True,
            "ercs": [
                {
                    "erc_number": 8183,
                    "magicians_topic_url": "https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902",
                    "proposal_pr_url": None,
                    "registered_from_record_id": "tg:tawg:3387",
                    "registered_at": "2026-08-28T00:00:00Z",
                }
            ],
        },
    )
    registry = ScanTargetStore(tmp_path).load()
    assert registry.github_organization == "trustless-ai"
    assert registry.include_public_archived_repositories
    assert registry.ercs[0].erc_number == 8183


def test_scan_registry_rejects_wrong_hosts() -> None:
    with pytest.raises(ScanTargetRejected):
        ErcScanTarget(
            erc_number=8183,
            magicians_topic_url="https://example.com/t/8183/1",
            proposal_pr_url="https://github.com/example/repo/pull/1",
            registered_from_record_id="tg:tawg:1",
            registered_at=NOW,
        )
```

- [ ] **Step 2: Run `python -m pytest tests/unit/test_scan_targets.py -q` and verify RED**

Expected: collection fails because `tawg_bot.scan_targets` does not exist.

- [ ] **Step 3: Implement strict models and stable YAML storage**

```python
class ScanRegistrationProposal(StrictModel):
    erc_number: int = Field(ge=1, le=99_999)
    magicians_topic_url: str = Field(min_length=1, max_length=2048)
    proposal_pr_url: str | None = Field(default=None, min_length=1, max_length=2048)


class ErcScanTarget(ScanRegistrationProposal):
    registered_from_record_id: str = Field(min_length=1, max_length=256)
    registered_at: datetime


class ScanTargetStore:
    PATH = "knowledge/meta/scan-targets.yml"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def load(self) -> ScanTargetRegistry:
        return parse_scan_target_yaml((self.root / self.PATH).read_text(encoding="utf-8"))

    def stage(self, uow: RepositoryUnitOfWork, registry: ScanTargetRegistry) -> None:
        uow.stage_bytes(self.PATH, render_scan_target_yaml(registry).encode("utf-8"))
```

Validate exact hosts and paths, UTC timestamps, uniqueness by ERC number and Magicians topic ID, stable numeric sorting, and idempotent identical registrations. Make `persistence_guard.py` accept only controller-staged registry changes whose organization remains `trustless-ai` and archived policy remains true.

- [ ] **Step 4: Seed ERC-8004, ERC-8183, ERC-8312, and ERC-8323 from exact existing Magicians mappings**

Do not infer optional PRs or the remaining seven ERC mappings.

- [ ] **Step 5: Run registry tests and vault lint GREEN**

Run: `python -m pytest tests/unit/test_scan_targets.py tests/unit/test_persistence_guard.py -q`

Run: `PYTHONPATH=src python -m tawg_bot.cli vault-lint`

- [ ] **Step 6: Commit**

```bash
git add src/tawg_bot/scan_targets.py knowledge/meta/scan-targets.yml tests/unit/test_scan_targets.py tests/support/runtime_repository.py src/tawg_bot/persistence_guard.py
git commit -m "feat: add scoped scan target registry"
```

### Task 2: Idempotent Existing-Data Migration

**Files:**
- Create: `src/tawg_bot/open_knowledge_migration.py`
- Create: `tests/integration/test_open_knowledge_migration.py`
- Modify: `src/tawg_bot/cli.py`
- Modify: `src/tawg_bot/vault.py`

**Interfaces:**
- Consumes: current scan registry, `pending-knowledge-refresh.json`, retained Telegram records, Markdown frontmatter.
- Produces: `OpenKnowledgeMigration.run(now: datetime) -> MigrationSummary` and CLI command `migrate-open-knowledge`.

- [ ] **Step 1: Write failing preservation and idempotency tests**

```python
def test_migration_preserves_raw_records_and_archives_refresh_jobs(tmp_path: Path) -> None:
    scaffold(tmp_path)
    write_legacy_refresh_jobs(tmp_path, count=2)
    telegram_before = snapshot_tree(tmp_path / "data/telegram")
    bodies_before = snapshot_markdown_bodies(tmp_path / "knowledge")
    summary = OpenKnowledgeMigration(tmp_path).run(now=NOW)
    assert snapshot_tree(tmp_path / "data/telegram") == telegram_before
    assert snapshot_markdown_bodies(tmp_path / "knowledge") == bodies_before
    assert summary.legacy_refresh_jobs_archived == 2
    assert json.loads((tmp_path / "data/state/pending-knowledge-refresh.json").read_text()) == []


def test_migration_second_run_has_no_diff(tmp_path: Path) -> None:
    scaffold(tmp_path)
    OpenKnowledgeMigration(tmp_path).run(now=NOW)
    first = snapshot_repository(tmp_path)
    OpenKnowledgeMigration(tmp_path).run(now=NOW)
    assert snapshot_repository(tmp_path) == first
```

- [ ] **Step 2: Run `python -m pytest tests/integration/test_open_knowledge_migration.py -q` and verify RED**

Expected: import fails because `OpenKnowledgeMigration` is absent.

- [ ] **Step 3: Implement hash-bound migration state**

```python
@dataclass(frozen=True, slots=True)
class MigrationSummary:
    legacy_refresh_jobs_archived: int
    provenance_backfilled: int
    provenance_marked_incomplete: int
    scan_targets_seeded: int
    changed: bool


class OpenKnowledgeMigration:
    STATE_PATH = "data/state/migrations/open-knowledge-v1.json"
    VERSION = "open-knowledge-v1"

    def run(self, *, now: datetime) -> MigrationSummary:
        require_utc(now)
        return self._run_with_hashes(now=now, input_hashes=self._input_hashes())
```

Stage the full old queue in the migration audit and empty the active queue in one unit of work. Store schema version, input/output hashes, and completion time; store no external response body. A completed migration is a no-op when current hashes match stored output hashes. A different current hash after completion is a conflict that requires diagnosis, not an automatic second migration.

- [ ] **Step 4: Backfill provenance without changing Markdown bodies**

For a legacy repo page with exactly one canonical `https://github.com/trustless-ai/` evidence link, add flat `source_urls` and `provenance_status: verified`. Add exact retained Telegram IDs only when already deterministically tied. Otherwise add `provenance_status: legacy_incomplete`. Extend `VaultLinter` so incomplete pages are navigation context but not mutation evidence.

- [ ] **Step 5: Add CLI wiring and run migration tests GREEN**

Run: `python -m pytest tests/integration/test_open_knowledge_migration.py tests/unit/test_vault_lint.py -q`

- [ ] **Step 6: Commit**

```bash
git add src/tawg_bot/open_knowledge_migration.py src/tawg_bot/cli.py src/tawg_bot/vault.py tests/integration/test_open_knowledge_migration.py
git commit -m "feat: migrate legacy knowledge state safely"
```

### Task 3: Interactive Mutation Capability and Exact Revisions

**Files:**
- Create: `src/tawg_bot/knowledge_mutation.py`
- Create: `tests/unit/test_knowledge_mutation.py`
- Modify: `src/tawg_bot/context.py`
- Modify: `src/tawg_bot/bot_router.py`
- Modify: `tests/integration/test_reply_pipeline_regressions.py`

**Interfaces:**
- Consumes: current trigger, audited chain, ranked retrieved paths, existing pages.
- Produces: `KnowledgeMutationCapability`, `ExactKnowledgeRevision`, `build_mutation_capability()`, and `validate_knowledge_transaction()`.

- [ ] **Step 1: Write failing tests for the removed authority gates and revision budget**

```python
def test_explicit_mention_can_create_arbitrary_knowledge(tmp_path: Path) -> None:
    capability = build_mutation_capability(
        tmp_path,
        route=BotRoute.KNOWLEDGE_CORRECTION,
        trigger=record("tg:tawg:5000", "Please record our Garden Clock concept."),
        reply_chain=(),
        retrieved_paths=(),
    )
    assert capability.can_create_page
    assert capability.required_evidence == ("tg:tawg:5000",)


def test_exact_revisions_stop_at_three_and_sixty_thousand_chars(tmp_path: Path) -> None:
    create_ranked_pages(tmp_path, character_counts=(20_000, 20_000, 20_000, 20_000))
    capability = correction_capability(tmp_path, ranked_paths(4))
    assert len(capability.exact_revisions) == 3
    assert sum(len(item.content) for item in capability.exact_revisions) <= 60_000


def test_stale_revision_is_rejected_without_overwrite(tmp_path: Path) -> None:
    capability = correction_capability(tmp_path, ("knowledge/topics/garden-clock.md",))
    mutate_page_after_capability(tmp_path, "knowledge/topics/garden-clock.md")
    with pytest.raises(ReplyRejected, match="stale knowledge revision"):
        validate_knowledge_transaction(stale_transaction(capability), capability)
```

- [ ] **Step 2: Run `python -m pytest tests/unit/test_knowledge_mutation.py -q` and verify RED**

- [ ] **Step 3: Implement capability models and safe revision loading**

```python
class ExactKnowledgeRevision(StrictModel):
    path: str
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: str


class KnowledgeMutationCapability(StrictModel):
    can_create_page: bool
    allowed_create_roots: tuple[str, ...]
    exact_revisions: tuple[ExactKnowledgeRevision, ...]
    required_evidence: tuple[str, ...]
    authorship_policy: Literal["explicit_only"] = "explicit_only"
```

Read only regular non-symlink Markdown under `knowledge/`. Exclude `knowledge/meta/`, index, hot cache, acknowledgement pages, and pages marked `provenance_status: legacy_incomplete`; include supported ERC pages for exact replacement. Reject duplicate or case-colliding paths.

- [ ] **Step 4: Put mutation capability in an unprunable context field**

Add `mutation_capability` to `ContextInputs`, serialize it after `trigger`, and omit it from `prune_order`. Build it for every `knowledge_correction` regardless of `context_scope` or `trigger_kind`; use a disabled capability otherwise.

- [ ] **Step 5: Validate transactions against capability instead of route shape**

New pages must use an allowed root, one write, allowed filename, and required trigger citation. Replacements must match one exact path and SHA. Remove `reply_to_bot`, audited-parent, and `context_scope` as write gates while retaining current-trigger route authorization.

- [ ] **Step 6: Run targeted tests GREEN**

Run: `python -m pytest tests/unit/test_knowledge_mutation.py tests/integration/test_reply_pipeline_regressions.py -q`

- [ ] **Step 7: Commit**

```bash
git add src/tawg_bot/knowledge_mutation.py src/tawg_bot/context.py src/tawg_bot/bot_router.py tests/unit/test_knowledge_mutation.py tests/integration/test_reply_pipeline_regressions.py
git commit -m "feat: authorize open evidence-backed knowledge writes"
```

### Task 4: Authorship-Aware Reply Contract

**Files:**
- Create: `src/tawg_bot/schemas/reply-result.v3.json`
- Modify: `src/tawg_bot/bot_router.py`
- Modify: `src/tawg_bot/claude_cli.py`
- Modify: `prompts/route-system.md`
- Modify: `prompts/reply-system.md`
- Modify: `bot-skill/SKILL.md`
- Modify: `tests/unit/test_claude_cli.py`
- Modify: `tests/e2e/test_mentions_and_corrections.py`

**Interfaces:**
- Consumes: mutation capability, correction transaction, citation allowlist.
- Produces: reply-result v3 fields `knowledge_write` and `scan_registration`.

- [ ] **Step 1: Write failing self-authored/external behavior tests**

```python
def test_external_knowledge_requires_original_url(tmp_path: Path) -> None:
    result = reply_result_v3(
        knowledge_write={
            "authorship": "external",
            "authorship_evidence": ["tg:tawg:5101"],
            "original_url": None,
        },
        correction_transaction=new_topic_transaction(
            body="A short description.", citations=["tg:tawg:5101"]
        ),
    )
    with pytest.raises(ReplyRejected, match="external knowledge requires an original URL"):
        prepare_result(tmp_path, result)
```

Add passing self-authored full-page and external brief-page cases, plus failures for ambiguous authorship, unallowlisted authorship IDs, unallowlisted original URL, and a 2,001-character external description.
Also assert that a friendly missing-evidence response with no transaction becomes `ready` and is not retried as `pending`.

- [ ] **Step 2: Run reply schema/E2E tests and verify RED**

Run: `python -m pytest tests/unit/test_claude_cli.py tests/e2e/test_mentions_and_corrections.py -q`

- [ ] **Step 3: Add v3 Pydantic and JSON Schema types**

```python
class _KnowledgeWriteDecision(StrictModel):
    authorship: Literal["self_authored", "external"]
    authorship_evidence: list[str]
    original_url: str | None


class _ReplyResult(StrictModel):
    schema_version: Literal["tawg.reply-result.v3"]
    reply_text: str
    language: str
    english_recap: str | None
    citations: list[str]
    evidence_status: Literal["verified", "partial", "not_verified"]
    verification_gaps: list[str]
    correction_transaction: VaultTransaction | None
    knowledge_write: _KnowledgeWriteDecision | None
    scan_registration: ScanRegistrationProposal | None
    refusal: bool
```

Require `knowledge_write` for knowledge-correction transactions and forbid it for null transactions or unrelated routes. Identity corrections retain their existing transaction contract.

- [ ] **Step 4: Enforce authorship and content shape**

For self-authored knowledge, require one current-trigger/audited-chain Telegram ID and allow only allowlisted IDs. For external knowledge, extract normalized public HTTPS URLs supplied by the current trigger or audited chain into the citation allowlist, require `original_url` to match one of them, require it in write citations and reply citations, and cap the description after excluding frontmatter and final `## Sources`. Keep the existing no-copy persistence guard.

- [ ] **Step 5: Align prompt and bot skill with the controller capability**

In `route-system.md`, classify every explicit record/add/change-knowledge request as `knowledge_correction` regardless of topic while preserving refusal for non-knowledge external actions. In the reply prompt and bot skill, state that any subject can be stored, unknown authorship is external, ordinary knowledge needs no ERC scan metadata, and missing evidence produces a friendly clarification with no transaction. Remove old direct-reply/context-scope page-creation language.

- [ ] **Step 6: Run reply tests GREEN**

Run: `python -m pytest tests/unit/test_claude_cli.py tests/e2e/test_mentions_and_corrections.py tests/integration/test_reply_pipeline_regressions.py -q`

- [ ] **Step 7: Commit**

```bash
git add src/tawg_bot/schemas/reply-result.v3.json src/tawg_bot/bot_router.py src/tawg_bot/claude_cli.py prompts/route-system.md prompts/reply-system.md bot-skill/SKILL.md tests/unit/test_claude_cli.py tests/e2e/test_mentions_and_corrections.py
git commit -m "feat: distinguish original and external knowledge"
```

### Task 5: Validated ERC Scan Registration

**Files:**
- Modify: `src/tawg_bot/scan_targets.py`
- Modify: `src/tawg_bot/bot_router.py`
- Modify: `src/tawg_bot/runtime.py`
- Create: `tests/integration/test_erc_scan_registration.py`

**Interfaces:**
- Consumes: `ScanRegistrationProposal`, `MagiciansHttpClient`, `GitHubHttpClient`, trigger ID, unit of work.
- Produces: `ScanTargetVerifier.verify() -> ErcScanTarget` and atomic staging.

- [ ] **Step 1: Write failing complete, optional-PR, mismatch, and partial-success tests**

```python
@pytest.mark.asyncio
async def test_invalid_registration_does_not_block_valid_knowledge(tmp_path: Path) -> None:
    prepared = await prepare_registration_reply(
        tmp_path,
        verifier=FakeScanTargetVerifier(topic_title="Other topic"),
        erc_number=8183,
        magicians_url="https://ethereum-magicians.org/t/other/27902",
        pr_url=None,
    )
    assert (tmp_path / "knowledge/topics/agentic-commerce.md").is_file()
    assert "recurring scan was not registered" in prepared.reply_text
    assert ScanTargetStore(tmp_path).load().ercs == []
```

- [ ] **Step 2: Run `python -m pytest tests/integration/test_erc_scan_registration.py -q` and verify RED**

- [ ] **Step 3: Implement metadata verification**

```python
class ScanTargetVerifier:
    async def verify(
        self,
        proposal: ScanRegistrationProposal,
        *,
        trigger_record_id: str,
        now: datetime,
    ) -> ErcScanTarget:
        topic = await self._topic(proposal.magicians_topic_url)
        self._require_erc_reference(topic, proposal.erc_number)
        if proposal.proposal_pr_url is not None:
            pull = await self._pull(proposal.proposal_pr_url)
            self._require_erc_reference(pull, proposal.erc_number)
        return ErcScanTarget(
            **proposal.model_dump(),
            registered_from_record_id=trigger_record_id,
            registered_at=now,
        )
```

Fetch Discourse `/t/<id>.json` and inspect resolved ID, slug, and title. Fetch optional `/repos/ethereum/ERCs/pulls/<number>` and inspect number, title, and body. Reject wrong hosts, redirects, credentials, private/unreachable data, mismatches, and oversized metadata.

- [ ] **Step 4: Stage valid registration with the correction transaction**

Inject verifier from `_LivePipeline`. Verify before publish and stage the registry in the same unit of work. A safe registration rejection drops only the registration and appends a deterministic localized clarification; a valid knowledge write remains staged.

- [ ] **Step 5: Run registration and persistence tests GREEN**

Run: `python -m pytest tests/integration/test_erc_scan_registration.py tests/integration/test_unit_of_work.py tests/unit/test_persistence_guard.py -q`

- [ ] **Step 6: Commit**

```bash
git add src/tawg_bot/scan_targets.py src/tawg_bot/bot_router.py src/tawg_bot/runtime.py tests/integration/test_erc_scan_registration.py
git commit -m "feat: register complete ERC scan targets"
```

### Task 6: Metadata-Only Scoped Scheduled Scanner

**Files:**
- Create: `src/tawg_bot/scoped_scanner.py`
- Create: `tests/integration/test_scoped_scanner.py`
- Modify: `src/tawg_bot/runtime.py`
- Modify: `src/tawg_bot/daily_evidence.py`
- Modify: `src/tawg_bot/scheduler.py`
- Modify: `tests/unit/test_scheduler.py`
- Modify: `tests/integration/test_runtime_composition.py`
- Modify: `tests/integration/test_daily_live_evidence.py`

**Interfaces:**
- Consumes: scan registry, GitHub/Magicians clients, `SourceCursors`.
- Produces: `ScopedSourceScanner.scan(since, now) -> ScopedScanResult` and metadata/cursor persistence.

- [ ] **Step 1: Write failing source-boundary and broad-refresh-removal tests**

```python
@pytest.mark.asyncio
async def test_scan_uses_all_org_repos_and_registered_erc_sources(tmp_path: Path) -> None:
    scanner = scoped_scanner_fixture(
        tmp_path,
        public_org_repos=("active", "archived"),
        magicians_topic_ids=(25098, 27902),
        proposal_pr_numbers=(1932,),
    )
    result = await scanner.scan(since=NOW - timedelta(minutes=30), now=NOW)
    assert result.github_repositories == ("active", "archived")
    assert result.magicians_topic_ids == (25098, 27902)
    assert result.proposal_pr_numbers == (1932,)


@pytest.mark.asyncio
async def test_pipeline_never_calls_broad_knowledge_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline = live_pipeline_fixture(tmp_path)
    monkeypatch.setattr(runtime_module, "KnowledgeRefresh", forbidden_refresh)
    await pipeline.source_check(NOW)
    await pipeline.knowledge_refresh(NOW)
```

- [ ] **Step 2: Run scanner/runtime tests and verify RED**

Run: `python -m pytest tests/integration/test_scoped_scanner.py tests/integration/test_runtime_composition.py -q`

- [ ] **Step 3: Implement metadata-only observations and cursor staging**

```python
class ScopedObservation(StrictModel):
    source_key: str
    source_kind: Literal["github_repository", "magicians_topic", "ethereum_ercs_pr"]
    source_locator: str
    updated_at: datetime
    metadata_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ScopedScanResult(StrictModel):
    observations: tuple[ScopedObservation, ...]
    github_cursors: dict[str, str | int | None]
    magicians_cursors: dict[str, str | int | None]
    failed_sources: tuple[str, ...]
```

Persist observations in `data/state/scoped-source-observations.json` and cursors in `SourceCursors`. Never stage external bodies under `data/github` or `data/magicians`. Include archived public repositories returned by the organization API.

- [ ] **Step 4: Add focused optional proposal-PR collection**

Fetch only the registered PR, issue comments, reviews, and review comments using bounded pagination. Hash normalized public metadata for change detection; do not scan the rest of `ethereum/ERCs`.

- [ ] **Step 5: Cut L2/L3 over without changing scheduler protocol**

Make `source_check()` run the scoped scanner. Keep `knowledge_refresh(cutoff)` for compatibility but remove `KnowledgeRefresh.run()` and legacy queue processing; it records a completion timestamp only. Keep L3 vault validation.

- [ ] **Step 6: Reuse scan targets for Daily evidence**

Build Magicians seeds from `ScanTargetRegistry`. Add focused optional proposal-PR activity to GitHub Daily evidence while retaining every public `trustless-ai` repository and current source-count budgets.

- [ ] **Step 7: Run scanner, scheduler, runtime, and Daily tests GREEN**

Run: `python -m pytest tests/integration/test_scoped_scanner.py tests/unit/test_scheduler.py tests/integration/test_runtime_composition.py tests/integration/test_daily_live_evidence.py -q`

- [ ] **Step 8: Commit**

```bash
git add src/tawg_bot/scoped_scanner.py src/tawg_bot/runtime.py src/tawg_bot/daily_evidence.py src/tawg_bot/scheduler.py tests/integration/test_scoped_scanner.py tests/unit/test_scheduler.py tests/integration/test_runtime_composition.py tests/integration/test_daily_live_evidence.py
git commit -m "feat: scope recurring source scans"
```

### Task 7: End-to-End Qualification, Reviews, and Publication

**Files:**
- Create: `tests/e2e/test_open_knowledge_rollout.py`
- Modify: `tests/support/runtime_repository.py`
- Modify: `docs/operator/runbook.md`
- Modify: `docs/operator/modal.md`
- Modify: `docs/superpowers/specs/2026-08-28-open-knowledge-mutation-and-scoped-scanning-design.md`

**Interfaces:**
- Consumes: all preceding controller, migration, registry, verifier, and scanner interfaces.
- Produces: offline webhook-to-delivery qualification and operator migration instructions.

- [ ] **Step 1: Write the failing end-to-end regression**

```python
@pytest.mark.asyncio
async def test_webhook_records_general_knowledge_and_registers_complete_erc(tmp_path: Path) -> None:
    runtime = offline_runtime_fixture(tmp_path)
    await runtime.ingest_webhook_envelope(
        envelope_for(6000, "@trustless_ai_bot record our Garden Clock concept in full"),
        now=NOW,
    )
    await runtime.ingest_webhook_envelope(
        envelope_for(
            6001,
            "@trustless_ai_bot record ERC-8183 and scan https://ethereum-magicians.org/t/erc-8183-agentic-commerce/27902",
        ),
        now=NOW + timedelta(seconds=1),
    )
    await runtime.maintenance_tick(NOW + timedelta(seconds=2), observe_only=False)
    assert (tmp_path / "knowledge/topics/garden-clock.md").is_file()
    assert ScanTargetStore(tmp_path).load().ercs[0].erc_number == 8183
    assert delivered_reply_targets(tmp_path) == {6000, 6001}
```

Add an offline production-state regression for the retained RVR jobs: the audited `tg:tawg:3470`
continuation creates the RVR page with its evidence, and the older `tg:tawg:3446` repair is marked
superseded instead of producing a duplicate write or another deterministic retry.

- [ ] **Step 2: Run `python -m pytest tests/e2e/test_open_knowledge_rollout.py -q` and verify RED**

- [ ] **Step 3: Make only integration corrections and run E2E GREEN**

Fix wiring, fixture completeness, schema-version consistency, and state-copy support. Do not add new policy branches.

- [ ] **Step 4: Update operator docs and mark spec implemented after verification**

Document `migrate-open-knowledge`, idempotency, audit path, scan registry, incomplete registration behavior, and rollback. State that workflow files are unchanged.

- [ ] **Step 5: Run full verification**

Run: `/usr/local/bin/ruff check src tests deploy`

Run: `python -m mypy src/tawg_bot`

Run: `python -m pytest -q`

Run: `PYTHONPATH=src python -m tawg_bot.cli vault-lint`

Run: `git diff --check`

Expected: Ruff clean, mypy clean, all tests pass, vault lint reports zero errors and warnings, and diff check is clean.

- [ ] **Step 6: Perform mandatory Python and security reviews**

Review correctness, test honesty, type safety, async boundaries, migration idempotency, SSRF, prompt-injection authority, URL validation, path confinement, stale SHA handling, external-body persistence, privacy, and scan-registry escalation. Every actionable finding starts with a failing regression, followed by the smallest production fix and full re-verification.

- [ ] **Step 7: Commit qualification**

```bash
git add tests/e2e/test_open_knowledge_rollout.py tests/support/runtime_repository.py docs/operator/runbook.md docs/operator/modal.md docs/superpowers/specs/2026-08-28-open-knowledge-mutation-and-scoped-scanning-design.md
git commit -m "test: qualify open knowledge rollout"
```

- [ ] **Step 8: Publish with GitHub MCP and monitor**

Publish verified commits to `main` through GitHub MCP. Verify remote contents, then read-only fetch and explicitly synchronize local state. Monitor qualifying Actions at that commit or newer. On failure: diagnose authenticated read-only logs, reproduce deterministically, add a failing regression, implement the smallest fix, repeat full verification and reviews, publish through GitHub MCP, and continue until Jimmy explicitly says to stop.
