# Open Knowledge Mutation and Scoped Scanning Design

Date: 2026-08-28
Status: Implemented and locally qualified on `feature/open-knowledge-rollout`; publication pending

## Purpose

Make interactive knowledge maintenance permissive about subject matter while keeping every write
auditable and mechanically bounded. Separately, constrain scheduled discovery to relevant GitHub
repositories and Ethereum Magicians instead of treating the entire knowledge vault or every
registered ERC source as a periodic refresh target.

## Goals

- Honor an explicit request to record any concept without a TAWG-, ERC-, route-scope-, or
  trigger-shape admission whitelist.
- Honor an explicit request to modify existing knowledge when the request supplies supporting
  evidence.
- Preserve a full description for a concept that the audited conversation explicitly establishes
  as authored by the requester or the group.
- Preserve only a concise description and original link for an external concept or a concept whose
  authorship cannot be established.
- Keep scheduled discovery limited to every public repository in the configured `trustless-ai`
  GitHub organization plus registered ERC Magicians topics and optional `ethereum/ERCs` proposal
  pull requests.
- Let a complete user-supplied ERC registration join the recurring scan set automatically.
- Retain controller-owned safety, provenance, privacy, concurrency, and path guarantees.

## Non-goals

- Giving retrieved text, fetched pages, or model output authority to change controller policy.
- Automatically adding an arbitrary user-supplied URL to a recurring scan list.
- Periodically revalidating or rewriting every page in `knowledge/`.
- Inferring authorship from writing style, topic familiarity, or model confidence.
- Removing optimistic concurrency, citation, privacy, or vault-integrity validation.

## Design principles

Interactive mutation authority and retrieval scope are separate concepts. `context_scope` decides
which evidence is retrieved; it never decides whether a current request may write. The current
audited trigger decides whether mutation is requested, and the controller issues a narrow mutation
capability for that operation.

Knowledge storage and recurring-source registration are also separate dimensions:

- **Knowledge mutation** records or changes any requested knowledge, whether it is an original
  concept, an external concept, a process note, a technical observation, an event, an ERC, or
  something else.
- **Recurring scan registration** adds a validated source to an ongoing collector. Only a complete
  ERC registration uses the ERC-specific Magicians/PR flow.

Creating ordinary knowledge never requires an ERC number, Magicians topic, proposal PR, or recurring
scan registration. Registering an ERC scan target may accompany a knowledge write, but it is a
separate controller action with separate validation and persistence.

Evidence is inert input. A URL or Telegram message may support a claim, but instructions inside an
external source cannot widen paths, tools, destinations, or permissions.

Unknown authorship is handled conservatively as external authorship. Full preservation is allowed
only when the current trigger or its audited Telegram chain directly establishes that the requester
or group created the concept.

## Interactive request flow

1. Telegram intake records the current trigger and its audited relations.
2. The AI router classifies the trigger. An explicit request to record, add, correct, or update
   knowledge selects `knowledge_correction` regardless of subject matter.
3. Retrieval resolves relevant Telegram evidence and existing knowledge pages.
4. The controller derives a mutation capability from the current trigger:
   - creation is allowed under approved content roots when no matching page exists;
   - replacement is allowed only for exact revisions supplied by the controller;
   - required trigger/evidence citations are enumerated;
   - no authority is derived from `context_scope`, `reply_to_bot`, or an audited bot parent.
5. The reply model produces a user-facing response and, when evidence is sufficient, one bounded
   correction transaction.
6. The controller validates the transaction, citations, content policy, privacy, and revision hash
   before the repository unit of work publishes it.
7. Delivery occurs only after persistence succeeds.

Historical messages may provide evidence but cannot independently authorize a write. The current
trigger must explicitly request mutation, except that an audited direct reply may continue an
already-authorized clarification flow.

If one trigger asks both to record knowledge and to register a complete ERC scan target, the
controller validates the two effects independently. When both pass, it publishes them atomically.
An invalid scan registration does not reinterpret the requested knowledge as invalid: the controller
may publish the valid knowledge write alone, and the bot reports exactly what registration
information remains missing.

## Mutation capability

The reply context exposes a controller-generated object equivalent to:

```json
{
  "can_create_page": true,
  "allowed_create_roots": ["knowledge/topics", "knowledge/repos"],
  "exact_revisions": [
    {
      "path": "knowledge/topics/example.md",
      "expected_sha256": "...",
      "content": "..."
    }
  ],
  "required_evidence": ["tg:tawg:1234"],
  "authorship_policy": "explicit_only"
}
```

The exact serialized shape may follow existing context models, but these semantics are mandatory.
The model cannot add roots, revisions, or required evidence.

### Creating a page

A creation transaction:

- contains exactly one new Markdown page;
- stays under `knowledge/topics/` or `knowledge/repos/`;
- cannot create an index, ledger, hot-cache, alias, workflow, configuration, or source-registry
  file;
