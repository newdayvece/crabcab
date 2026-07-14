#!/bin/sh
# crabcab release-check: run before tagging/publishing a release. zero dependencies.
# proves the tree is clean, the tests pass, and no credential/pii/path slipped into the source.
set -eu
cd "$(dirname "$0")/.."

echo "== offline selftest =="
python3 scripts/selftest.py >/dev/null && echo "  ok"

echo "== python compiles =="
python3 -m compileall -q scripts && echo "  ok"

echo "== working tree clean =="
if [ -n "$(git status --porcelain 2>/dev/null)" ]; then echo "  DIRTY - commit or stash first"; exit 1; fi
git diff --check && echo "  ok"

echo "== no credentials / pii / absolute home paths in tracked source =="
# pattern is built from fragments so this scanner never matches its OWN source line
home_re='/'"Users/"'|/home/[a-z]'
secret_re='Bearer[[:space:]]+[A-Za-z0-9._-]{8}|BEGIN [A-Z ]*PRIVATE KEY'
token_re='(access|refresh)[_-]?token[[:space:]]*[:=][[:space:]]*["'"'"'][^"'"'"']+'
scan_re="($home_re|$secret_re|$token_re)"
if git grep -nEi "$scan_re" -- 'scripts/*' '*.md' 'references/*' 2>/dev/null; then
  echo "  REVIEW the matches above"; exit 1
else
  echo "  clean"
fi

echo "== git authors across all history (should be only the anon release identity) =="
git log --all --format='  %an <%ae>' | sort -u

echo
echo "release-check passed. next: tag, then scripts/release-sign.sh <tag>"
