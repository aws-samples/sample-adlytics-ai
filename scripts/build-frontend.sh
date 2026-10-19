#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Build the static export and publish it to the distribution's bucket.
#
# There is no configuration to inject. CloudFront serves the app and the API from
# one distribution, so the frontend calls relative paths and never names a host —
# which is why the localhost defect ADR-012 guards against cannot occur here at
# all rather than being validated against.
#
# What this still needs from SSM is where to put the files, which is why it runs
# after FrontendStack.
#
# Usage: PROFILE=<aws-profile> scripts/build-frontend.sh <env>

set -euo pipefail

# Required, with no default. A defaulted profile in a template that customers
# deploy is the same hazard as a defaulted URL: it silently targets whatever
# account that name happens to resolve to on the machine running it. Passed
# explicitly on every call rather than left to AWS_PROFILE, so an ambient
# environment variable can never redirect a deploy.
PROFILE="${PROFILE:-}"
REGION="${REGION:-}"
EXPECTED_ACCOUNT="${EXPECTED_ACCOUNT:-}"
if [ -z "$PROFILE" ]; then
  echo "ERROR: PROFILE is required." >&2
  echo "  eg: PROFILE=my-profile scripts/$(basename "$0") ..." >&2
  echo "  or: make <target> PROFILE=my-profile" >&2
  exit 1
fi
if [ -z "$REGION" ]; then
  echo "ERROR: REGION is required." >&2
  echo "  eg: REGION=us-east-1 scripts/$(basename "$0") ..." >&2
  exit 1
fi

if [ -z "$EXPECTED_ACCOUNT" ]; then
  echo "ERROR: EXPECTED_ACCOUNT is required." >&2
  echo "  eg: EXPECTED_ACCOUNT=123456789012 scripts/$(basename "$0") ..." >&2
  exit 1
fi

ENV_NAME="${1:-}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ -z "$ENV_NAME" ]; then
  echo "usage: scripts/build-frontend.sh <env>   (e.g. dev)" >&2
  exit 1
fi

# Every AWS call names the profile explicitly. Never the default profile, never
# ambient credentials.
echo "identity check"
ACTUAL_ACCOUNT=$(aws sts get-caller-identity \
  --profile "$PROFILE" \
  --region "$REGION" \
  --output text \
  --query Account)
if [ "$ACTUAL_ACCOUNT" != "$EXPECTED_ACCOUNT" ]; then
  echo "ERROR: caller account does not match EXPECTED_ACCOUNT." >&2
  exit 1
fi
echo "$ACTUAL_ACCOUNT"
API_STACK="Adlytics-Api-${ENV_NAME}"

read_param() {
  aws ssm get-parameter \
    --name "$1" \
    --profile "$PROFILE" \
    --region "$REGION" \
    --query 'Parameter.Value' \
    --output text 2>/dev/null || echo "MISSING"
}

BUCKET=$(read_param "/adlytics/${ENV_NAME}/frontend/bucket-name")
DISTRIBUTION=$(read_param "/adlytics/${ENV_NAME}/frontend/distribution-id")

missing=()
[ "$BUCKET" = "MISSING" ] && missing+=("/adlytics/${ENV_NAME}/frontend/bucket-name")
[ "$DISTRIBUTION" = "MISSING" ] && missing+=("/adlytics/${ENV_NAME}/frontend/distribution-id")

if [ ${#missing[@]} -gt 0 ]; then
  echo "ERROR: these parameters do not exist. Deploy FrontendStack first." >&2
  for path in "${missing[@]}"; do echo "  - $path" >&2; done
  exit 1
fi

echo "validating candidate and deployed presentation compatibility"
(
  cd "$ROOT"
  uv run python -m adapters.jobs.presentation_validation
  uv run python -m adapters.jobs.presentation_release_preflight \
    --profile "$PROFILE" \
    --region "$REGION" \
    --expected-account "$EXPECTED_ACCOUNT" \
    --env "$ENV_NAME" \
    --operation-id "presentation-release-preflight-${ENV_NAME}" \
    --stack "$API_STACK" \
    --trace
)

echo "building"
cd "$ROOT/frontend"
npm run build

echo "publishing to s3://${BUCKET}"
# Two passes, because the two kinds of file need opposite cache directives and S3
# carries Cache-Control per object.
#
# Without this the objects carry no Cache-Control at all, CloudFront applies its
# CachingOptimized default, and the browser falls back to heuristic freshness. Stale
# HTML then points at hashed chunk names that the --delete below has already purged,
# so the scripts 404, React never hydrates, and the page sits on its pre-render
# forever. A hard refresh fixes it, which is what makes it look like a redirect bug.
#
# --delete respects the filters, so each pass only considers the files it uploads and
# neither deletes the other's work.

# Content-hashed, so the name changes whenever the bytes do and it can be held for a
# year. immutable additionally stops a revalidation request on reload.
aws s3 sync out/ "s3://${BUCKET}/" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --delete \
  --exclude "*.html" \
  --exclude "*.txt" \
  --cache-control "public, max-age=31536000, immutable"

# Names are stable, so these must be revalidated every time or a deploy never
# reaches anyone already holding a copy. ETag makes the check a 304, not a download.
aws s3 sync out/ "s3://${BUCKET}/" \
  --profile "$PROFILE" \
  --region "$REGION" \
  --delete \
  --exclude "*" \
  --include "*.html" \
  --include "*.txt" \
  --cache-control "no-cache"

echo "invalidating ${DISTRIBUTION}"
# Everything, because a static export rewrites hashed asset names but keeps the
# HTML paths stable, and stale HTML referencing removed assets is a blank page.
aws cloudfront create-invalidation \
  --distribution-id "$DISTRIBUTION" \
  --paths '/*' \
  --profile "$PROFILE" \
  --region "$REGION" \
  --query 'Invalidation.Id' \
  --output text

echo "done"
