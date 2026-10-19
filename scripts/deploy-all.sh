#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Deploy or update one complete environment: every stack the configuration composes, in
# DEPLOY_ORDER (infra/lib/deploy-order.ts), then the frontend, then optionally one invited user.
#
# Usage: PROFILE=<aws-profile> ENV=<env> [APPROVE=1] [EMAIL=<address>] scripts/deploy-all.sh
#        or: make deploy-all PROFILE=... ENV=... [APPROVE=1] [EMAIL=...]
#
# Without APPROVE=1 it changes nothing: it builds, then shows `cdk diff` for every stack with the
# same settings a deployment would use, and stops.
#
# Configuration comes from the environment, and from `.env` at the repository root when it
# exists (copy `.env.example`). PROFILE, ENV, APPROVE and EMAIL from the command line win over
# `.env`. Re-running after a failure resumes: CloudFormation skips stacks with no changes.
#
# What it decides for you, and how:
# - Bootstraps CDK in the profile's Region and in us-east-1 (the edge WAF Region) when either
#   has no CDKToolkit stack.
# - The frontend's two origin flags. When the API stack has not published its two parameters yet
#   (`/adlytics/<env>/api/id` and `/adlytics/<env>/api/conversation-function-url`), the
#   distribution is created without its /api origins and the frontend stack is deployed a second
#   time with both on once the API exists. A first API create that rolled back counts as not yet
#   deployed. When both exist, both flags are on from the start; leaving either off would detach a
#   live route. Any error reading them other than "not found" stops the run.
# - The invite. Re-running with the same EMAIL does not send a second invite.
#
# Not decided for you: an update to a new version may carry a one-time step between two stacks.
# Those are stated beside the stack in DEPLOY_ORDER (infra/lib/config/index.ts); read them before
# updating an existing environment.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

fail() { echo "ERROR: $*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

cli_profile="${PROFILE:-}"; cli_env="${ENV:-}"; cli_approve="${APPROVE:-}"; cli_email="${EMAIL:-}"
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi
PROFILE="${cli_profile:-${PROFILE:-}}"
ENV_NAME="${cli_env:-${ENV:-}}"
APPROVE="${cli_approve:-}"
EMAIL="${cli_email:-}"
[ -n "$PROFILE" ] || fail "PROFILE is required: make deploy-all PROFILE=<profile> ENV=<env>"
[ "$PROFILE" != "default" ] || fail "PROFILE must name an explicit, non-default profile."
[ -n "$ENV_NAME" ] || fail "ENV is required: make deploy-all PROFILE=<profile> ENV=<env>"

step "Checking local tools"
for tool in node npm uv aws file; do
  command -v "$tool" >/dev/null 2>&1 || fail "$tool is not installed (see README prerequisites)."
done
want_node="$(tr -d '[:space:]' < .nvmrc)"
have_node="$(node --version | sed -E 's/^v([0-9]+).*/\1/')"
[ "$have_node" = "$want_node" ] || fail "Node.js $want_node is required; found $(node --version)."

step "Checking the AWS target"
ACCOUNT="$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)" \
  || fail "profile $PROFILE has no working credentials."
REGION="$(aws configure get region --profile "$PROFILE" || true)"
[ -n "$REGION" ] || fail "profile $PROFILE has no Region configured."
# The CDK CLI prefers these over the profile's Region; pin them so every step targets one Region.
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
echo "account $ACCOUNT, Region $REGION, environment $ENV_NAME"

step "Installing dependencies"
make install
[ -d infra/node_modules ] || (cd infra && npm ci)
[ -d frontend/node_modules ] || (cd frontend && npm ci)

step "Building Lambda packages"
make build-lambdas
# Slice 7 deploy gate (D40): refuse a null write_unit_batching allowance before any diff or deploy.
step "Checking the execution-graph deploy gate"
make check-execution-graph-deployable

# The frontend's two API origins read exactly these two parameters, which only a deployed API stack
# writes. Both present is an existing deployment. A missing one (ParameterNotFound) means the API
# has not deployed yet, including a first create that rolled back and left the stack behind in
# ROLLBACK_COMPLETE. Any other error stops the run rather than guessing and detaching routes.
first_deployment=false
for api_parameter in "/adlytics/${ENV_NAME}/api/id" "/adlytics/${ENV_NAME}/api/conversation-function-url"; do
  if ! read_error="$(aws ssm get-parameter --name "$api_parameter" --profile "$PROFILE" \
      --region "$REGION" --query Parameter.Name --output text 2>&1 >/dev/null)"; then
    case "$read_error" in
      *ParameterNotFound*) first_deployment=true ;;
      *) fail "could not read $api_parameter: $read_error" ;;
    esac
  fi
done
export ADLYTICS_API_ORIGIN_ATTACHED ADLYTICS_CONVERSATION_ORIGIN_ATTACHED
if [ "$first_deployment" = true ]; then
  ADLYTICS_API_ORIGIN_ATTACHED=false ADLYTICS_CONVERSATION_ORIGIN_ATTACHED=false
else
  ADLYTICS_API_ORIGIN_ATTACHED=true ADLYTICS_CONVERSATION_ORIGIN_ATTACHED=true
fi
cdk() { (cd infra && npx cdk "$@" --profile "$PROFILE" -c env="$ENV_NAME"); }

if [ "$APPROVE" != "1" ]; then
  step "Changes a deployment would make (nothing is deployed)"
  cdk diff --all || true
  echo
  echo "To deploy:  make deploy-all PROFILE=$PROFILE ENV=$ENV_NAME APPROVE=1${EMAIL:+ EMAIL=$EMAIL}"
  exit 0
fi

step "Bootstrapping CDK where needed"
for bootstrap_region in $(printf '%s\nus-east-1\n' "$REGION" | sort -u); do
  if aws cloudformation describe-stacks --stack-name CDKToolkit --profile "$PROFILE" \
      --region "$bootstrap_region" >/dev/null 2>&1; then
    echo "$bootstrap_region: already bootstrapped"
  else
    cdk bootstrap "aws://${ACCOUNT}/${bootstrap_region}"
  fi
done

if [ "$first_deployment" = true ]; then
  step "Deploying every stack (first deployment: the frontend's API origins attach afterwards)"
else
  step "Deploying every stack (existing deployment: API origins stay attached)"
fi
cdk deploy --all --require-approval never --concurrency 1

if [ "$first_deployment" = true ]; then
  step "Attaching the API origins to the frontend"
  ADLYTICS_API_ORIGIN_ATTACHED=true ADLYTICS_CONVERSATION_ORIGIN_ATTACHED=true
  cdk deploy "Adlytics-Frontend-${ENV_NAME}" --exclusively --require-approval never
fi

step "Publishing the frontend"
make build-frontend ENV="$ENV_NAME" PROFILE="$PROFILE" REGION="$REGION" ACCOUNT="$ACCOUNT"

if [ -n "$EMAIL" ]; then
  step "Inviting $EMAIL"
  make create-user ENV="$ENV_NAME" EMAIL="$EMAIL" PROFILE="$PROFILE"
fi

URL="$(aws ssm get-parameter --name "/adlytics/${ENV_NAME}/frontend/public-base-url" \
  --profile "$PROFILE" --region "$REGION" --query Parameter.Value --output text)"
step "Done"
echo "Open $URL and sign in. A new user gets a temporary password by email."
[ -n "$EMAIL" ] || echo "No user was invited: make create-user ENV=$ENV_NAME EMAIL=<address> PROFILE=$PROFILE"
