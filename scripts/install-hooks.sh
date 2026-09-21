#!/bin/sh
# Point git at the versioned hooks. Run once per clone.
set -eu
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/* 2>/dev/null || true
echo "hooks installed (core.hooksPath=scripts/git-hooks)"
