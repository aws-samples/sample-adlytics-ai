#!/usr/bin/env bash
# SPDX-License-Identifier: MIT-0
#
# Install or remove the tracked hooks in .githooks/ as stubs in the repository's own hooks
# directory, $(git rev-parse --git-common-dir)/hooks/<name>, which every worktree shares.
#
# It never sets or unsets core.hooksPath and changes no git configuration at any scope. A
# system-level hooksPath set by the machine's security tooling therefore stays in force; that
# tooling runs the repository's own hooks directory after its own hooks, and without a system
# hooksPath git runs that directory itself.
#
#   install    write one stub per tracked .githooks/<name>. A stub already in place is left as is.
#              Any other file of that name is named and left untouched, and nothing is installed
#              (exit 1).
#   uninstall  remove only the stubs this script wrote; any other file is named and left.
#
# A stub runs the tracked hook of the checkout it fires in, so each worktree runs its own version;
# a checkout without that hook (a branch older than .githooks/) says so and lets git continue.
# Written for bash 3.2.
set -euo pipefail

usage() {
  echo "usage: install-hooks.sh install|uninstall" >&2
  exit 2
}

[ "$#" -eq 1 ] || usage
action="$1"
case "$action" in
  install | uninstall) ;;
  *) usage ;;
esac

root=$(git rev-parse --show-toplevel)
hooks="$(git rev-parse --path-format=absolute --git-common-dir)/hooks"
marker="# installed by make install-hooks (scripts/install-hooks.sh); make uninstall-hooks removes it"

# The stub for hook $1, exactly as written to disk.
stub() {
  printf '%s\n' \
    '#!/bin/sh' \
    "$marker" \
    "hook=\"\$(git rev-parse --show-toplevel)/.githooks/$1\"" \
    'if [ -x "$hook" ]; then exec "$hook" "$@"; fi' \
    "echo \"$1: \$hook is not in this checkout; no tracked hook ran\" >&2"
}

# True when $1 is a regular file holding exactly the stub for hook $2.
is_stub() {
  [ -f "$1" ] && [ ! -L "$1" ] && [ "$(cat "$1")" = "$(stub "$2")" ]
}

names=""
for path in "$root"/.githooks/*; do
  [ -f "$path" ] && names="$names $(basename "$path")"
done
[ -n "$names" ] || {
  echo "install-hooks: no tracked hooks in $root/.githooks" >&2
  exit 1
}

if [ "$action" = uninstall ]; then
  for name in $names; do
    target="$hooks/$name"
    if is_stub "$target" "$name"; then
      rm -f "$target"
      echo "install-hooks: removed $name"
    elif [ -e "$target" ] || [ -L "$target" ]; then
      echo "install-hooks: left $target: make install-hooks did not write it" >&2
    fi
  done
  exit 0
fi

conflict=0
for name in $names; do
  target="$hooks/$name"
  if { [ -e "$target" ] || [ -L "$target" ]; } && ! is_stub "$target" "$name"; then
    echo "install-hooks: REFUSED $target exists and is not this script's stub; it is left untouched" >&2
    conflict=1
  fi
done
if [ "$conflict" -ne 0 ]; then
  echo "install-hooks: nothing was installed" >&2
  exit 1
fi

mkdir -p "$hooks"
for name in $names; do
  target="$hooks/$name"
  if is_stub "$target" "$name"; then
    echo "install-hooks: $name already installed"
  else
    stub "$name" > "$target"
    echo "install-hooks: installed $name"
  fi
  chmod 755 "$target"
done
if git config --local --get core.hooksPath > /dev/null; then
  echo "install-hooks: warning: this repository sets core.hooksPath, so git does not run $hooks" >&2
fi
