#!/usr/bin/env bash
set -euo pipefail

operation_id="${1:-}"
if [[ ! "$operation_id" =~ ^[A-Za-z0-9._:-]{1,180}$ ]]; then
  exit 2
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
trap 'rm -f "$paths_file"' EXIT
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
git push origin "HEAD:${GITHUB_REF_NAME:?GITHUB_REF_NAME is required}"