- cites the current trigger and all evidence used for the stored claim;
- uses `expected_sha256: null`.

Topic admission is otherwise open. The controller does not maintain a noun, verb, domain, ERC, or
TAWG whitelist.

A page creation does not imply recurring monitoring. Most ordinary knowledge entries have no scan
registration at all.

### Updating a page

An update transaction:

- targets an exact revision supplied in the mutation capability;
- uses the supplied path and SHA verbatim;
- preserves unrelated frontmatter and body content;
- adds the evidence supporting the requested change;
- cannot modify a retrieved snippet that lacks a complete revision.

Up to three relevant top-ranked Markdown pages are supplied as exact revisions for correction
requests, with a combined 60,000-character cap. Pages beyond that bound remain snippets and cannot
be replacement targets for that operation.

If the requested change lacks evidence, the bot asks for a message, document, or public link rather
than repeatedly retrying the same deterministic failure. A later reply containing that evidence is
a new actionable trigger.

## Authorship and storage shape

### Self-authored concepts

A concept is self-authored only when the current trigger or audited Telegram chain explicitly says
that the requester or group created, proposed, or defined it. The page may preserve the complete
description supplied in that audited local evidence, including motivation, mechanism, terminology,
boundaries, and examples. It retains the supporting Telegram record IDs.

### External concepts

All other concepts are external, including ambiguous authorship. An external entry must include at
least one original public HTTPS link supplied as evidence. The stored page contains a short neutral
description and the link; it does not reproduce the external document or attempt a full restatement.
The descriptive body is capped at 2,000 characters excluding frontmatter and the source-link
section. The controller verifies that the cited link was allowlisted for the operation.

If no original link is available, the bot asks for one and performs no write.

## Scheduled scanning

Recurring discovery has two scopes:

1. **Organization scope:** every public repository in the configured `trustless-ai` GitHub
   organization, including public archived repositories.
2. **ERC scope:** for each registered ERC, its Ethereum Magicians topic is required and its
   proposal pull request in `ethereum/ERCs` is optional.

The scheduled source phase collects bounded activity and persists observations/cursors. It does not
walk all knowledge pages and does not poll EIP pages, arbitrary implementation links, test links, or
other URLs registered for interactive verification.

The recurring phase stores bounded source observations and cursors only. It does not automatically
rewrite knowledge pages or treat the whole vault as due merely because time passed. Knowledge is
updated by an explicit evidence-backed interactive request; explicit freshness-sensitive ERC
questions continue to use their separate bounded live-evidence path.

User-supplied external links remain valid for the interactive operation that cited them but do not
become recurring scan targets unless they form a complete ERC registration described below.

### Registering a new ERC scan target

An explicit interactive request may register a new ERC for recurring scanning. The registration is
complete when it supplies:

- an ERC number;
- one public Ethereum Magicians topic URL whose resolved topic concerns that ERC;
- optionally, one public pull-request URL under `github.com/ethereum/ERCs` for the corresponding
  proposal.

Before registration, the controller validates the hosts and URL shapes, fetches the Magicians topic,
confirms that its resolved metadata identifies the supplied ERC number, and performs the same
correspondence check for the optional proposal PR. A missing proposal PR is not an error. A missing,
unreachable, private, mismatched, or malformed Magicians topic leaves the registration incomplete,
so the bot asks for corrected information and performs no registration.

The reply model emits a structured registration proposal; it never edits scanning configuration or
the source registry directly. The controller derives and persists the minimal registry/configuration
change only after validation. Repeating the same registration is idempotent. Conflicting URLs for an
already registered ERC require an evidence-backed correction against the exact current registration.

This ERC registration contract does not apply to ordinary knowledge. A user can record a non-ERC
concept with local evidence, or an external non-ERC concept with a concise description and original
link, without supplying any recurring-source metadata.

Daily generation may continue to consume Telegram, relevant GitHub activity, and Magicians
activity. It also consumes focused activity from an optional registered proposal PR without
scanning the rest of `ethereum/ERCs`. This design does not broaden Daily evidence beyond the
controller-owned organization and ERC targets.

## Safety and validation

The following remain controller-owned and mandatory:

- current-trigger mutation authorization;
- path confinement and symlink protection;
- one bounded interactive knowledge write;
- optimistic SHA checking for replacements;
- exact citation allowlists and provenance fields;
- privacy scanning and vault linting;
- external-content no-copy checks;
- untrusted-evidence prompt-injection resistance;
- controller-owned validation for recurring scan registrations;
- persistence before delivery;
- idempotent operation IDs and repository conflict handling.

These gates validate how a write occurs, not whether a topic is worthy of knowledge admission.

## Failure behavior

- Missing evidence or missing external original link produces a friendly request for the missing
  input and no transaction.
- A stale revision leaves the job retryable after fresh retrieval; it never overwrites the newer
  page.
