#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Run one command under the single-deployer lock of a shared environment, or force-release a stale
# lock. Only one locked command runs at a time per lock file.
#
#   deploy-locked.sh --profile <P> --region <R> --expected-account <A> --env <E> \
#     --lock-file <F> --log-file <L> --targets-file <T> --source-sha <SHA> [--source-branch <B>] \
#     --label <X> [--poll-seconds <N>] [--bound-seconds <N>] -- <command> [args...]
#
#   deploy-locked.sh --force-release --profile <P> --region <R> --expected-account <A> --env <E> \
#     --lock-file <F> --log-file <L> --targets-file <T> --owner-pid <PID> --reason <TEXT>
#
# <T> lists the exact stacks the lock covers, one `<stack> <region>` per line: every app stack in
# its own Region (the edge stack is not in the application Region) and the CDK bootstrap stack
# (CDKToolkit) in each of those Regions, because a deploy may bootstrap there. `dev_operations.py
# lock-targets` writes it from the app's `cdk list --long --json` (make deploy-all-locked does).
# Each app stack ends in -<E>. Every stack read is `describe-stacks --stack-name` in that stack's
# Region; nothing lists the account. <R> is the application Region of the identity check. <F> and
# <L> are operator-chosen durable paths outside /tmp, so a host restart cannot drop the lock or its
# record.
#
# In order, run mode: creates the lock, complete, with one atomic link (75 if held). The lock
# records the pid, label, source and command, the account, environment and Region, and every
# target with the targets' sha256. It then checks the caller identity (76); the deploy source,
# HEAD = <SHA>, a clean tree and, when given, the branch (78); and that no target is in
# *_IN_PROGRESS (77). It exports ADLYTICS_DEPLOY_LOCK_HELD=<F> and forks the command's own process
# group, which waits at a gate until the lock also records `command_group=<pgid>` (renamed into
# place); a failure to record it stops the group before the command runs (2). Once the command ends, it waits until no process of that group remains and every
# target is out of *_IN_PROGRESS, then removes the lock. Each wait pass is one `release_poll` line
# in <L>. A read failure never counts as terminal: it keeps the lock and is logged with its error.
# Past --bound-seconds (default 3600) the lock is kept, LOCK_KEPT is logged and the exit is 84. A
# check that refuses before the command starts, or a lock record that cannot be read back, releases
# at once: the lock covered nothing.
#
# TERM and HUP are forwarded to the command's whole process group, and INT is forwarded as TERM.
# The command is waited for, then the release wait runs. A signal during the release wait is
# logged and forwarded to any process of the group still running; the wrapper polls once more and,
# if a process or a target is still running, keeps the lock (LOCK_KEPT, 84). The command's group is
# not the terminal's foreground group, so TTIN and TTOU are ignored for it: a terminal read fails
# (EIO) rather than stopping the command, which would leave the wrapper waiting with no record.
# A descendant that moves itself to another process group is outside this wait.
#
# SIGKILL skips all of this and leaves the lock: a stale lock is never released automatically,
# and the command's group, outside the wrapper's, can keep running. Force-release mode removes it
# only when the lock names --owner-pid and that process is gone (kill -0 reports no such process;
# any other failure refuses); no process of the command group
# the lock records is left (a lock that records none never started its command); the lock's recorded account, environment, Region and targets equal --expected-account, --env,
# --region and <T>; the identity check passes; and no recorded target is in progress. It logs
# FORCE_RELEASE_APPROVED with those checks, removes the lock, then logs FORCED_RELEASE. Otherwise
# it exits 83 (or 76, 77, 2) and changes nothing.
#
# The lock log is the custody record, so every append is checked; <L> must be writable at start
# (2). A failed append later is LOG_WRITE_FAILED on stderr with the record, and the run ends 85
# whatever else happened. The lock is the state the log can no longer show, so it stays: in run
# mode the command does not start once a record before it (`taken`, `targets`, `checks ok`) is
# missing, and once the lock is taken no failed record lets the wrapper remove it (LOCK_KEPT
# reason=log_write_failed); remove it with --force-release once <L> is writable again. Every
# removal follows a durable record that authorizes it (`release_approved`, the terminal
# `release_poll`, FORCE_RELEASE_APPROVED), so a force release whose approval cannot be written
# leaves the lock unchanged. Only the record after a removal (`released`, FORCED_RELEASE) can fail
# with the lock already gone; that still ends 85.
#
# Exit codes: the command's own code, or 128+n after a forwarded signal; 2 usage or read failure;
# 75 LOCK_HELD; 76 IDENTITY_MISMATCH; 77 STACK_IN_PROGRESS; 78 DEPLOY_SOURCE_MISMATCH;
# 83 FORCE_RELEASE_REFUSED; 84 LOCK_KEPT; 85 LOG_WRITE_FAILED (overrides every other code).
# Written for bash 3.2 (the macOS system bash).
set -uo pipefail

