#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Run one gate into a log that names the exact tree it judged, or verify such a log.
#
#   gate-log.sh <log-file> <command> [args...]
#   gate-log.sh --verify <log-file> [--pin <sha>]
#
# A gate result is evidence only for the commit and working tree it ran on. A log without HEAD,
# tree and porcelain cannot say whether it judged the commit under review or a dirty tree beside
# it, so a review could cite a green run of different code. The header records HEAD, its tree, the
# branch and every porcelain line (untracked files included) before the command starts; the footer
# records the command's exit code and HEAD, its tree and the porcelain count after the command, so
# a gate that rewrites tracked source (a formatter, a generator) and exits 0 is not evidence for
# the tree sampled before it. The log's own `??` line is the one change left out of that count,
# when the log lives inside the checkout. Header and footer carry one per-run nonce, so a truncated
# log, or command output that imitates a footer, is not read as a finished run.
#
# Run mode refuses an existing log file (exit 2) rather than appending to or replacing another
# run's evidence, and exits with the command's own code.
#
# Verify mode exits 0 only for a well-formed log of a tree that was clean before and after a gate
# that passed at the pinned commit (default: the current HEAD). Otherwise it prints one
# GATE_LOG_REFUSED line and exits:
#   2 usage error, or a pin that does not resolve
#   3 the log's head differs from the pin
#   4 the tree was dirty when the gate ran, before or after the command
#   5 the gate failed (non-zero footer exit)
#   6 the log is missing, malformed or truncated (a GATE-LOG v1 log, which has no post-state, too)
#   7 HEAD or its tree changed while the gate ran, or git could not read the state after it
#
# Reads only local git state; touches no AWS. Written for bash 3.2 (the macOS system bash).
set -uo pipefail

usage() {
  echo "usage: gate-log.sh <log-file> <command> [args...]" >&2
  echo "       gate-log.sh --verify <log-file> [--pin <sha>]" >&2
  exit 2
}

refuse() {
  # $1 exit code, $2 reason, $3 detail
  echo "GATE_LOG_REFUSED $2 $3" >&2
  exit "$1"
}

utc_now() {
  date -u +%Y-%m-%dT%H:%M:%SZ
}

line_at() {
  # Print line $2 of file $1 (empty when the file is shorter).
  sed -n "${2}p" "$1"
}

