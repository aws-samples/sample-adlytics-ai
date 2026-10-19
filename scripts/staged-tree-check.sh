#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Prove the staged Lambda source tree is the committed one before a deploy.
#
#   staged-tree-check.sh <repo-root>
#
# Every tracked src/ and contracts/ path must be byte-equal in build/src (src/<p> stages as
# build/src/<p>, contracts/<p> as build/src/contracts/<p>) to its HEAD blob, and no staged .py file
# may exist without a HEAD path. Prints one MISMATCH or EXTRA line per difference, then
# `checked=<n> mismatches=<n> extra_py=<n> head=<sha>`, then STAGED_EQUAL or STAGED_DIFFERS.
#
# Exit codes: 0 STAGED_EQUAL; 81 STAGED_TREE_DIFFERS (including an absent build/src); 2 usage or a
# failed git read. Reads only local files and git; touches no AWS. Written for bash 3.2.
set -uo pipefail

[ "$#" -eq 1 ] && [ -n "$1" ] || { echo "USAGE: staged-tree-check.sh <repo-root>" >&2; exit 2; }
cd "$1" 2>/dev/null || { echo "READ_FAILED cannot enter $1" >&2; exit 2; }
head=$(git rev-parse --verify -q HEAD) || { echo "READ_FAILED git rev-parse HEAD in $1" >&2; exit 2; }
if [ ! -d build/src ]; then
  echo "checked=0 mismatches=0 extra_py=0 head=$head"
  echo "STAGED_DIFFERS build/src is absent; run make build-lambdas"
  exit 81
fi
tracked=$(git ls-files src contracts) || { echo "READ_FAILED git ls-files" >&2; exit 2; }
staged_py=$(find build/src -name '*.py' -not -path '*/__pycache__/*') || { echo "READ_FAILED find build/src" >&2; exit 2; }
tmp=$(mktemp "${TMPDIR:-/tmp}/staged-tree-check.XXXXXX") || { echo "READ_FAILED mktemp" >&2; exit 2; }
trap 'rm -f "$tmp"' EXIT

count=0
bad=0
while IFS= read -r path; do
  [ -n "$path" ] || continue
  staged="build/src/${path#src/}"
  git show "HEAD:$path" > "$tmp" || { echo "READ_FAILED git show HEAD:$path" >&2; exit 2; }
  if ! cmp -s "$tmp" "$staged"; then
    echo "MISMATCH $path"
    bad=$((bad + 1))
  fi
  count=$((count + 1))
done <<EOF
$tracked
EOF

extra=0
while IFS= read -r staged; do
  [ -n "$staged" ] || continue
  rel="${staged#build/src/}"
  case "$rel" in contracts/*) repo="$rel" ;; *) repo="src/$rel" ;; esac
  if ! git cat-file -e "HEAD:$repo" 2>/dev/null; then
    echo "EXTRA $staged"
    extra=$((extra + 1))
  fi
done <<EOF
$staged_py
EOF

echo "checked=$count mismatches=$bad extra_py=$extra head=$head"
if [ "$bad" -eq 0 ] && [ "$extra" -eq 0 ]; then
  echo STAGED_EQUAL
  exit 0
fi
echo STAGED_DIFFERS
exit 81
