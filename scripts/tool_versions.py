#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Read the pinned maintainer-tool versions from CONTRIBUTING.md and compare an installed one.

The "Maintainer tools" table in CONTRIBUTING.md is the one place a binary tool's version is pinned
(`| Tool | Version | Used by |`). The publication and release gates refuse a tool whose installed
version differs, so a gate's pass always means the pinned scanner ran. A row whose version is
`any current` names a required tool that is deliberately not pinned (the container runtime);
`pinned()` refuses it, so no gate can mistake it for a pin.

    uv run python scripts/tool_versions.py <tool>    print the pinned version (exit 1 if absent)

Standard library only.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

CONTRIBUTING = Path(__file__).resolve().parents[1] / "CONTRIBUTING.md"
SECTION = "### Maintainer tools"
HEADER = ("Tool", "Version", "Used by")
VERSION = re.compile(r"\d+\.\d+\.\d+")
UNPINNED = "any current"


class ToolVersionError(Exception):
    """The Maintainer tools table, or the named tool in it, is absent or malformed."""


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def table(path: Path = CONTRIBUTING) -> dict[str, str]:
    """Return {tool: version} from the Maintainer tools table, failing loudly when it is absent."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ToolVersionError(f"{path}: unreadable ({error})") from error
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == SECTION)
    except StopIteration:
        raise ToolVersionError(f"{path}: no '{SECTION}' section") from None
    rows: dict[str, str] = {}
    in_table = False
    for line in lines[start + 1 :]:
        if line.startswith("#"):
            break
        if not line.strip().startswith("|"):
            if in_table:
                break
            continue
        cells = _cells(line)
        if not in_table:
            if tuple(cells) != HEADER:
                raise ToolVersionError(f"{path}: the Maintainer tools table header is not {HEADER}")
            in_table = True
            continue
        if all(set(cell) <= {"-", ":"} for cell in cells):
            continue
        if len(cells) != len(HEADER) or not (VERSION.fullmatch(cells[1]) or cells[1] == UNPINNED):
            raise ToolVersionError(f"{path}: malformed Maintainer tools row: {line.strip()}")
        rows[cells[0]] = cells[1]
    if not rows:
        raise ToolVersionError(f"{path}: the Maintainer tools table has no rows")
    return rows


def pinned(name: str, path: Path = CONTRIBUTING) -> str:
    """Return the pinned version of `name`, failing loudly when the table or the row is absent."""
    rows = table(path)
    if name not in rows:
        raise ToolVersionError(f"{path}: no Maintainer tools row for {name}")
    if rows[name] == UNPINNED:
        raise ToolVersionError(f"{path}: {name} is listed as '{UNPINNED}', not version-pinned")
    return rows[name]


def mismatch(name: str, installed: str | None, path: Path = CONTRIBUTING) -> str | None:
    """Return why `installed` does not satisfy the pin for `name`, or None when it matches."""
    expected = pinned(name, path)
    if installed is None:
        return f"{name} is not installed; CONTRIBUTING.md Maintainer tools pins {expected}"
    if installed != expected:
        return f"{name} {installed} is installed; CONTRIBUTING.md Maintainer tools pins {expected}"
    return None


def installed_gitleaks() -> str | None:
    """Return the installed gitleaks version from `gitleaks version`, or None when absent."""
    gitleaks = shutil.which("gitleaks")
    if gitleaks is None:
        return None
    try:
        # A-SUBPROCESS-001: fixed gitleaks argv; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        result = subprocess.run(  # noqa: S603 - fixed argv on the resolved gitleaks path, no shell
            [gitleaks, "version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    match = VERSION.search(result.stdout) if result.returncode == 0 else None
    return match.group(0) if match else None


def main(argv: list[str] | None = None) -> int:
    """Print the pinned version of the named tool."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        sys.stderr.write("usage: tool_versions.py <tool>\n")
        return 2
    try:
        sys.stdout.write(pinned(args[0]) + "\n")
    except ToolVersionError as error:
        sys.stderr.write(f"tool-versions: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
