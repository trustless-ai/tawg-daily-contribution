#!/usr/bin/env bash
set -euo pipefail

operation_id="${1:-}"
if [[ ! "$operation_id" =~ ^[A-Za-z0-9._:-]{1,180}$ ]]; then
  exit 2
fi

persist_mode="${TAWG_REPOSITORY_PERSIST_MODE:-full}"
case "$persist_mode" in
  full) ;;
  receipt-only) ;;
  none) exit 0 ;;
  *) exit 7 ;;
esac

# Dev/observe workers commit only the merge commit and their own receipt.
if [[ "$persist_mode" == "receipt-only" ]]; then
  bot_id="${TAWG_BOT_ID:-}"
  if [[ ! "$bot_id" =~ ^[0-9]+$ ]]; then
    exit 6
  fi
  receipt_file="data/state/telegram-webhook-receipts.${bot_id}.json"
  if [[ -f "$receipt_file" ]]; then
    git add -- "$receipt_file"
  fi
  delivery_file="data/state/delivery-state.${bot_id}.json"
  if [[ -f "$delivery_file" ]]; then
    git add -- "$delivery_file"
  fi
  if ! git diff --cached --quiet; then
    git config user.name "TAWG Knowledge Bot"
    git config user.email "tawg-knowledge-bot@users.noreply.github.com"
    git commit -m "bot: checkpoint ${operation_id}"
  fi
  push_output_file="$(mktemp "${TMPDIR:-/tmp}/tawg-push.XXXXXX")"
  chmod 600 "$push_output_file"
  if LC_ALL=C git push --porcelain origin \
    "HEAD:${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}" >"$push_output_file" 2>&1; then
    exit 0
  fi
  if grep -Eq \
    '^![[:space:]]+[^[:space:]]+[[:space:]]+\[rejected\][[:space:]]+\((non-fast-forward|fetch first)\)$' \
    "$push_output_file"; then
    exit 75
  fi
  exit 1
fi

allowed_path() {
  local path="$1"
  case "$path" in
    data/*.json|data/*.jsonl|data/*.yml|data/*.yaml|data/*.md)
      return 0
      ;;
    knowledge/*.json|knowledge/*.yml|knowledge/*.yaml|knowledge/*.md)
      return 0
      ;;
    knowledge/.gitkeep)
      return 0
      ;;
    # Explicitly excluded control and contract roots: contracts/ .github/ config/ scripts/
    *)
      return 1
      ;;
  esac
}

paths_file="$(mktemp)"
push_output_file=""
cleanup() {
  rm -f -- "$paths_file"
  if [[ -n "$push_output_file" ]]; then
    rm -f -- "$push_output_file"
  fi
}
trap cleanup EXIT
git ls-files --modified --others --deleted --exclude-standard -z >"$paths_file"
git diff --cached --name-only -z >>"$paths_file"

while IFS= read -r -d '' path; do
  if ! allowed_path "$path"; then
    exit 3
  fi
  if [[ -L "$path" ]]; then
    exit 4
  fi
done <"$paths_file"

python -m tawg_bot.cli vault-lint
git add -- data knowledge

if git diff --cached --quiet; then
  exit 0
fi

while IFS= read -r -d '' path; do
  allowed_path "$path" || exit 5
done < <(git diff --cached --name-only -z)

git config user.name "TAWG Knowledge Bot"
git config user.email "tawg-knowledge-bot@users.noreply.github.com"
git commit -m "bot: checkpoint ${operation_id}"
push_output_file="$(mktemp "${TMPDIR:-/tmp}/tawg-push.XXXXXX")"
chmod 600 "$push_output_file"
if LC_ALL=C git push --porcelain origin \
  "HEAD:${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}" >"$push_output_file" 2>&1; then
  exit 0
fi
if grep -Eq \
  '^![[:space:]]+[^[:space:]]+[[:space:]]+\[rejected\][[:space:]]+\((non-fast-forward|fetch first)\)$' \
  "$push_output_file"; then
  exit 75
fi
exit 1
