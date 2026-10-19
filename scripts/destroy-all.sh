#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Destroy every stack of one environment, in reverse DEPLOY_ORDER, then report what survives.
#
# Usage: PROFILE=<aws-profile> ENV=<env> CONFIRM=<env> [INCLUDE_ACCOUNT_SETTINGS=1] \
#          scripts/destroy-all.sh
#        or: make destroy-all PROFILE=... ENV=... CONFIRM=... [INCLUDE_ACCOUNT_SETTINGS=1]
#
# Kept unless INCLUDE_ACCOUNT_SETTINGS=1: `Adlytics-Observability-<env>`, which holds the
# account-wide CloudWatch Transaction Search setting that every environment in the account shares.
#
# Retained resources (data buckets, tables, the user pool, conversation memory, prompt history,
# evaluation evidence) are deliberately not deleted by CloudFormation and keep billing. The last
# steps list any stack of this environment still present, every resource still tagged with it,
# and its retained Glue databases, which take no tags; the README's clean-up section says how to
# delete them. Exits non-zero while a stack remains.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { echo "ERROR: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

cli_profile="${PROFILE:-}"; cli_env="${ENV:-}"; cli_confirm="${CONFIRM:-}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
PROFILE="${cli_profile:-${PROFILE:-}}"
ENV_NAME="${cli_env:-${ENV:-}}"
CONFIRM="${cli_confirm:-}"
[ -n "$PROFILE" ] || fail "PROFILE is required."
[ "$PROFILE" != "default" ] || fail "PROFILE must name an explicit, non-default profile."
[ -n "$ENV_NAME" ] || fail "ENV is required."

ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)" \
  || fail "profile $PROFILE has no working credentials."
REGION="$(aws configure get region --profile "$PROFILE" || true)"
[ -n "$REGION" ] || fail "profile $PROFILE has no Region configured."
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

if [ "$CONFIRM" != "$ENV_NAME" ]; then
  echo "This destroys every stack of environment $ENV_NAME in account $ACCOUNT ($REGION and"
  echo "us-east-1). It cannot be undone. Retained data survives and must be deleted by hand."
  echo "To proceed:  make destroy-all ENV=$ENV_NAME PROFILE=$PROFILE CONFIRM=$ENV_NAME"
  exit 1
fi

step "Building Lambda packages (the app must synthesize to be destroyed)"
make build-lambdas

# The flags change only the frontend's template; destroy ignores them, but synth needs a value.
export ADLYTICS_API_ORIGIN_ATTACHED=true ADLYTICS_CONVERSATION_ORIGIN_ATTACHED=true
cdk() { (cd infra && npx cdk "$@" --profile "$PROFILE" -c env="$ENV_NAME"); }

targets=()
while IFS= read -r stack; do
  [ -n "$stack" ] || continue
  if [ "$stack" = "Adlytics-Observability-${ENV_NAME}" ] && [ "${INCLUDE_ACCOUNT_SETTINGS:-}" != "1" ]; then
    echo "keeping $stack (account-wide setting); pass INCLUDE_ACCOUNT_SETTINGS=1 to destroy it"
    continue
  fi
  targets+=("$stack")
done < <(cdk list 2>/dev/null)
[ "${#targets[@]}" -gt 0 ] || fail "the app composed no stacks for $ENV_NAME."

# S3 refuses to delete a non-empty bucket, and the Frontend's site bucket holds the built site.
# The stack carries no auto-delete helper (its CDK provider role would write any log group;
# ADR-085), so the bucket is emptied here, found by exact stack and logical resource. The bucket
# is not versioned; if it ever becomes versioned this step must delete every version as well.
frontend="Adlytics-Frontend-${ENV_NAME}"
for stack in "${targets[@]}"; do
  [ "$stack" = "$frontend" ] || continue
  step "Emptying the site bucket of $frontend"
  if ! resources="$(aws cloudformation describe-stack-resources --stack-name "$frontend" \
      --profile "$PROFILE" \
      --query "StackResources[?ResourceType=='AWS::S3::Bucket' && starts_with(LogicalResourceId, 'SiteBucket')].PhysicalResourceId" \
      --output text 2>&1)"; then
    case "$resources" in
      *"does not exist"*) echo "$frontend is not deployed; nothing to empty"; break ;;
      *) fail "could not read the resources of $frontend: $resources" ;;
    esac
  fi
  read -r -a buckets <<< "$resources"
  [ "${#buckets[@]}" -eq 1 ] || fail "expected one SiteBucket in $frontend, found ${#buckets[@]}."
  aws s3 rm "s3://${buckets[0]}" --recursive --profile "$PROFILE"
done

step "Destroying ${#targets[@]} stacks of $ENV_NAME"
# --exclusively: exactly these stacks; the CLI orders them in reverse dependency order.
cdk destroy "${targets[@]}" --exclusively --force

step "Stacks of $ENV_NAME still present"
remaining=0
for region in $(printf '%s\nus-east-1\n' "$REGION" | sort -u); do
  while IFS= read -r stack; do
    [ -n "$stack" ] || continue
    echo "$region: $stack"
    if [ "$stack" = "Adlytics-Observability-${ENV_NAME}" ] && [ "${INCLUDE_ACCOUNT_SETTINGS:-}" != "1" ]; then
      continue  # kept on purpose, above
    fi
    remaining=$((remaining + 1))
  done < <(aws cloudformation list-stacks --profile "$PROFILE" --region "$region" \
    --query "StackSummaries[?StackStatus!='DELETE_COMPLETE' && starts_with(StackName, 'Adlytics-') && ends_with(StackName, '-${ENV_NAME}')].StackName" \
    --output text | tr '\t' '\n')
done
[ "$remaining" -gt 0 ] || echo "none"

step "Resources still tagged Environment=$ENV_NAME (retained; review and delete by hand)"
for region in $(printf '%s\nus-east-1\n' "$REGION" | sort -u); do
  aws resourcegroupstaggingapi get-resources --profile "$PROFILE" --region "$region" \
    --tag-filters "Key=Application,Values=adlytics-ai" "Key=Environment,Values=$ENV_NAME" \
    --query 'ResourceTagMappingList[].ResourceARN' --output text | tr '\t' '\n'
done

# AWS::Glue::Database takes no tags, so the retained DataV2 databases (and their tables) never
# appear above. Their names are derived as in semanticV2DatabaseName (infra/lib/config/index.ts).
step "Glue databases of $ENV_NAME still present (retained, untagged; delete by hand)"
glue_found=0
for layer in source_aligned candidate current; do
  database="adlytics_${ENV_NAME//-/_}_v2_${layer}"
  if read_out="$(aws glue get-database --name "$database" --profile "$PROFILE" --region "$REGION" \
      --query Database.Name --output text 2>&1)"; then
    echo "$read_out"
    glue_found=$((glue_found + 1))
  else
    case "$read_out" in
      *EntityNotFoundException*) ;;
      *) fail "could not read Glue database $database: $read_out" ;;
    esac
  fi
done
[ "$glue_found" -gt 0 ] || echo "none"

if [ "$remaining" -gt 0 ]; then
  echo
  echo "$remaining stack(s) remain. A stack from a feature you have since turned off is not"
  echo "composed any more; delete it in the CloudFormation console or with"
  echo "aws cloudformation delete-stack --stack-name <name> --profile $PROFILE --region <region>."
  exit 1
fi
