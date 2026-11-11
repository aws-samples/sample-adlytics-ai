# SPDX-License-Identifier: MIT-0
"""Refuse a demand-forecast job lock that drifts, lacks a hash or holds an unapproved package.

The job image installs exactly `src/adapters/jobs/demand_forecast/requirements.lock` with
`pip install --require-hashes --no-deps` (design §4b). This check fails when:

- a fresh `uv export --frozen --only-group forecasting` differs from the committed file;
- any requirement in the file has no `--hash`;
- the `name==version` set whose markers apply to the image (linux, x86_64, CPython 3.14) differs
  from `approved-packages.txt`, the one home of approval D-1's list.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
JOB_DIR = ROOT / "src" / "adapters" / "jobs" / "demand_forecast"
LOCK = JOB_DIR / "requirements.lock"
APPROVED = JOB_DIR / "approved-packages.txt"
# The image's interpreter and platform (Dockerfile FROM, design §4b).
IMAGE_ENVIRONMENT = {
    "sys_platform": "linux",
    "platform_machine": "x86_64",
    "platform_system": "Linux",
    "os_name": "posix",
    "python_version": "3.14",
    "python_full_version": "3.14.0",
    "implementation_name": "cpython",
    "platform_python_implementation": "CPython",
}
EXPORT = (
    "uv",
    "export",
    "--frozen",
    "--only-group",
    "forecasting",
    "--no-emit-project",
    "--format",
    "requirements-txt",
)


@dataclass(frozen=True)
class Requirement:
    """One requirement of an exported lock: its pin, its marker (or None) and its hash count."""

    name: str
    version: str
    marker: str | None
    hashes: int


def parse_lock(text: str) -> tuple[Requirement, ...]:
    """Return every requirement of a `uv export` requirements file, with its hash count."""
    found: list[Requirement] = []
    current: list[str] = []

    def close() -> None:
        if not current:
            return
        head = current[0].rstrip("\\").strip()
        spec, _, marker = head.partition(";")
        name, sep, version = spec.strip().partition("==")
        if not sep:
            raise ValueError(f"requirement is not an exact pin: {head!r}")
        hashes = sum(1 for line in current if line.strip().startswith("--hash="))
        found.append(
            Requirement(canonicalize_name(name), version.strip(), marker.strip() or None, hashes)
        )

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if raw[:1].isspace():
            current.append(line)
            continue
        close()
        current = [line]
    close()
    return tuple(found)


def applicable(requirements: tuple[Requirement, ...]) -> set[str]:
    """Return the `name==version` lines whose markers apply to the job image."""
    return {
        f"{r.name}=={r.version}"
        for r in requirements
        if r.marker is None or Marker(r.marker).evaluate(IMAGE_ENVIRONMENT)
    }


def approved(text: str) -> set[str]:
    """Return the approved set, normalised the way `parse_lock` normalises names."""
    lines: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        lines.add(f"{canonicalize_name(name)}=={version}")
    return lines


def problems(lock_text: str, exported_text: str, approved_text: str) -> list[str]:
    """Return every reason the committed lock is refused, empty when it passes."""
    found: list[str] = []
    if lock_text != exported_text:
        found.append(
            "requirements.lock differs from a fresh export; run make generate-forecast-lock"
        )
    requirements = parse_lock(lock_text)
    found.extend(f"{r.name}=={r.version} has no --hash" for r in requirements if r.hashes == 0)
    have = applicable(requirements)
    want = approved(approved_text)
    found.extend(f"not approved: {line}" for line in sorted(have - want))
    found.extend(f"approved but missing: {line}" for line in sorted(want - have))
    return found


def _export() -> str:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "requirements.lock"
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [*EXPORT, "-o", str(out)], cwd=ROOT, check=True, capture_output=True
        )
        text = out.read_text(encoding="utf-8")
    # The export records the command line it ran; the committed file names its own path.
    return text.replace(f"-o {out}", "-o src/adapters/jobs/demand_forecast/requirements.lock")


def main() -> int:
    """Run the check against the repository; exit 1 with every problem listed."""
    found = problems(
        LOCK.read_text(encoding="utf-8"), _export(), APPROVED.read_text(encoding="utf-8")
    )
    for line in found:
        print(f"check-forecast-lock: {line}", file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
