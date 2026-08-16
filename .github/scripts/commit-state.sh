#!/usr/bin/env bash
# Commit the SQLite state file back to the repository.
#
# Rebases before pushing because several workflows share the state and can queue
# behind each other, and retries once because a concurrent push is the expected
# failure here rather than an exceptional one.
set -euo pipefail

MESSAGE="${1:-update state}"
STATE_PATH="state/ba_radar.sqlite"

git config user.name "ba-radar[bot]"
git config user.email "ba-radar[bot]@users.noreply.github.com"

if git diff --quiet -- "$STATE_PATH" 2>/dev/null && \
   ! git ls-files --others --exclude-standard --error-unmatch "$STATE_PATH" >/dev/null 2>&1; then
  echo "state unchanged; nothing to commit"
  exit 0
fi

git add "$STATE_PATH"
# [skip ci] keeps the state commit from triggering the CI workflow on every run.
git commit -m "chore(state): ${MESSAGE} [skip ci]"

for attempt in 1 2 3; do
  if git push; then
    echo "pushed on attempt ${attempt}"
    exit 0
  fi
  echo "push rejected; rebasing and retrying (attempt ${attempt})"
  git pull --rebase --autostash
  sleep 3
done

echo "could not push state after 3 attempts" >&2
exit 1
