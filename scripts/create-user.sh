#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Invite one user to the pool. An operator tool, not part of the application.
#
# Self sign-up is off, so this is the only way an account comes into existence.
# The invite carries a temporary password that Cognito emails; managed login then
# forces a change on first sign-in, so the user chooses their own password and
# nobody else ever knows it.
#
# Deliberately uses only AdminCreateUser. It never calls AdminSetUserPassword or
# AdminInitiateAuth, because a deployment that permits those turns "IAM admin on
# the pool" into "can silently obtain any user's tokens". This is an open-source
# template that customers deploy into their own accounts, so that capability is
# not something to enable anywhere, even for testing convenience.
#
# Usage: PROFILE=<aws-profile> scripts/create-user.sh <env> <email>

set -euo pipefail

# Required, with no default. A defaulted profile in a template that customers
# deploy is the same hazard as a defaulted URL: it silently targets whatever
# account that name happens to resolve to on the machine running it. Passed
# explicitly on every call rather than left to AWS_PROFILE, so an ambient
# environment variable can never redirect a deploy.
PROFILE="${PROFILE:-}"
if [ -z "$PROFILE" ]; then
  echo "ERROR: PROFILE is required." >&2
  echo "  eg: PROFILE=my-profile scripts/$(basename "$0") ..." >&2
  echo "  or: make <target> PROFILE=my-profile" >&2
  exit 1
fi

ENV_NAME="${1:-}"
EMAIL="${2:-}"

if [ -z "$ENV_NAME" ] || [ -z "$EMAIL" ]; then
  echo "usage: scripts/create-user.sh <env> <email>" >&2
  echo "   eg: scripts/create-user.sh dev someone@example.com" >&2
  exit 1
fi

# Transposing the two is the obvious mistake, and without this it would create a
# user named after the environment and mail an invite nowhere.
case "$EMAIL" in
  *@*.*) ;;
  *)
    echo "ERROR: '${EMAIL}' is not an email address." >&2
    echo "Argument order is <env> then <email>." >&2
    exit 1
    ;;
esac

case "$ENV_NAME" in
  *@*)
    echo "ERROR: '${ENV_NAME}' looks like an email. Argument order is <env> then <email>." >&2
    exit 1
    ;;
esac

# Every AWS call names the profile explicitly. Never the default profile.
echo "identity check"
aws sts get-caller-identity --profile "$PROFILE" --output text --query Account

POOL_PATH="/adlytics/${ENV_NAME}/auth/user-pool-id"
if ! POOL_ID=$(aws ssm get-parameter --name "$POOL_PATH" --profile "$PROFILE" \
  --query 'Parameter.Value' --output text 2>/dev/null); then
  echo "ERROR: $POOL_PATH does not exist. Deploy the auth stack first." >&2
  exit 1
fi

echo "inviting ${EMAIL} to ${POOL_ID}"

# email_verified is set so the user is not asked to verify an address they just
# received the invite on. Cognito generates and emails the temporary password;
# we never see it and never choose it.
#
# Re-running for an address that already exists (deploy-all resumed after a failure) sends
# nothing and succeeds. It never resends: a resend is a second AdminCreateUser mode this script
# deliberately does not use.
if ! created=$(aws cognito-idp admin-create-user \
  --user-pool-id "$POOL_ID" \
  --username "$EMAIL" \
  --user-attributes "Name=email,Value=${EMAIL}" "Name=email_verified,Value=true" \
  --desired-delivery-mediums EMAIL \
  --profile "$PROFILE" \
  --query 'User.[Username,UserStatus]' \
  --output text 2>&1); then
  case "$created" in
    *UsernameExistsException*)
      echo "${EMAIL} is already invited; no new invite sent."
      exit 0
      ;;
    *)
      echo "$created" >&2
      exit 1
      ;;
  esac
fi
echo "$created"

cat <<EOF

Done. ${EMAIL} is invited with status FORCE_CHANGE_PASSWORD.

Next:
  1. Check that inbox for the temporary password.
  2. Open the app, click Sign in.
  3. Managed login will require a new password. Set your own.

The account is usable from then on. Nothing here or in the deployed stack can
read or reset that password without the user going through the same flow.
EOF