run_gate() {
  local log="$1"
  shift
  [ "$#" -gt 0 ] || usage
  [ ! -e "$log" ] || refuse 2 LOG_EXISTS "$log"
  local log_dir
  log_dir=$(dirname "$log")
  [ -d "$log_dir" ] || refuse 2 LOG_DIR_MISSING "$log_dir"

  # The tree is read before the log file exists, so a log written inside the checkout does not
  # count itself as an untracked change.
  local head tree branch porcelain count nonce
  head=$(git rev-parse --verify -q HEAD) || refuse 2 NO_HEAD "git rev-parse HEAD failed in $(pwd)"
  tree=$(git rev-parse --verify -q 'HEAD^{tree}') || refuse 2 NO_TREE "git rev-parse HEAD^{tree} failed"
  branch=$(git symbolic-ref --short -q HEAD) || branch="(detached)"
  porcelain=$(git status --porcelain --untracked-files=all) || refuse 2 STATUS_FAILED "git status failed"
  if [ -z "$porcelain" ]; then
    count=0
  else
    count=$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')
  fi
  nonce=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')
  [ "${#nonce}" -eq 32 ] || refuse 2 NONCE_FAILED "could not read 16 bytes from /dev/urandom"

  # noclobber makes the create-or-refuse atomic: a concurrent run cannot share the file.
  ( set -C; : > "$log" ) 2>/dev/null || refuse 2 LOG_EXISTS "$log"
  {
    echo "GATE-LOG v2"
    echo "nonce=$nonce"
    echo "utc_start=$(utc_now)"
    echo "head=$head"
    echo "tree=$tree"
    echo "branch=$branch"
    echo "porcelain_count=$count"
    if [ "$count" -gt 0 ]; then
      printf '%s\n' "$porcelain" | sed 's/^/porcelain_line=/'
    fi
    printf 'command='
    printf '%q ' "$@"
    printf '\n'
  } >> "$log" || refuse 2 LOG_WRITE_FAILED "$log"

  # The log's own porcelain line, when the log lives inside the checkout: the one change this run
  # makes on purpose. Computed before the command, so the command cannot move it.
  local own=""
  own=$(own_porcelain_line "$log")

  "$@" >> "$log" 2>&1
  local rc=$?
  # A command whose output did not end in a newline would otherwise glue the footer to it.
  if [ -s "$log" ] && [ -n "$(tail -c 1 "$log")" ]; then
    printf '\n' >> "$log"
  fi

  # The state after the command, so a gate that rewrote tracked source (a formatter, a generator)
  # or moved HEAD and still exited 0 does not stand as evidence for the tree sampled before it.
  # A failed read is recorded as `unknown`, which verify refuses.
  local post_head post_tree post_porcelain post_count
  post_head=$(git rev-parse --verify -q HEAD) || post_head=unknown
  post_tree=$(git rev-parse --verify -q 'HEAD^{tree}') || post_tree=unknown
  if post_porcelain=$(git status --porcelain --untracked-files=all); then
    if [ -n "$own" ]; then
      post_porcelain=$(printf '%s\n' "$post_porcelain" | grep -vxF -- "$own")
    fi
    if [ -z "$post_porcelain" ]; then
      post_count=0
    else
      post_count=$(printf '%s\n' "$post_porcelain" | wc -l | tr -d ' ')
    fi
  else
    post_count=unknown
  fi

  echo "exit=$rc utc_end=$(utc_now) post_head=$post_head post_tree=$post_tree post_porcelain_count=$post_count nonce=$nonce" >> "$log" \
    || refuse 2 LOG_WRITE_FAILED "$log"
  echo "gate-log: $log exit=$rc head=$head porcelain_count=$count post_head=$post_head post_porcelain_count=$post_count" >&2
  if [ "$post_count" != 0 ] && [ "$post_count" != unknown ]; then
    printf '%s\n' "$post_porcelain" | sed 's/^/gate-log: post_porcelain_line=/' >&2
  fi
  exit "$rc"
}