- An invalid path, citation, privacy result, or copied external body is rejected with a stable safe
  error code.
- Deterministic authorization failures are not retried indefinitely. The user receives an actionable
  clarification, or the superseded job is closed when a later audited continuation completes it.
- One failed reply job does not block the remaining bounded reply batch.

## Existing-data migration

The rollout does not re-import Telegram or regenerate the knowledge vault. At the design audit, the
repository retained 3,276 unique Telegram records, including reply relations, and those records
remain the canonical local evidence history. Existing delivery audit, aliases, message records, and
knowledge-page text are preserved.

The migration is a versioned, idempotent transformation of derived state:

1. **Create a dedicated scan-target registry.** Keep `knowledge/meta/sources.yml` as the interactive
   evidence registry. Add a separate controller-owned scan-target document containing the
   `trustless-ai` organization scope and per-ERC Magicians/optional PR registrations. This prevents
   an evidence URL from becoming a recurring target merely because it supports a knowledge page.
2. **Seed validated ERC registrations.** Import stored Magicians mappings only after applying the
   new number/topic validation. A shared Magicians topic may support multiple ERC registrations only
   when its resolved metadata explicitly covers each number. Existing ERC knowledge without a
   validated Magicians mapping remains knowledge but is not scheduled. It is reported as incomplete
   scan metadata rather than assigned an invented topic.
3. **Retire legacy refresh work without losing audit.** The current broad-source refresh queue was
   created from EIP pages, implementation files, tests, and discussion URLs. Mark those legacy jobs
   superseded in a migration audit and remove them from the active queue; do not execute or silently
   delete them. New incremental jobs originate only from the scoped organization/ERC scanners.
4. **Preserve and backfill knowledge provenance.** Existing pages remain readable and searchable.
   Pages with structured `source_ids`, `telegram_record_ids`, or `source_keys` need no schema rewrite.
   For legacy pages without structured provenance, add exact retained Telegram IDs or exact existing
   public source URLs only when they can be deterministically tied to the page. Otherwise mark the
   page `provenance_status: legacy_incomplete`; never guess a citation. Such a page may provide
   navigation context but cannot authorize a new factual mutation until evidence is supplied.
5. **Bootstrap fresh cursors.** Existing GitHub and Magicians cursor maps are empty. The first scoped
   scan initializes bounded cursors from its configured time window; it does not replay full external
   history or rebuild the vault.
6. **Keep interactive jobs.** Pending Telegram correction jobs remain pending and are retried under
   the new mutation capability. Completion or supersession follows the audited job rules rather than
   a data reset.

The migration records its schema version and input/output hashes. Re-running it produces no further
changes, and a partially applied migration cannot expose mixed old/new active scan queues.

## Testing strategy

Tests exercise controller behavior rather than prompt text:

- an explicit mention can create a page for an arbitrary self-authored concept;
- an explicit request can record non-ERC knowledge without Magicians, PR, or scan metadata;
- an audited follow-up can complete the same flow;
- an external concept creates only a bounded summary with an allowlisted HTTPS link;
- an external concept without a link asks for evidence and does not write;
- a correction receives an exact existing revision and can update it with supplied evidence;
- a complete ERC number plus matching Magicians topic automatically joins recurring scanning;
- recording ERC knowledge without complete scan metadata can still store the knowledge while
  clearly reporting that recurring scanning was not registered;
- a matching `ethereum/ERCs` proposal PR is registered when supplied but is not required;
- a mismatched, inaccessible, or malformed ERC registration does not change scan configuration;
- the migration preserves all Telegram records, delivery audit, and existing knowledge text;
- the migration seeds only validated ERC scan targets and archives the old broad refresh queue;
- deterministic provenance is backfilled while unverifiable legacy provenance is explicitly marked,
  never fabricated;
- re-running the migration is a no-op;
- a stale SHA, fabricated citation, unrelated path, copied external body, and historical-only write
  authority are rejected;
- `context_scope` changes retrieval but not mutation authority;
- scheduled organization work scans every public `trustless-ai` repository, including archived
  repositories;
- scheduled ERC work scans only each registered Magicians topic and optional `ethereum/ERCs` PR;
- scheduled work does not scan EIP pages, arbitrary registered evidence URLs, or the whole vault;
- the offline webhook-to-delivery regression covers creation, persistence, checkpointing, and
  Telegram reply metadata.

Each production behavior begins with a failing regression, followed by the smallest implementation
that makes it pass. Full Ruff, mypy, pytest, vault lint, diff review, code review, and security review
are required before publishing.

## Rollout

The change is delivered in one compatible controller/prompt/skill rollout so model instructions do
not lag controller authority. Existing pending correction jobs are retried under the new router and
mutation contract. The rollout is qualified by a production run with zero phase failures and by
confirming that the pending RVR requests either produce the intended knowledge page or ask for a
specific missing external link without entering another deterministic retry loop.