usage() {
  echo "USAGE: $1" >&2
  echo "see the header of scripts/deploy-locked.sh" >&2
  exit 2
}

utc_now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

BOOTSTRAP_STACK=CDKToolkit
PROFILE="" REGION="" ACCOUNT="" ENV_NAME="" LOCK="" LOG="" TARGETS_FILE="" SOURCE_SHA=""
SOURCE_BRANCH="" LABEL="" POLL=15 BOUND=3600 FORCE=0 OWNER_PID="" REASON=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    --force-release) FORCE=1; shift; continue ;;
    --profile|--region|--expected-account|--env|--lock-file|--log-file|--targets-file|--source-sha|\
    --source-branch|--label|--poll-seconds|--bound-seconds|--owner-pid|--reason)
      [ "$#" -ge 2 ] || usage "$1 needs a value"
      case "$1" in
        --profile) PROFILE="$2" ;;
        --region) REGION="$2" ;;
        --expected-account) ACCOUNT="$2" ;;
        --env) ENV_NAME="$2" ;;
        --lock-file) LOCK="$2" ;;
        --log-file) LOG="$2" ;;
        --targets-file) TARGETS_FILE="$2" ;;
        --source-sha) SOURCE_SHA="$2" ;;
        --source-branch) SOURCE_BRANCH="$2" ;;
        --label) LABEL="$2" ;;
        --poll-seconds) POLL="$2" ;;
        --bound-seconds) BOUND="$2" ;;
        --owner-pid) OWNER_PID="$2" ;;
        --reason) REASON="$2" ;;
      esac
      shift 2 ;;
    *) usage "unknown argument $1" ;;
  esac
done

[ -n "$PROFILE" ] || usage "--profile is required"
[ "$PROFILE" != "default" ] || usage "--profile must name an explicit nondefault profile"
[ -n "$REGION" ] || usage "--region is required"
[ -n "$ACCOUNT" ] || usage "--expected-account is required"
case "$ACCOUNT" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) usage "--expected-account must be a 12-digit account id" ;;
esac
[ -n "$ENV_NAME" ] || usage "--env is required"
[ -n "$LOCK" ] || usage "--lock-file is required"
[ -n "$LOG" ] || usage "--log-file is required"
[ -n "$TARGETS_FILE" ] || usage "--targets-file is required"
case "$POLL" in ''|*[!0-9]*) usage "--poll-seconds must be a whole number" ;; esac
case "$BOUND" in ''|*[!0-9]*) usage "--bound-seconds must be a whole number" ;; esac
[ "$POLL" -ge 1 ] || usage "--poll-seconds must be at least 1"
if [ "$FORCE" = 1 ]; then
  [ -n "$OWNER_PID" ] || usage "--owner-pid is required with --force-release"
  case "$OWNER_PID" in *[!0-9]*) usage "--owner-pid must be a process id" ;; esac
  [ -n "$REASON" ] || usage "--reason is required with --force-release"
  [ "$#" -eq 0 ] || usage "--force-release takes no command"
else
  [ -n "$SOURCE_SHA" ] || usage "--source-sha is required"
  [ -n "$LABEL" ] || usage "--label is required"
  [ "$#" -gt 0 ] || usage "a command after -- is required"
fi