own_porcelain_line() {
  # Print `?? <path>` for a log file inside this checkout, relative to its top level as porcelain
  # v1 prints it; print nothing for a log outside it. A path git would quote does not match, so
  # its line stays in the post-state and verify refuses it: fail closed, never a wider exclusion.
  local log="$1" top dir abs
  top=$(git rev-parse --show-toplevel 2>/dev/null) || return 0
  top=$(cd "$top" && pwd -P) || return 0
  dir=$(cd "$(dirname "$log")" && pwd -P) || return 0
  abs="$dir/$(basename "$log")"
  case "$abs" in
    "$top"/*) printf '?? %s\n' "${abs#"$top"/}" ;;
  esac
}

verify_log() {
  local log="" pin=""
  [ "$#" -ge 1 ] || usage
  log="$1"
  shift
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --pin)
        [ "$#" -ge 2 ] && [ -n "$2" ] || usage
        pin="$2"
        shift 2
        ;;
      *) usage ;;
    esac
  done

  if [ -z "$pin" ]; then
    pin=$(git rev-parse --verify -q HEAD) || refuse 2 NO_HEAD "no --pin and git rev-parse HEAD failed"
  else
    # Every pin resolves, a full-length SHA included: `^{commit}` needs the object to exist, so an
    # absent commit is an unresolved pin (2), not a head mismatch (3).
    local resolved
    resolved=$(git rev-parse --verify -q "$pin^{commit}") || refuse 2 PIN_UNRESOLVED "$pin"
    pin="$resolved"
  fi

  [ -f "$log" ] || refuse 6 LOG_MISSING "$log"
  [ -s "$log" ] || refuse 6 LOG_EMPTY "$log"
  # A complete log ends with the footer's newline; anything else was cut off mid-write.
  [ -z "$(tail -c 1 "$log")" ] || refuse 6 LOG_TRUNCATED "$log does not end in a newline"

  [ "$(line_at "$log" 1)" = "GATE-LOG v2" ] || refuse 6 LOG_MALFORMED "line 1 is not 'GATE-LOG v2'"
  local nonce head count
  nonce=$(line_at "$log" 2 | sed -n -E 's/^nonce=([0-9a-f]{32})$/\1/p')
  [ -n "$nonce" ] || refuse 6 LOG_MALFORMED "line 2 is not nonce=<32 hex>"
  line_at "$log" 3 | grep -Eq '^utc_start=[^[:space:]]+$' || refuse 6 LOG_MALFORMED "line 3 is not utc_start=<time>"
  head=$(line_at "$log" 4 | sed -n -E 's/^head=([0-9a-f]{40})$/\1/p')
  [ -n "$head" ] || refuse 6 LOG_MALFORMED "line 4 is not head=<40 hex>"
  local tree
  tree=$(line_at "$log" 5 | sed -n -E 's/^tree=([0-9a-f]{40})$/\1/p')
  [ -n "$tree" ] || refuse 6 LOG_MALFORMED "line 5 is not tree=<40 hex>"
  line_at "$log" 6 | grep -Eq '^branch=.+$' || refuse 6 LOG_MALFORMED "line 6 is not branch=<name>"
  count=$(line_at "$log" 7 | sed -n -E 's/^porcelain_count=([0-9]+)$/\1/p')
  [ -n "$count" ] || refuse 6 LOG_MALFORMED "line 7 is not porcelain_count=<n>"
  local i=0
  while [ "$i" -lt "$count" ]; do
    line_at "$log" $((8 + i)) | grep -q '^porcelain_line=' \
      || refuse 6 LOG_MALFORMED "porcelain_count=$count but porcelain line $((i + 1)) is absent"
    i=$((i + 1))
  done
  line_at "$log" $((8 + count)) | grep -q '^command=.' \
    || refuse 6 LOG_MALFORMED "line $((8 + count)) is not command=<argv>"

  local footer fields rc post_head post_tree post_count
  footer=$(tail -n 1 "$log")
  fields=$(printf '%s' "$footer" | sed -n -E \
    "s/^exit=([0-9]+) utc_end=[^[:space:]]+ post_head=([0-9a-f]{40}|unknown) post_tree=([0-9a-f]{40}|unknown) post_porcelain_count=([0-9]+|unknown) nonce=${nonce}\$/\1 \2 \3 \4/p")
  [ -n "$fields" ] || refuse 6 LOG_TRUNCATED "the last line is not this run's exit footer"
  read -r rc post_head post_tree post_count <<EOF
$fields
EOF

  [ "$head" = "$pin" ] || refuse 3 HEAD_MISMATCH "log head=$head pin=$pin"
  [ "$count" -eq 0 ] || refuse 4 DIRTY_TREE "porcelain_count=$count at gate time"
  [ "$post_head" = "$head" ] && [ "$post_tree" = "$tree" ] \
    || refuse 7 STATE_CHANGED "head=$head post_head=$post_head tree=$tree post_tree=$post_tree"
  [ "$post_count" != unknown ] || refuse 7 STATE_CHANGED "post_porcelain_count=unknown: git status failed after the gate"
  [ "$post_count" -eq 0 ] || refuse 4 DIRTY_TREE "post_porcelain_count=$post_count after the gate ran"
  [ "$rc" -eq 0 ] || refuse 5 GATE_FAILED "exit=$rc"
  echo "GATE_LOG_OK head=$head log=$log"
  exit 0
}

[ "$#" -ge 1 ] || usage
case "$1" in
  --verify)
    shift
    # ${1+"$@"}: bash 3.2 reads an empty "$@" as unbound under set -u.
    verify_log ${1+"$@"}
    ;;
  -*) usage ;;
  *) run_gate "$@" ;;
esac
