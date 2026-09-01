# TAWG Dev Bot Mirror Design

Date: 2026-09-01

Status: proposed for implementation

## 1. Goal

Add a second, developer-facing TAWG bot ("dev bot") that runs the same Python core as production but against the `dev` branch, so new reply and routing logic can be exercised against a real Telegram group without touching production state or the canonical knowledge store.

The dev bot:

- reads historical Telegram context from `dev` (kept in sync with `main`);
- receives its own Telegram webhook updates through a separate bot account;
- replies in the same group using its own bot token;
- persists only its own webhook dedup receipts, keyed by bot identity;
- never writes canonical message history, knowledge, pending jobs, delivery state, or layer-success markers;
- never runs the Daily digest or the L1–L4 layer pipeline.

Production (`main`, app `tawg-production`) keeps its canonical single-writer role unchanged. `main` never merges another branch.

## 2. Non-goals

- Do not change production's canonical single-writer model, its receipt file, or its commit cadence.
- Do not make dev's reply delivery reliable under crash (a lost reply is acceptable); the correctness focus is on not duplicating sends.
- Do not make dev authoritative for knowledge or message history.
- Do not merge `dev` into `main` automatically.
- Do not trigger a dev deploy on every data or receipt sync; deploy only when code paths change.
- Do not use the dev bot's username as its identity key (usernames are mutable).
- Do not run paid reply models for the dev bot unless the operator explicitly enables a real dev reply test.

## 3. Approaches considered

### A. Reuse the production runtime unchanged on dev

Smallest change, but the dev bot and production bot would share one receipt file and one pending-job/delivery namespace keyed only by `tg:tawg:<message_id>`. Two bot accounts in the same group with independent `update_id` streams corrupt dedup and reply routing.

### B. Namespaced receipts plus bot-local reply state (selected)

Keep the canonical historical record (`tg:tawg:<message_id>`) shared, but make webhook dedup per bot and make reply-job identity bot-local. Dev persists only its own receipt; production is unchanged. This isolates the two bots without a second canonical store.

### C. Fully separate canonical store for dev

Give dev its own `data/telegram`, `data/state`, and `knowledge` trees on `dev`. Cleanest isolation but creates a second source of truth, complicates main↔dev sync, and duplicates the knowledge store for no operational benefit to a test bot.

## 4. Components

### 4.1 Bot identity

A stable numeric bot identity is derived from the bot account's numeric id (the prefix of the Telegram bot token, equivalent to `getMe().id`). It is never the username.

### 4.2 Per-bot webhook receipts

The webhook dedup file is namespaced:

```text
data/state/telegram-webhook-receipts.<bot_id>.json
```

Read rules:

- if the `<bot_id>` file exists, use it;
- else if the persistence mode is `full` (production) and the legacy `telegram-webhook-receipts.json` exists, load it and migrate to the namespaced file on the next successful production checkpoint;
- else start empty.

The legacy fallback is gated on persistence mode, not on a separately configured bot id: `full` is production (and therefore owns the legacy file), while `receipt-only` (dev) never falls back. The legacy unnamed file is retained only for the production migration and is otherwise no longer written.

### 4.3 Persistence modes

`TAWG_REPOSITORY_PERSIST_MODE` replaces the boolean gate, with values:

- `full` — current production behavior: commit allowlisted `data/**` and `knowledge/**` state, message history, and layer markers.
- `receipt-only` — dev: commit only (a) the main→dev merge commit and (b) the current bot's own receipt file.
- `none` — fully ephemeral observe mode (no commit at all).

The existing `TAWG_REPOSITORY_PERSIST_ENABLED` boolean is accepted for backward compatibility and maps to `full` (true) or `none` (false).

### 4.4 main→dev merge on dev entry

For any non-`main` branch, before processing a webhook or a scheduled sync, the worker fetches `origin/main` and merges it into the checked-out branch. The merge:

- uses `--no-edit` and no automatic conflict resolution;
- is treated as failure on any conflict (fail closed: no receipt write, no Telegram send);
- is committed and pushed as part of the receipt-only commit only when it produces changes.

`main` never performs this merge; the merge step is a no-op when `TAWG_MODAL_BRANCH == main`.

### 4.5 Dev webhook path

Dev webhook processing order:

1. clone `dev`;
2. if not `main`, merge `origin/main` (fail closed on conflict);
3. load the bot's namespaced receipt;
4. if `update_id` is already seen, acknowledge without replying;
5. stage the receipt with the new `update_id` and push it together with any merge commit;
6. compute the reply trigger from the envelope (mention, command, or reply-to-dev-bot) using the dev bot's username;
7. route and generate the reply in memory using the shared router and reply code;
8. send the Telegram reply using the dev bot's token and chat configuration;
9. discard all other local modifications when the temporary checkout is torn down.

Because the receipt is pushed before sending, a crash after push does not resend; a crash before push does not send. Replies can be lost, but should not be duplicated.

### 4.6 Bot-local reply identity