# Every input is present; from here on external commands run.
[ -d "$(dirname "$LOCK")" ] || usage "the lock file's directory does not exist: $(dirname "$LOCK")"
[ -d "$(dirname "$LOG")" ] || usage "the log file's directory does not exist: $(dirname "$LOG")"
[ -f "$TARGETS_FILE" ] || usage "the targets file does not exist: $TARGETS_FILE"
: >> "$LOG" 2>/dev/null || usage "the log file is not writable: $LOG"

# parse_targets reads `<stack> <region>` lines on stdin into TARGET_STACKS, TARGET_REGIONS and
# TARGET_LINES, and sets TARGETS_SHA (the sha256 of the sorted lines). On a malformed line it
# sets PARSE_ERROR and returns 1.
parse_targets() {
  local line stack region extra seen
  TARGET_STACKS=() TARGET_REGIONS=() TARGET_LINES=() TARGETS_SHA="" PARSE_ERROR=""
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    stack="" region="" extra=""
    read -r stack region extra <<EOF
$line
EOF
    if [ -z "$region" ] || [ -n "$extra" ]; then
      PARSE_ERROR="a targets line is not '<stack> <region>': $line"; return 1
    fi
    case "$stack" in *[!A-Za-z0-9-]*) PARSE_ERROR="a targets line names no stack: $line"; return 1 ;; esac
    case "$region" in *[!a-z0-9-]*) PARSE_ERROR="a targets line names no Region: $line"; return 1 ;; esac
    case "$stack" in
      "$BOOTSTRAP_STACK"|*-"$ENV_NAME") ;;
      *) PARSE_ERROR="stack $stack does not end in -$ENV_NAME"; return 1 ;;
    esac
    for seen in ${TARGET_LINES[@]+"${TARGET_LINES[@]}"}; do
      [ "$seen" != "$stack $region" ] || { PARSE_ERROR="a target is listed twice: $line"; return 1; }
    done
    TARGET_STACKS+=("$stack")
    TARGET_REGIONS+=("$region")
    TARGET_LINES+=("$stack $region")
  done
  [ "${#TARGET_LINES[@]}" -gt 0 ] || { PARSE_ERROR="no target is listed"; return 1; }
  TARGETS_SHA=$(printf '%s\n' "${TARGET_LINES[@]}" | LC_ALL=C sort | shasum -a 256 | cut -d' ' -f1)
  case "$TARGETS_SHA" in
    [0-9a-f][0-9a-f][0-9a-f][0-9a-f]*) ;;
    *) PARSE_ERROR="the targets' sha256 could not be computed"; return 1 ;;
  esac
  return 0
}

targets_list() { (IFS=,; printf '%s' "${TARGET_LINES[*]}" | tr ' ' '@'); }

parse_targets < "$TARGETS_FILE" || usage "$PARSE_ERROR (in $TARGETS_FILE)"
SUPPLIED_SHA="$TARGETS_SHA"

ERR_FILE=$(mktemp "${TMPDIR:-/tmp}/deploy-locked-err.XXXXXX") || { echo "READ_FAILED mktemp" >&2; exit 2; }
# The command's start gate: created only once the lock records the command's process group.
GO_FILE="$ERR_FILE.go"

LOG_FAILED=0
log() {
  # Append one record line to the lock log and echo it to stderr. A failed append is reported on
  # stderr, sets LOG_FAILED and returns 1: from then on the run keeps the lock and ends 85.
  echo "$*" >&2
  if ! printf '%s\n' "$*" 2>/dev/null >> "$LOG"; then
    LOG_FAILED=1
    echo "LOG_WRITE_FAILED $(utc_now) log=$LOG record=${1%% *}" >&2
    return 1
  fi
  return 0
}

stop() {
  # stop <code>: exit with that code, or 85 when any lock-log record was not written.
  [ "$LOG_FAILED" = 0 ] || exit 85
  exit "$1"
}

aws_read() {
  # aws_read <region> <args...>: one bounded AWS CLI read in that Region; stdout is the result,
  # stderr lands in ERR_FILE.
  local region="$1"
  shift
  aws "$@" --profile "$PROFILE" --region "$region" --cli-connect-timeout 10 --cli-read-timeout 30 \
    < /dev/null 2> "$ERR_FILE"
}

