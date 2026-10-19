# SPDX-License-Identifier: MIT-0
"""Verify exact SPDX markers on every authored or owned generated source file."""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

ROOT_NAMES = ("frontend", "infra", "screens", "scripts", "src", "tests", "validation")
ELIGIBLE_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh"})
HASH_MARKER = "# SPDX-License-Identifier: MIT-0"
SLASH_MARKER = "// SPDX-License-Identifier: MIT-0"
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "node_modules",
        ".pnpm-store",
        ".venv",
        "venv",
        "cdk.out",
        ".cdk.staging",
        ".next",
        "out",
        ".vercel",
        ".test-dist",
        "build",
        "dist",
        "htmlcov",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".import_linter_cache",
        ".protoshield",
    }
)
EXCLUDED_PREFIXES = (("validation", "findings"),)
EXCLUDED_PATHS = frozenset({("frontend", "next-env.d.ts")})


def _is_excluded(relative_path: Path) -> bool:
    """Return whether a repository-relative path is outside publication source scope."""
    parts = relative_path.parts
    return (
        parts in EXCLUDED_PATHS
        or any(parts[: len(prefix)] == prefix for prefix in EXCLUDED_PREFIXES)
        or any(part in EXCLUDED_DIRECTORY_NAMES or part.endswith(".egg-info") for part in parts)
    )


def is_candidate(path: Path, repository_root: Path) -> bool:
    """Return whether a file belongs to the deterministic license-header scope."""
    try:
        relative_path = path.relative_to(repository_root)
    except ValueError:
        return False
    return (
        path.is_file()
        and relative_path.parts[:1] in {(root,) for root in ROOT_NAMES}
        and path.suffix in ELIGIBLE_SUFFIXES
        and not _is_excluded(relative_path)
    )


def candidate_paths(repository_root: Path) -> list[Path]:
    """Return all in-scope files in stable repository-relative order."""
    candidates: list[Path] = []
    for root_name in ROOT_NAMES:
        root = repository_root / root_name
        if root.is_dir():
            candidates.extend(
                path for path in root.rglob("*") if is_candidate(path, repository_root)
            )
    return sorted(candidates, key=lambda path: path.relative_to(repository_root).as_posix())


def expected_marker(path: Path) -> str:
    """Return the exact marker required for a file's language."""
    if path.suffix in {".py", ".sh"}:
        return HASH_MARKER
    return SLASH_MARKER


def has_valid_header(path: Path) -> bool:
    """Accept a marker only on line one, or line two immediately after a shebang."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines:
        return False
    marker = expected_marker(path)
    if lines[0] == marker:
        return True
    return lines[0].startswith("#!") and len(lines) > 1 and lines[1] == marker


def missing_headers(paths: Iterable[Path]) -> list[Path]:
    """Return every path whose marker is absent or malformed, preserving input order."""
    return [path for path in paths if not has_valid_header(path)]


def main() -> int:
    """Report every failure and return a nonzero status when enforcement fails."""
    repository_root = Path(__file__).resolve().parents[1]
    candidates = candidate_paths(repository_root)
    missing = missing_headers(candidates)
    if missing:
        for path in missing:
            relative_path = path.relative_to(repository_root).as_posix()
            print(f"MISSING: {relative_path}")  # noqa: T201
        summary = f"LICENSE HEADER CHECK FAILED: {len(missing)} of {len(candidates)} files missing."
        print(summary)  # noqa: T201
        return 1
    print(f"LICENSE HEADER CHECK PASSED: {len(candidates)} files checked.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
