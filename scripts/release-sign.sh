#!/bin/sh
# crabcab release-sign: build the archive, checksum the exact TAG contents, sign the manifest, verify.
# usage: scripts/release-sign.sh <tag>     (key at ~/.ssh/crabcab_release, or $CRABCAB_RELEASE_KEY)
set -eu
cd "$(dirname "$0")/.."

TAG="${1:?usage: release-sign.sh <tag>}"
KEY="${CRABCAB_RELEASE_KEY:-$HOME/.ssh/crabcab_release}"

# --- validate the tag before it ever touches a path (no rm -rf footgun) ---
printf '%s\n' "$TAG" | grep -Eq '^v[0-9]+\.[0-9]+\.[0-9]+$' \
  || { echo "invalid tag (want vMAJOR.MINOR.PATCH): $TAG" >&2; exit 2; }
case "$TAG" in
  *..*|*/*|*\\*) echo "unsafe tag: $TAG" >&2; exit 2 ;;
esac
git rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null \
  || { echo "tag does not exist: $TAG" >&2; exit 2; }

# --- canonical output root; refuse anything that escapes dist/ ---
DIST_ROOT="$(pwd)/dist"
OUT="$DIST_ROOT/$TAG"
case "$OUT" in "$DIST_ROOT"/*) ;; *) echo "refusing unsafe output path" >&2; exit 2 ;; esac

# --- refuse a dirty tree and an unsafe signing key ---
git diff --quiet && git diff --cached --quiet \
  || { echo "working tree or index is dirty" >&2; exit 2; }
[ -f "$KEY" ] || { echo "signing key missing: $KEY" >&2; exit 2; }
mode="$(stat -f '%Lp' "$KEY" 2>/dev/null || stat -c '%a' "$KEY")"
[ "$mode" = "600" ] || { echo "signing key must be mode 600 (is $mode): $KEY" >&2; exit 2; }

rm -rf "$OUT"
mkdir -p "$OUT"

# --- archive the exact tag, and hash every file from the tag (not the checkout) ---
ARCHIVE="$OUT/crabcab-$TAG.tar.gz"
git archive --format=tar.gz --prefix="crabcab-$TAG/" "$TAG" > "$ARCHIVE"
git ls-tree -r --name-only "$TAG" | LC_ALL=C sort | while IFS= read -r f; do
  git show "$TAG:$f" | shasum -a 256 | awk -v name="$f" '{print $1 "  " name}'
done > "$OUT/SHA256SUMS.files"
{ ( cd "$OUT" && shasum -a 256 "crabcab-$TAG.tar.gz" ); cat "$OUT/SHA256SUMS.files"; } > "$OUT/SHA256SUMS"
rm -f "$OUT/SHA256SUMS.files"

# --- sign, then verify locally before you trust/upload it ---
ssh-keygen -Y sign -f "$KEY" -n file "$OUT/SHA256SUMS" >/dev/null
echo "crabcab-release $(cut -d' ' -f1-2 RELEASE-PUBLIC-KEY.txt)" > "$OUT/allowed_signers"
ssh-keygen -Y verify -f "$OUT/allowed_signers" -I crabcab-release -n file \
  -s "$OUT/SHA256SUMS.sig" < "$OUT/SHA256SUMS" >/dev/null \
  || { echo "self-verify FAILED - do not publish" >&2; exit 1; }

echo "signed + self-verified $TAG in $OUT/:"
ls -1 "$OUT" | grep -v allowed_signers
echo "publish (draft first, then release): gh release create $TAG --draft $OUT/crabcab-$TAG.tar.gz $OUT/SHA256SUMS $OUT/SHA256SUMS.sig"