first_error_line() {
  head -n 1 "$ERR_FILE" | tr -d '\r' | cut -c1-300
}

check_identity() {
  local got
  if ! got=$(aws_read "$REGION" sts get-caller-identity --query Account --output text); then
    log "READ_FAILED $(utc_now) label=$LABEL read=sts error=$(first_error_line)"
    return 2
  fi
  if [ "$got" != "$ACCOUNT" ]; then
    log "IDENTITY_MISMATCH $(utc_now) label=$LABEL expected=$ACCOUNT got=$got"
    return 76
  fi
  CALLER_ACCOUNT="$got"
  return 0
}

# poll_stacks sets POLL_STATE (terminal | in_progress | read_failed) and POLL_DETAIL. It reads
# each target in its own Region and stops at the first that is not terminal: one such target
# already holds the lock. A target that does not exist holds nothing.
poll_stacks() {
  local i stack region status
  POLL_STATE=terminal
  POLL_DETAIL="targets=${#TARGET_LINES[@]}"
  for i in "${!TARGET_LINES[@]}"; do
    stack="${TARGET_STACKS[$i]}"
    region="${TARGET_REGIONS[$i]}"
    if status=$(aws_read "$region" cloudformation describe-stacks --stack-name "$stack" \
        --query 'Stacks[0].StackStatus' --output text); then
      case "$status" in
        *[!A-Z_]*|'')
          POLL_STATE=read_failed
          POLL_DETAIL="$stack@$region: unexpected status output '$(printf '%s' "$status" | head -n 1 | cut -c1-80)'"
          return ;;
        *_IN_PROGRESS)
          POLL_STATE=in_progress
          POLL_DETAIL="$stack@$region=$status"
          return ;;
      esac
    elif grep -q 'does not exist' "$ERR_FILE"; then
      continue
    else
      POLL_STATE=read_failed
      POLL_DETAIL="$stack@$region: $(first_error_line)"
      return
    fi
  done
}

check_source() {
  local head want branch porcelain
  if ! head=$(git rev-parse --verify -q HEAD) || ! want=$(git rev-parse --verify -q "$SOURCE_SHA^{commit}"); then
    log "DEPLOY_SOURCE_MISMATCH $(utc_now) label=$LABEL reason=unresolved head_or_source=$SOURCE_SHA"
    return 78
  fi
  if [ "$head" != "$want" ]; then
    log "DEPLOY_SOURCE_MISMATCH $(utc_now) label=$LABEL reason=head head=$head source=$want"
    return 78
  fi
  if ! porcelain=$(git status --porcelain --untracked-files=all); then
    log "READ_FAILED $(utc_now) label=$LABEL read=git-status"
    return 2
  fi
  if [ -n "$porcelain" ]; then
    log "DEPLOY_SOURCE_MISMATCH $(utc_now) label=$LABEL reason=dirty_tree lines=$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')"
    return 78
  fi
  if [ -n "$SOURCE_BRANCH" ]; then
    branch=$(git symbolic-ref --short -q HEAD) || branch="(detached)"
    if [ "$branch" != "$SOURCE_BRANCH" ]; then
      log "DEPLOY_SOURCE_MISMATCH $(utc_now) label=$LABEL reason=branch branch=$branch source_branch=$SOURCE_BRANCH"
      return 78
    fi
  fi
  SOURCE_HEAD="$head"
  return 0
}

# --- force-release mode ------------------------------------------------------------------------

