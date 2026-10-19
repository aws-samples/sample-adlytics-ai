#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Run the publication gates locally, before a publication push: make check-publication BASE=<rev>
#
# The export gates ran only at pre-push, so a publication defect surfaced after the work was
# done. This runs them on demand, on exactly what HEAD tracks, and proves the secret scanner can
# fail before trusting its pass:
#
#   P7  file classes in the export: no security-findings*, validation/findings/ holds only
#       .gitkeep, and the only .env* file is .env.example
#   P1  make check-publication-boundary (custody references, account IDs, literal profiles)
#   P2  make check-links
#   P3  make check-license-headers
#   C   scanner positive control: gitleaks must find a runtime-generated AWS access-key-shaped
#       token in a separate temporary directory, or the gate fails with SCANNER_NOT_PROVEN
#   P4  gitleaks dir over the export, after removing .venv/, every __pycache__/ and the
#       throwaway index
#   P10a gitleaks git over <BASE>..HEAD minus everything already on a remote, in the real
#       checkout, read-only
#
# P1's generic 12-digit and --profile rules subsume literal account and profile greps, so none is
# written here. make verify-clean owns the full gate run in a clean export.
#
# The export is indexed exactly as scripts/verify-clean.sh does it, because P1 and P2 enumerate
# tracked files with git and skip without a repository; a skip line fails the gate. Each gate
# prints one line; the first failure prints its output and exits non-zero. Touches no AWS.
set -euo pipefail

if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  echo "usage: check-publication.sh <BASE revision>  (make check-publication BASE=<rev>)" >&2
  exit 2
fi
base_arg="$1"

root=$(git rev-parse --show-toplevel)
cd "$root"
base=$(git rev-parse --verify -q "$base_arg^{commit}") || {
  echo "check-publication: BASE $base_arg does not name a commit in this checkout" >&2
  exit 2
}
command -v gitleaks >/dev/null 2>&1 || {
  echo "check-publication: gitleaks is not installed; the publication scan cannot run" >&2
  exit 2
}
if [ -n "$(git status --porcelain)" ]; then
  echo "warning: the working tree has uncommitted changes; they are not checked." >&2
fi

tmp=$(mktemp -d)
control=$(mktemp -d)
trap 'rm -rf "$tmp" "$control"' EXIT
export_dir="$tmp/export"
mkdir "$export_dir"
head=$(git rev-parse HEAD)
echo "check-publication: HEAD $head BASE $base ($(gitleaks version))"

fail() {
  # $1 gate, $2 reason, $3 optional output file to show
  echo "check-publication: $1 FAILED $2" >&2
  if [ "$#" -ge 3 ] && [ -f "$3" ]; then
    tail -n 60 "$3" >&2
  fi
  exit 1
}

# Run one gate in the export with its output kept for a failure report.
gate() {
  local name="$1"
  shift
  local log="$tmp/$name.log"
  if ! "$@" > "$log" 2>&1; then
    fail "$name" "($*)" "$log"
  fi
  if grep -q "skipped: git is unavailable" "$log"; then
    fail "$name" "a tracked-file gate skipped" "$log"
  fi
  echo "check-publication: $name ok ($*)"
}

git archive HEAD | tar -x -C "$export_dir"
cd "$export_dir"

# P7 runs on the pristine export, before anything is installed into it.
findings_files=$(find . -name 'security-findings*' -print)
[ -z "$findings_files" ] || fail P7 "security-findings files are tracked: $findings_files"
if [ -d validation/findings ]; then
  extra=$(find validation/findings -mindepth 1 ! -name .gitkeep -print)
  [ -z "$extra" ] || fail P7 "validation/findings holds more than .gitkeep: $extra"
fi
env_files=$(find . -name '.env*' ! -name .env.example -print)
[ -z "$env_files" ] || fail P7 ".env* files other than .env.example are tracked: $env_files"
echo "check-publication: P7 ok (file classes)"

# The tracked-file gates enumerate with git; index exactly what HEAD tracked so they scan the
# export instead of skipping. This is a throwaway repository in the temporary directory.
git init -q
git add -A
test "$(git ls-files | wc -l)" -gt 0 || fail setup "the export indexed no files"
gate install make install
gate P1 make check-publication-boundary
gate P2 make check-links
gate P3 make check-license-headers

# The control comes before P4: a scanner that cannot find a planted key proves nothing by passing.
# The token is generated here and never written inside a repository.
# Key IDs are base32 (A-Z, 2-7), which the aws-access-token rule requires; 16 distinct characters
# keep the token above the rule's entropy floor on every run.
token_tail=$(LC_ALL=C tr -dc 'A-Z2-7' < /dev/urandom 2>/dev/null | head -c 400 | fold -w1 \
  | awk '!seen[$0]++' | head -n 16 | tr -d '\n' || true)
[ "${#token_tail}" -eq 16 ] || fail C "could not generate a control token"
printf 'aws_access_key_id = AKIA%s\n' "$token_tail" > "$control/credentials.txt"
control_report="$tmp/control-report.json"
set +e
gitleaks dir "$control" --redact --no-banner -c "$export_dir/.gitleaks.toml" \
  --report-format json --report-path "$control_report" > "$tmp/C.log" 2>&1
control_rc=$?
set -e
if [ "$control_rc" -ne 1 ] || ! grep -q '"RuleID": *"aws-access-token"' "$control_report"; then
  fail C "SCANNER_NOT_PROVEN (gitleaks exit $control_rc on a planted access key)" "$tmp/C.log"
fi
echo "check-publication: C ok (scanner found the planted access key, exit 1)"

rm -rf .venv .git
find . -name __pycache__ -type d -prune -exec rm -rf {} +
gate P4 gitleaks dir . --redact --no-banner -c "$export_dir/.gitleaks.toml"

cd "$root"
unpushed=$(git rev-list --count "$base..HEAD" --not --remotes)
gate P10a gitleaks git "$root" --redact --no-banner -c "$export_dir/.gitleaks.toml" \
  "--log-opts=$base..HEAD --not --remotes"
echo "check-publication: P10a scanned $unpushed unpushed commit(s) after BASE"

echo "check-publication: passed for HEAD $(git rev-parse --short HEAD)"