Reply jobs are no longer keyed solely by `tg:tawg:<message_id>`. In `receipt-only` mode the job and delivery identities are namespaced by bot id, for example:

```text
reply:<bot_id>:tg:tawg:<message_id>
```

Dev never reads production's `pending-bot-jobs.json` or `delivery-state.json` as authoritative for whether to reply. The reply decision is computed fresh from the current envelope. In particular, the existing `_message_supersedes_record` cross-bot `update_id` comparison must not gate reply-job creation in `receipt-only` mode: a message already persisted by production (whose canonical record carries production's `update_id`) must still produce a dev reply when the dev bot is mentioned.

### 4.7 Lightweight dev sync

A dev-only Modal schedule (every 30 minutes) performs a bare main→dev merge and push. It does not run the Daily digest, does not invoke the L1–L4 pipeline, does not call a model, and does not send Telegram. On `main` it exits immediately.

The per-webhook merge (4.4) still runs so an interactive dev message always sees the freshest `main` context; the schedule only keeps `dev` advancing while the dev bot is idle.

## 5. Data flow

```text
Telegram (dev bot) ──update──▶ dev web endpoint
                                  │ authenticate + normalize
                                  ▼
                         dev repository_worker
                                  │ clone dev → merge origin/main
                                  │ load receipts.<dev_bot_id>.json
                                  │ seen? ──yes──▶ ack, no reply
                                  │ no
                                  ▼
                         stage receipt + merge ──push──▶ dev
                                  │
                                  ▼
                         route + generate reply (in memory)
                                  │
                                  ▼
                         send Telegram reply (dev bot token)
```

Production flow is unchanged.

## 6. Ordering and idempotency

- Webhook dedup is by bot-namespaced `update_id`.
- The receipt is committed before the Telegram send; a retry after a successful push sees the `update_id` as seen and does not resend.
- A merge conflict aborts before the receipt is written and before any send, so a conflicted dev never replies.
- The canonical historical record `tg:tawg:<message_id>` remains the single shared key; only reply-job identity and receipt identity are namespaced.

## 7. Security and privacy

- The dev bot token, chat id, and username live in the dev workspace's own Modal secrets (`tawg-worker`, `tawg-webhook`, `tawg-maintenance`, `tawg-github-announcements`), never in production secrets.
- The dev `GITHUB_TOKEN` is scoped to push to `dev` only; it is not the production push credential.
- The bot identity key is the numeric id; the username is never persisted as an identity key.
- Numeric user and chat ids continue to follow the existing pseudonymization policy; no raw update is logged or persisted as a function input.

## 8. Failure handling

- Merge conflict: fail closed, no receipt write, no send; Modal may retry after the operator resolves `dev`.
- Receipt push failure: no send; Modal may retry.
- Post-push processing failure (routing, model, or send): the update is already receipted, so a retry skips it and the reply is lost (accepted for dev).
- Send succeeded but the worker crashed before returning: the receipt is already persisted, so no duplicate send.

## 9. Deployment and cutover

- The dev Modal app is `tawg-development`, deployed from `.github/workflows/modal-deploy-dev.yml` with `TAWG_MODAL_BRANCH=dev` and `TAWG_REPOSITORY_PERSIST_MODE=receipt-only`.
- The dev environment's `MODAL_TOKEN_*` point at the dev workspace (or the dev app in the shared workspace) with dev-bound secrets.
- A code change to `src/**`, `deploy/**`, or the workflow itself on `dev` triggers the dev deploy; receipt or data-only merges do not.
- Production (`tawg-production`, `main`) and its `full` mode are unchanged; the `TAWG knowledge bot` workflow remains `disabled_manually` and `Deploy TAWG Modal app` remains `active`.

## 10. Testing requirements

- Two bots with the same `update_id` each dedupe independently.
- A fresh dev bot never reads the production legacy receipt.
- Production migrates the legacy receipt to its namespaced file exactly once and then stops writing the legacy file.
- A message already persisted by production still creates a dev reply when the dev bot is mentioned.
- A dev crash after receipt push does not resend; a dev crash before receipt push sends nothing.
- A merge conflict writes no receipt and sends nothing.
- `main` never performs the merge step.
- A dev push contains no message-history, knowledge, delivery, or layer-success changes.
- A data or receipt-only sync does not trigger a dev deploy; a code sync does.

## 11. Operational acceptance criteria

- Production cadence, delivery, knowledge, and message-history stream are unchanged; the only production-side change is a one-time migration of the webhook receipt file from the legacy unnamed path to the namespaced path.
- The dev bot replies to a real mention in the shared group with its own identity, and produces exactly one ordinary reply per trigger (repair jobs remain separate).
- `dev` advances via the 30-minute sync and per-webhook merge without accumulating production `data/telegram` or `knowledge` diffs.
- Only `telegram-webhook-receipts.<dev_bot_id>.json` and merge commits appear as dev-originated non-code changes on the dev branch.