if [ "$FORCE" = 1 ]; then
  trap 'rm -f "$ERR_FILE"' EXIT
  LABEL="force-release"
  refuse() { log "FORCE_RELEASE_REFUSED $(utc_now) lock=$LOCK reason=$1"; stop 83; }
  [ -f "$LOCK" ] || refuse "no_lock_file"
  content=$(cat "$LOCK") || refuse "lock_unreadable"
  lock_pid=$(printf '%s\n' "$content" | sed -n -E 's/^pid=([0-9]+)( .*)?$/\1/p' | head -n 1)
  [ -n "$lock_pid" ] || refuse "lock_names_no_pid"
  [ "$lock_pid" = "$OWNER_PID" ] || refuse "owner_mismatch lock_pid=$lock_pid owner_pid=$OWNER_PID"
  # Only "no such process" proves the owner gone; any other failure (a pid this caller may not
  # signal, for one) proves nothing, and refuses.
  if owner_out=$(kill -0 "$lock_pid" 2>&1); then refuse "owner_alive pid=$lock_pid"; fi
  case "$owner_out" in
    *"No such process"*) ;;
    *) refuse "owner_unknown pid=$lock_pid error=\"$(printf '%s' "$owner_out" | head -n 1 | cut -c1-120)\"" ;;
  esac
  # The command runs in its own process group, which outlives a SIGKILLed wrapper. The lock
  # records that group before the command may start, so a lock without one ran no command.
  lock_group=$(printf '%s\n' "$content" | sed -n -E 's/^command_group=([0-9]+)$/\1/p' | head -n 1)
  if [ -n "$lock_group" ]; then
    if group_out=$(kill -0 -- -"$lock_group" 2>&1); then
      refuse "command_group_alive process_group=$lock_group"
    fi
    case "$group_out" in
      *"No such process"*) ;;
      *) refuse "command_group_unknown process_group=$lock_group error=\"$(printf '%s' "$group_out" | head -n 1 | cut -c1-120)\"" ;;
    esac
  fi
  # The lock's own record of what it covered, bound before any read: a different account,
  # environment, Region or target set cannot stand in for the one the abandoned command held.
  meta=$(printf '%s\n' "$content" | sed -n '/^account=/p' | head -n 1)
  [ -n "$meta" ] || refuse "lock_records_no_targets"
  field() { printf '%s\n' "$meta" | tr ' ' '\n' | sed -n "s/^$1=//p" | head -n 1; }
  lock_account=$(field account)
  lock_env=$(field env)
  lock_region=$(field region)
  lock_sha=$(field targets_sha256)
  [ "$lock_account" = "$ACCOUNT" ] || refuse "account_mismatch lock_account=$lock_account expected_account=$ACCOUNT"
  [ "$lock_env" = "$ENV_NAME" ] || refuse "env_mismatch lock_env=$lock_env env=$ENV_NAME"
  [ "$lock_region" = "$REGION" ] || refuse "region_mismatch lock_region=$lock_region region=$REGION"
  [ "$lock_sha" = "$SUPPLIED_SHA" ] || refuse "targets_mismatch lock_targets_sha256=$lock_sha targets_file_sha256=$SUPPLIED_SHA"
  recorded=$(printf '%s\n' "$content" | sed -n 's/^target=//p')
  parse_targets <<EOF || refuse "lock_targets_unreadable error=\"$PARSE_ERROR\""
$recorded
EOF
  [ "$TARGETS_SHA" = "$lock_sha" ] || refuse "lock_targets_corrupt recorded_sha256=$lock_sha lines_sha256=$TARGETS_SHA"
  check_identity; rc=$?
  [ "$rc" -eq 0 ] || stop "$rc"
  poll_stacks
  case "$POLL_STATE" in
    in_progress) log "STACK_IN_PROGRESS $(utc_now) lock=$LOCK $POLL_DETAIL"; stop 77 ;;
    read_failed) log "READ_FAILED $(utc_now) lock=$LOCK $POLL_DETAIL"; stop 2 ;;
  esac
  # Re-read just before removal: a different owner must not lose a lock taken since the check.
  [ "$(cat "$LOCK" 2>/dev/null)" = "$content" ] || refuse "lock_changed_during_checks"
  checks="sts=$CALLER_ACCOUNT in_progress=none owner_alive=false command_group=${lock_group:-none} command_group_alive=false targets=${#TARGET_LINES[@]} targets_sha256=$lock_sha"
  # The approval is durable before the lock goes: if it cannot be written, the lock stays (85).
  log "FORCE_RELEASE_APPROVED $(utc_now) pid=$lock_pid checks: $checks" || stop 85
  rm -f "$LOCK" || refuse "remove_failed"
  log "FORCED_RELEASE $(utc_now) pid=$lock_pid reason=\"$REASON\" checks: $checks lock_was=\"$(printf '%s' "$content" | head -n 1)\""
  stop 0
