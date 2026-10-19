# SPDX-License-Identifier: MIT-0
"""Deterministic publisher generation over every packaged rendering input."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

PUBLISHER_INPUT_ROOTS = (
    "domain",
    "ports",
    "adapters",
    "contracts",
    "presentation/pages",
)


def publisher_runtime_generation(root: Path, publisher_input: Path) -> str:
    """Hash stable relative paths and bytes, excluding the outer-hashed publisher input."""
    resolved_root = root.resolve()
    excluded = publisher_input.resolve()
    digest = hashlib.sha256()
    files = sorted(
        path
        for relative in PUBLISHER_INPUT_ROOTS
        for path in (resolved_root / relative).rglob("*")
        if path.is_file()
        and path.resolve() != excluded
        and path.suffix != ".pyc"
        and "__pycache__" not in path.parts
    )
    if not files:
        raise ValueError("publisher generation has no packaged inputs.")
    for path in files:
        relative = path.relative_to(resolved_root).as_posix().encode()
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def main() -> int:
    """Print one deterministic generation for the staging script."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--publisher-input", required=True, type=Path)
    args = parser.parse_args()
    print(publisher_runtime_generation(args.root, args.publisher_input))  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
