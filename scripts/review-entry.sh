#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Refuse to start a review unless it is bound to the exact commit, a clean tree, present evidence
# and gate logs of that commit.
#
#   review-entry.sh --pin <sha> [--branch <name>] [--evidence <path>]... [--gate-log <file>]...
#
# A review of a dirty tree, or against a pin behind the branch tip, judges code nobody will merge,
# and its verdict is then cited for the code that is. This check runs before a review starts and
# stops at the first failed condition, printing one REVIEW_ENTRY_REFUSED line:
#   2 usage error
#   3 the tree is dirty (porcelain, untracked files included, is non-empty)
#   4 --pin does not resolve, differs from HEAD, or differs from refs/heads/<branch>
#   5 an evidence path is missing or empty
#   6 a gate log fails `gate-log.sh --verify --pin <sha>`
# Success prints REVIEW_ENTRY_OK head=<sha>.
#
# Reads only local git state and the named files; touches no AWS. Written for bash 3.2.
set -uo pipefail

script_dir=$(cd "$(dirname "$0")" && pwd)

usage() {
  echo "usage: review-entry.sh --pin <sha> [--branch <name>] [--evidence <path>]... [--gate-log <file>]..." >&2
  exit 2
}

refuse() {
  # $1 exit code, $2 reason, $3 detail
  echo "REVIEW_ENTRY_REFUSED $2 $3"
  exit "$1"
}

pin=""
branch=""
# Newline-separated lists: bash 3.2 has no safe empty-array expansion under set -u.
evidence=""
gate_logs=""
nl='
'
while [ "$#" -gt 0 ]; do
  case "$1" in
    --pin | --branch | --evidence | --gate-log)
      [ "$#" -ge 2 ] && [ -n "$2" ] || usage
      case "$2" in *"$nl"*) usage ;; esac
      case "$1" in
        --pin) pin="$2" ;;
        --branch) branch="$2" ;;
        --evidence) evidence="$evidence$2$nl" ;;
        --gate-log) gate_logs="$gate_logs$2$nl" ;;
      esac
      shift 2
      ;;
    *) usage ;;
  esac
done
[ -n "$pin" ] || usage

porcelain=$(git status --porcelain --untracked-files=all) || refuse 3 STATUS_FAILED "git status failed in $(pwd)"
if [ -n "$porcelain" ]; then
  count=$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')
  first=$(printf '%s\n' "$porcelain" | head -n 1)
  refuse 3 DIRTY_TREE "porcelain_count=$count first='$first'"
fi

pinned=$(git rev-parse --verify -q "$pin^{commit}") || refuse 4 PIN_UNRESOLVED "$pin"
head=$(git rev-parse --verify -q HEAD) || refuse 4 NO_HEAD "git rev-parse HEAD failed"
[ "$pinned" = "$head" ] || refuse 4 PIN_NOT_HEAD "pin=$pinned head=$head"
if [ -n "$branch" ]; then
  tip=$(git rev-parse --verify -q "refs/heads/$branch") || refuse 4 BRANCH_MISSING "refs/heads/$branch"
  [ "$pinned" = "$tip" ] || refuse 4 PIN_NOT_BRANCH_TIP "pin=$pinned refs/heads/$branch=$tip"
fi

while IFS= read -r path; do
  [ -n "$path" ] || continue
  if [ -d "$path" ]; then
    [ -n "$(ls -A "$path")" ] || refuse 5 EVIDENCE_EMPTY "$path"
  elif [ -f "$path" ]; then
    [ -s "$path" ] || refuse 5 EVIDENCE_EMPTY "$path"
  else
    refuse 5 EVIDENCE_MISSING "$path"
  fi
done <<EOF
$evidence
EOF

while IFS= read -r log; do
  [ -n "$log" ] || continue
  verdict=$(bash "$script_dir/gate-log.sh" --verify "$log" --pin "$pinned" 2>&1) \
    || refuse 6 GATE_LOG_REJECTED "$log: $verdict"
done <<EOF
$gate_logs
EOF

echo "REVIEW_ENTRY_OK head=$head"