fi

# --- run mode ----------------------------------------------------------------------------------

# The complete record is written first and then linked into place: link(2) fails when the lock
# exists, so the lock is taken and its record readable in one step.
LOCK_TMP=$(mktemp "$(dirname "$LOCK")/.$(basename "$LOCK").XXXXXX") \
  || { echo "READ_FAILED mktemp beside $LOCK" >&2; rm -f "$ERR_FILE"; exit 2; }
if ! {
  printf 'pid=%s label=%s utc=%s source_sha=%s cmd=%s\n' "$$" "$LABEL" "$(utc_now)" "$SOURCE_SHA" \
    "$(printf '%s' "$*" | tr '\n' ' ')"
  printf 'account=%s env=%s region=%s targets=%s targets_sha256=%s\n' "$ACCOUNT" "$ENV_NAME" \
    "$REGION" "${#TARGET_LINES[@]}" "$TARGETS_SHA"
  printf 'target=%s\n' "${TARGET_LINES[@]}"
} > "$LOCK_TMP"; then
  echo "READ_FAILED cannot write $LOCK_TMP" >&2
  rm -f "$LOCK_TMP" "$ERR_FILE"
  exit 2
fi
if ! ln "$LOCK_TMP" "$LOCK" 2> "$ERR_FILE"; then
  rm -f "$LOCK_TMP"
  if [ -e "$LOCK" ]; then
    log "LOCK_HELD $(utc_now) label=$LABEL lock=$LOCK held_by=\"$(head -n 1 "$LOCK" 2>/dev/null)\""
    rm -f "$ERR_FILE"
    stop 75
  fi
  log "READ_FAILED $(utc_now) label=$LABEL lock=$LOCK reason=lock_create_failed error=$(first_error_line)"
  rm -f "$ERR_FILE"
  stop 2
fi
rm -f "$LOCK_TMP"

CHILD=""
CHILD_RC=""
SIGNAL_NAME=""
SIGNAL_CODE=""
RELEASING=0
RELEASE_SIGNAL=""
DONE=0

# group_alive: whether any process of the command's group remains. A kill -0 refused for any
# reason other than "no such process" counts as alive, with the error in GROUP_DETAIL.
group_alive() {
  local out
  GROUP_DETAIL=""
  if out=$(kill -0 -- -"$CHILD" 2>&1); then return 0; fi
  case "$out" in *"No such process"*) return 1 ;; esac
  GROUP_DETAIL=" error=$(printf '%s' "$out" | head -n 1 | cut -c1-120)"
  return 0
}

on_signal() {
  # on_signal <name> <exit code> <signal to forward>
  if [ "$RELEASING" = 1 ]; then
    RELEASE_SIGNAL="$1"
    if group_alive; then
      kill -"$3" -- -"$CHILD" 2>/dev/null
      log "signal_during_release $1 $(utc_now) label=$LABEL forwarded=$3 process_group=$CHILD"
    else
      log "signal_during_release $1 $(utc_now) label=$LABEL"
    fi
    return
  fi
  SIGNAL_NAME="$1"
  SIGNAL_CODE="$2"
  if [ -n "$CHILD" ] && group_alive; then
    kill -"$3" -- -"$CHILD" 2>/dev/null
    log "signal $1 $(utc_now) label=$LABEL forwarded=$3 process_group=$CHILD"
  else
    log "signal $1 $(utc_now) label=$LABEL child=none"
  fi
}
trap 'on_signal TERM 143 TERM' TERM
trap 'on_signal INT 130 TERM' INT
trap 'on_signal HUP 129 HUP' HUP

release_now() {
  # The command never started, so the lock covered nothing: remove it without the terminal wait,
  # once that decision is durable in the log.
  log "release_approved $(utc_now) label=$LABEL command=not_started reason=$1" || return 85
  rm -f "$LOCK" || { log "LOCK_KEPT $(utc_now) label=$LABEL reason=remove_failed"; return 84; }
  log "released $(utc_now) label=$LABEL waited_for_terminal=skipped reason=$1"
  return 0
}

