#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Diff one stack, or deploy it only if its diff still equals the diff the operator read. Runs only
# under scripts/deploy-locked.sh.
#
#   diff-then-deploy.sh --profile <P> --env <E> --region <R> --expected-account <A> \
#     --stack <STACK> --evidence-dir <D> (--diff-only | --deploy <READ_DIFF_FILE>)
#
# Run from any directory; it works in the repository that holds it. <D> is an existing,
# operator-chosen durable directory outside /tmp. In order:
#   1. ADLYTICS_DEPLOY_LOCK_HELD must name a lock file whose pid is an ancestor of this process (79).
#   2. The tree is clean, untracked files included (78), recorded in <D>/<STACK>-clean-tree-<utc>.txt.
#   3. build/src equals HEAD (scripts/staged-tree-check.sh; 81).
#   4. The caller identity is the expected account (76).
#   5. `cdk diff <STACK> --exclusively` into <D>/<STACK>-diff-<utc>.log through scripts/gate-log.sh,
#      and the stack's synthesized asset keys through the probe runner's --record-assets.
# --diff-only stops here and prints the diff path. The operator reads that diff and records the read
# as `shasum -a 256 <diff> > <diff>.read`; that pair is the --deploy argument. --deploy continues:
#   6. <READ_DIFF_FILE>.read names the read file's sha256, and the stack section of the diff taken in
#      step 5 equals the one read (80).
#   7. The daily ingestion work queues hold no visible, in-flight or delayed message
#      (validation/dev_operations.py work-queues; 82).
#   8. `cdk deploy <STACK> --exclusively --require-approval never` into <D>/<STACK>-deploy-<utc>.log
#      through scripts/gate-log.sh, then the stack's describe-stacks and its events since the deploy
#      began are saved to <D>/<STACK>-stack-readback-<utc>.json.
#
# Exit codes: 0 done; 1 the deploy failed or the stack did not read *_COMPLETE after it; 2 usage or
# read failure; 76 IDENTITY_MISMATCH; 78 DEPLOY_SOURCE_MISMATCH (dirty tree); 79 NOT_UNDER_LOCK;
# 80 DIFF_CHANGED_SINCE_READ; 81 STAGED_TREE_DIFFERS; 82 WORK_IN_FLIGHT. Written for bash 3.2.
set -uo pipefail

usage() {
  echo "USAGE: $1" >&2
  echo "see the header of scripts/diff-then-deploy.sh" >&2
  exit 2
}

PROFILE="" ENV_NAME="" REGION="" ACCOUNT="" STACK="" EVIDENCE="" MODE="" READ=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --diff-only) MODE=diff-only; shift ;;
    --profile|--env|--region|--expected-account|--stack|--evidence-dir|--deploy)
      [ "$#" -ge 2 ] || usage "$1 needs a value"
      case "$1" in
        --profile) PROFILE="$2" ;;
        --env) ENV_NAME="$2" ;;
        --region) REGION="$2" ;;
        --expected-account) ACCOUNT="$2" ;;
        --stack) STACK="$2" ;;
        --evidence-dir) EVIDENCE="$2" ;;
        --deploy) MODE=deploy; READ="$2" ;;
      esac
      shift 2 ;;
    *) usage "unknown argument $1" ;;
  esac
done
[ -n "$PROFILE" ] || usage "--profile is required"
[ "$PROFILE" != "default" ] || usage "--profile must name an explicit nondefault profile"
[ -n "$ENV_NAME" ] || usage "--env is required"
[ -n "$REGION" ] || usage "--region is required"
[ -n "$ACCOUNT" ] || usage "--expected-account is required"
case "$ACCOUNT" in
  [0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) ;;
  *) usage "--expected-account must be a 12-digit account id" ;;
esac
[ -n "$STACK" ] || usage "--stack is required"
case "$STACK" in *[!A-Za-z0-9-]*) usage "--stack must be one stack name" ;; *-"$ENV_NAME") ;;
  *) usage "--stack must end in -$ENV_NAME" ;; esac
[ -n "$EVIDENCE" ] || usage "--evidence-dir is required"
[ -n "$MODE" ] || usage "one of --diff-only or --deploy <READ_DIFF_FILE> is required"

