#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Run `make verify`, `make check`, `make infra-test` and the frontend gates in a clean export
# of HEAD.
#
# A working tree can pass the gates because of a file that is ignored, untracked, or only
# present locally, and a clean clone would then fail. This exports exactly what HEAD tracks
# into a temporary directory and runs the gates there, so a publication push is verified
# against what a reader will actually clone. Run it before every publication push.
#
# The tracked-file gates (check-publication-boundary, check-links) enumerate with git and skip
# when there is no repository, so the export is indexed first; a skip line fails the run.
set -euo pipefail

root=$(git rev-parse --show-toplevel)
cd "$root"

if [ -n "$(git status --porcelain)" ]; then
  echo "warning: the working tree has uncommitted changes; they are not verified." >&2
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

git archive HEAD | tar -x -C "$tmp"

cd "$tmp"
# A reader's shell carries no ADLYTICS_* deploy flags, and the operator's would change the
# configuration surface the gates synthesize (config.test.ts refuses online evaluation outside dev),
# so the export is judged on tracked content alone.
for name in $(compgen -e); do
  case "$name" in ADLYTICS_*) unset "$name" ;; esac
done
test -z "$(compgen -e | grep '^ADLYTICS_' || true)" || { echo "verify-clean: an ADLYTICS_* variable survived" >&2; exit 1; }
# The tracked-file gates enumerate with git; index exactly what HEAD tracked so they scan the export
# instead of skipping. This is a throwaway repository in the temporary directory, never the checkout.
git init -q
git add -A
test "$(git ls-files | wc -l)" -gt 0 || { echo "verify-clean: the export indexed no files" >&2; exit 1; }
# validation/dev_operations.py records HEAD and its tree, so its tests need a born HEAD. The
# identity is passed per command so a reader without a configured git identity can still run this.
git -c user.name=verify-clean -c user.email=verify-clean@invalid -c commit.gpgsign=false \
  commit -q -m "verify-clean export"
make install
make verify
# make check reads aws-cdk-lib and ts-node from infra/node_modules (check-construct-levels,
# check-parameter-contract), so the infra install comes first.
(cd infra && npm ci)
make check 2>&1 | tee "$tmp/check.log"
! grep -q "skipped: git is unavailable" "$tmp/check.log" || { echo "verify-clean: a tracked-file gate skipped" >&2; exit 1; }
make infra-test
(
  cd frontend
  npm ci
  npm run typecheck
  npm run lint
  npm test
  npm run build
)

echo "verify-clean: passed for HEAD $(git -C "$root" rev-parse --short HEAD)"