release_after_command() {
  local start=$SECONDS waited sleeper
  RELEASING=1
  while :; do
    # A process of the command still running can still change a target: wait for it first.
    if group_alive; then
      POLL_STATE=command_running
      POLL_DETAIL="process_group=$CHILD$GROUP_DETAIL"
    else
      poll_stacks
    fi
    waited=$((SECONDS - start))
    # Each pass is durable before it decides anything: the terminal one authorizes the removal.
    # Any record missing so far (this pass, or a signal's) keeps the lock.
    log "release_poll $(utc_now) label=$LABEL waited=${waited}s result=$POLL_STATE $POLL_DETAIL"
    if [ "$LOG_FAILED" = 1 ]; then
      log "LOCK_KEPT $(utc_now) label=$LABEL reason=log_write_failed last=$POLL_STATE $POLL_DETAIL"
      return 85
    fi
    if [ "$POLL_STATE" = terminal ]; then
      if rm -f "$LOCK"; then
        log "released $(utc_now) label=$LABEL waited_for_terminal=${waited}s"
        return 0
      fi
      log "LOCK_KEPT $(utc_now) label=$LABEL reason=remove_failed"
      return 84
    fi
    if [ -n "$RELEASE_SIGNAL" ]; then
      log "LOCK_KEPT $(utc_now) label=$LABEL reason=signal_during_release signal=$RELEASE_SIGNAL last=$POLL_STATE $POLL_DETAIL"
      return 84
    fi
    if [ "$waited" -ge "$BOUND" ]; then
      log "LOCK_KEPT $(utc_now) label=$LABEL reason=bound bound=${BOUND}s last=$POLL_STATE $POLL_DETAIL"
      return 84
    fi
    # A background sleep and wait, so a signal is handled now rather than after the sleep.
    sleep "$POLL" &
    sleeper=$!
    wait "$sleeper" 2>/dev/null
    kill "$sleeper" 2>/dev/null
    wait "$sleeper" 2>/dev/null
  done
}

finish() {
  # finish <phase rc>: release by phase, then exit with the code the header documents.
  local rc="$1" release_rc
  DONE=1
  if [ "$LOG_FAILED" = 1 ]; then
    # A record this run needed is missing from the log: keep the lock for a force release, which
    # needs the log writable again, rather than end the custody the log cannot show.
    log "LOCK_KEPT $(utc_now) label=$LABEL reason=log_write_failed"
    release_rc=85
  elif [ -z "$CHILD" ]; then
    release_now "${2:-command_not_started}"; release_rc=$?
  else
    release_after_command; release_rc=$?
  fi
  rm -f "$ERR_FILE" "$GO_FILE"
  trap - EXIT
  # A record that failed after the removal (`released`) still ends 85.
  [ "$LOG_FAILED" = 0 ] || exit 85
  if [ "$release_rc" -ne 0 ]; then exit "$release_rc"; fi
  exit "$rc"
}

# Any exit not routed through finish (an unexpected error) still releases by the same rules.
trap '[ "$DONE" = 1 ] || finish 2 unexpected_exit' EXIT
# The link took the lock; its record is read back from it. A record that cannot be read starts
# nothing, and the lock goes as any unstarted run's does: only after a durable release_approved,
# so with the log unwritable it stays (85).
if ! LOCK_CONTENT=$(cat "$LOCK" 2> "$ERR_FILE"); then
  log "READ_FAILED $(utc_now) label=$LABEL lock=$LOCK reason=lock_unreadable error=$(first_error_line)"
  finish 2 lock_unreadable
fi

log "taken $(head -n 1 "$LOCK")"
log "targets label=$LABEL count=${#TARGET_LINES[@]} sha256=$TARGETS_SHA names=$(targets_list)"
[ "$LOG_FAILED" = 0 ] || finish 85 log_write_failed

check_identity; rc=$?
[ "$rc" -eq 0 ] || finish "$rc" identity_check_refused
check_source; rc=$?
[ "$rc" -eq 0 ] || finish "$rc" source_check_refused
poll_stacks
case "$POLL_STATE" in
  in_progress) log "STACK_IN_PROGRESS $(utc_now) label=$LABEL $POLL_DETAIL"; finish 77 stack_in_progress ;;
  read_failed) log "READ_FAILED $(utc_now) label=$LABEL $POLL_DETAIL"; finish 2 read_failed ;;
