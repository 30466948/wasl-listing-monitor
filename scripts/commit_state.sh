#!/bin/sh
# Commit state/ back to the default branch using the workflow's GITHUB_TOKEN.
# Pushes made with GITHUB_TOKEN never trigger other workflows, so no loop is possible;
# [skip ci] is kept as defence in depth.
set -eu
git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -A state/
if git diff --staged --quiet; then
  echo "state unchanged"
  exit 0
fi
git commit -q -m "chore(state): ${WASLMON_MODE:-monitor} run ${GITHUB_RUN_ID:-local} $(date -u +%FT%TZ) [skip ci]"
n=0
until git push; do
  n=$((n+1))
  if [ "$n" -ge 3 ]; then
    echo "::error::push failed after 3 attempts"
    exit 1
  fi
  sleep $((n*10))
  git pull --rebase --autostash -X theirs || { git rebase --abort || true; exit 1; }
done
echo "state committed and pushed"