# 1. Under the lock: the named lock file's owner must be an ancestor of this process.
under_lock() {
  local lock="${ADLYTICS_DEPLOY_LOCK_HELD:-}" owner p hops=0
  [ -n "$lock" ] && [ -f "$lock" ] || return 1
  owner=$(sed -n -E 's/^pid=([0-9]+)( .*)?$/\1/p' "$lock" | head -n 1)
  [ -n "$owner" ] || return 1
  p=$$
  while [ -n "$p" ] && [ "$p" -gt 1 ] && [ "$hops" -lt 64 ]; do
    [ "$p" = "$owner" ] && return 0
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
    hops=$((hops + 1))
  done
  return 1
}
if ! under_lock; then
  echo "NOT_UNDER_LOCK ADLYTICS_DEPLOY_LOCK_HELD='${ADLYTICS_DEPLOY_LOCK_HELD:-}' does not name a lock held by an ancestor; run under scripts/deploy-locked.sh" >&2
  exit 79
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || { echo "READ_FAILED cannot enter $ROOT" >&2; exit 2; }
[ -d "$EVIDENCE" ] || usage "the evidence directory does not exist: $EVIDENCE"
if [ "$MODE" = deploy ]; then
  [ -f "$READ" ] || usage "the read diff file does not exist: $READ"
  [ -f "$READ.read" ] || usage "no read record $READ.read: record the read with shasum -a 256 <diff> > <diff>.read"
fi
# The synth configuration loads as scripts/deploy-all.sh loads it; the explicit inputs above win.
if [ -f .env ]; then
  saved="$PROFILE|$ENV_NAME|$REGION|$ACCOUNT|$STACK|$EVIDENCE|$MODE|$READ"
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
  IFS='|' read -r PROFILE ENV_NAME REGION ACCOUNT STACK EVIDENCE MODE READ <<EOF
$saved
EOF
fi
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
U=$(date -u +%Y%m%dT%H%M%SZ)
SHA=$(git rev-parse --verify -q HEAD) || { echo "READ_FAILED git rev-parse HEAD" >&2; exit 2; }

# 2. Clean tree, recorded with the same fields as a gate-log header.
CLEAN="$EVIDENCE/$STACK-clean-tree-$U.txt"
porcelain=$(git status --porcelain --untracked-files=all) || { echo "READ_FAILED git status" >&2; exit 2; }
{
  echo "utc=$U mode=$MODE stack=$STACK"
  echo "head=$SHA"
  echo "tree=$(git rev-parse 'HEAD^{tree}')"
  echo "branch=$(git symbolic-ref --short -q HEAD || echo '(detached)')"
  if [ -z "$porcelain" ]; then echo "porcelain_count=0"; else
    echo "porcelain_count=$(printf '%s\n' "$porcelain" | wc -l | tr -d ' ')"
    printf '%s\n' "$porcelain" | sed 's/^/porcelain_line=/'
  fi
} > "$CLEAN" || { echo "READ_FAILED cannot write $CLEAN" >&2; exit 2; }
if [ -n "$porcelain" ]; then echo "DEPLOY_SOURCE_MISMATCH dirty tree: $CLEAN" >&2; exit 78; fi

# 3. Staged tree.
staged=$(bash scripts/staged-tree-check.sh "$ROOT" 2>&1); rc=$?
printf '%s\n' "$staged" | tail -n 5 >> "$CLEAN"
case "$rc" in
  0) ;;
  81) echo "STAGED_TREE_DIFFERS: $CLEAN" >&2; exit 81 ;;
  *) echo "READ_FAILED staged-tree-check rc=$rc: $CLEAN" >&2; exit 2 ;;
esac

# 4. Identity.
got=$(aws sts get-caller-identity --profile "$PROFILE" --region "$REGION" --query Account --output text \
  --cli-connect-timeout 10 --cli-read-timeout 30 < /dev/null) || { echo "READ_FAILED sts get-caller-identity" >&2; exit 2; }
[ "$got" = "$ACCOUNT" ] || { echo "IDENTITY_MISMATCH expected=$ACCOUNT got=$got" >&2; exit 76; }

# 5. Diff and asset record.
DIFF="$EVIDENCE/$STACK-diff-$U.log"
bash scripts/gate-log.sh "$DIFF" bash -c 'cd infra && npx cdk diff "$1" --exclusively --profile "$2" -c env="$3"' \
  cdk-diff "$STACK" "$PROFILE" "$ENV_NAME"