esac
if [ -n "$SIGNAL_NAME" ]; then
  log "command not started $(utc_now) label=$LABEL signal=$SIGNAL_NAME"
  finish "$SIGNAL_CODE" signal_before_command
fi
# The command starts only once the log shows why it may: without this record it does not run.
log "checks ok $(utc_now) label=$LABEL account=$CALLER_ACCOUNT source=$SOURCE_HEAD in_progress=none" \
  || finish 85 log_write_failed

record_command_group() {
  # Rewrite the lock as its record plus `command_group=<CHILD>`, renamed into place, so a force
  # release can see the group outlive a SIGKILLed wrapper. Sets RECORD_ERROR on failure.
  local tmp
  RECORD_ERROR=""
  [ "$(cat "$LOCK" 2>/dev/null)" = "$LOCK_CONTENT" ] || { RECORD_ERROR=lock_changed; return 1; }
  tmp=$(mktemp "$(dirname "$LOCK")/.$(basename "$LOCK").XXXXXX") || { RECORD_ERROR=mktemp; return 1; }
  if ! printf '%s\ncommand_group=%s\n' "$LOCK_CONTENT" "$CHILD" > "$tmp"; then
    rm -f "$tmp"; RECORD_ERROR=write; return 1
  fi
  if ! mv -f "$tmp" "$LOCK"; then rm -f "$tmp"; RECORD_ERROR=rename; return 1; fi
  return 0
}

export ADLYTICS_DEPLOY_LOCK_HELD="$LOCK"
# Job control is on for this one fork only, so the command leads its own process group: a
# forwarded signal reaches every descendant, and the release wait sees the last one leave. TTIN
# and TTOU stay ignored for it (the header says why). `<&0` keeps the caller's stdin. The group
# first waits at a gate: the command runs only once the lock records the group, and not at all if
# the wrapper is gone before that.
trap '' TTIN TTOU
WRAPPER_PID=$$
START_FAILED=0
set -m
(
  while [ ! -e "$GO_FILE" ]; do
    kill -0 "$WRAPPER_PID" 2>/dev/null || exit 125
    sleep 0.05
  done
  exec "$@"
) <&0 &
CHILD=$!
set +m
if ! record_command_group; then
  log "LOCK_RECORD_FAILED $(utc_now) label=$LABEL process_group=$CHILD reason=$RECORD_ERROR command=not_started"
  kill -TERM -- -"$CHILD" 2>/dev/null
  START_FAILED=1
elif [ -n "$SIGNAL_NAME" ]; then
  # A signal that landed before CHILD was set was not forwarded: stop the gated group instead.
  kill -TERM -- -"$CHILD" 2>/dev/null
  log "command not started $(utc_now) label=$LABEL signal=$SIGNAL_NAME process_group=$CHILD"
elif ! : > "$GO_FILE"; then
  log "READ_FAILED $(utc_now) label=$LABEL process_group=$CHILD reason=start_gate_unwritable gate=$GO_FILE command=not_started"
  kill -TERM -- -"$CHILD" 2>/dev/null
  START_FAILED=1
else
  log "command started $(utc_now) label=$LABEL child=$CHILD process_group=$CHILD"
fi
# wait returns early, with 128+n, when a trapped signal arrives. Above 128 is either that or the
# child's own signal status; once the child is gone one more wait returns its real status (bash
# keeps the status of an exited background job).
while :; do
  wait "$CHILD"
  CHILD_RC=$?
  [ "$CHILD_RC" -gt 128 ] || break
  if ! kill -0 "$CHILD" 2>/dev/null; then
    wait "$CHILD"
    CHILD_RC=$?
    break
  fi
done
log "command exit=$CHILD_RC $(utc_now) label=$LABEL"
if [ "$START_FAILED" = 1 ]; then finish 2; fi
if [ -n "$SIGNAL_NAME" ]; then finish "$SIGNAL_CODE"; fi
finish "$CHILD_RC"