rc=$?
[ "$rc" -eq 0 ] || { echo "READ_FAILED cdk diff rc=$rc: $DIFF" >&2; exit 2; }
assets=$(uv run python validation/probe_ingestion_traceability_gate.py --record-assets "$STACK" --evidence-dir "$EVIDENCE") \
  || { echo "READ_FAILED record-assets for $STACK" >&2; exit 2; }
echo "$assets"
echo "diff: $DIFF"
if [ "$MODE" = diff-only ]; then exit 0; fi

# 6. The diff taken now equals the diff read. The body is the stack section, without the gate-log
# footer (last line) and the timing lines.
body() { sed '$d' "$1" | sed -n '/^Stack /,$p' | grep -v '✨'; }
read_sha=$(shasum -a 256 "$READ" | cut -d' ' -f1)
grep -q "$read_sha" "$READ.read" || { echo "DIFF_CHANGED_SINCE_READ $READ.read does not name sha256 $read_sha of $READ" >&2; exit 80; }
[ -n "$(body "$DIFF")" ] || { echo "READ_FAILED the diff has no stack section: $DIFF" >&2; exit 2; }
if [ "$(body "$READ" | shasum -a 256)" != "$(body "$DIFF" | shasum -a 256)" ]; then
  echo "DIFF_CHANGED_SINCE_READ read=$READ now=$DIFF" >&2
  exit 80
fi

# 7. Work queues idle. The stack set, each stack with its own Region, is the cloud assembly step 5
# synthesized.
LISTING="$EVIDENCE/$STACK-cdk-list-$U.json"
(cd infra && npx cdk list --long --json --app cdk.out) > "$LISTING" \
  || { echo "READ_FAILED cdk list --long --json --app cdk.out" >&2; exit 2; }
uv run python validation/dev_operations.py work-queues --profile "$PROFILE" --env "$ENV_NAME" \
  --region "$REGION" --expected-account "$ACCOUNT" --evidence-dir "$EVIDENCE" --cdk-list "$LISTING"
rc=$?
case "$rc" in
  0) ;;
  82) echo "WORK_IN_FLIGHT: see the work-queues record in $EVIDENCE" >&2; exit 82 ;;
  *) echo "READ_FAILED work-queues rc=$rc" >&2; exit 2 ;;
esac

# 8. Deploy and read back.
BEGAN=$(date -u +%Y-%m-%dT%H:%M:%SZ)
DEPLOY_LOG="$EVIDENCE/$STACK-deploy-$U.log"
bash scripts/gate-log.sh "$DEPLOY_LOG" bash -c \
  'cd infra && npx cdk deploy "$1" --exclusively --profile "$2" -c env="$3" --require-approval never' \
  cdk-deploy "$STACK" "$PROFILE" "$ENV_NAME"
deploy_rc=$?
echo "deploy exit=$deploy_rc log=$DEPLOY_LOG"
READBACK="$EVIDENCE/$STACK-stack-readback-$U.json"
if ! stacks_json=$(aws cloudformation describe-stacks --profile "$PROFILE" --region "$REGION" \
    --stack-name "$STACK" --output json < /dev/null) \
  || ! events_json=$(aws cloudformation describe-stack-events --profile "$PROFILE" --region "$REGION" \
    --stack-name "$STACK" --output json < /dev/null \
    --query "StackEvents[?Timestamp>=\`$BEGAN\`].[Timestamp,LogicalResourceId,ResourceType,ResourceStatus,ResourceStatusReason]"); then
  echo "READ_FAILED stack readback for $STACK (deploy exit=$deploy_rc)" >&2
  exit 2
fi
printf '{"describe_stacks":%s,"events_since":"%s","events":%s}\n' "$stacks_json" "$BEGAN" "$events_json" > "$READBACK"
status=$(aws cloudformation describe-stacks --profile "$PROFILE" --region "$REGION" --stack-name "$STACK" \
  --query 'Stacks[0].StackStatus' --output text < /dev/null) || { echo "READ_FAILED stack status" >&2; exit 2; }
echo "readback: $READBACK status=$status"
[ "$deploy_rc" -eq 0 ] || exit 1
case "$status" in
  CREATE_COMPLETE|UPDATE_COMPLETE) exit 0 ;;
  *) echo "DEPLOY_NOT_COMPLETE $STACK reads $status" >&2; exit 1 ;;
esac
