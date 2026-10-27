# SPDX-License-Identifier: MIT-0
"""The one parser for repository-owned YAML: contracts, declarations and registries.

Every loader parses through libyaml's C parser with PyYAML's safe constructor (`CSafeLoader`). It
builds the same plain values as the pure-Python `SafeLoader` for a fraction of the CPU, and
contract loading is part of every Lambda cold start; docs/repo-structure.md §4 owns the
measurement. There is no pure-Python fallback, because every supported runtime ships the
extension; an environment without it fails at import rather than silently parsing slower.

Safety is unchanged: the safe constructor builds only plain YAML types and refuses every
`!!python/` tag. Duplicate-key checks stay with each loader, either over `compose`'s node tree or
in a `RepositoryLoader` subclass's mapping constructor.
"""

from __future__ import annotations

from typing import cast

import yaml

__all__ = ["RepositoryLoader", "compose", "safe_load"]


class RepositoryLoader(yaml.CSafeLoader):
    """libyaml's C parser with the safe constructor; subclass it to add a mapping check."""


def safe_load(document: str | bytes) -> object:
    """Construct one YAML document as plain values."""
    # A-YAML-SAFELOADER-001: a CSafeLoader subclass; see docs/security-dispositions.md
    return cast("object", yaml.load(document, Loader=RepositoryLoader))  # noqa: S506  # nosec B506


def compose(document: str) -> object:
    """Parse one YAML document to its node tree, for a duplicate-key check before construction."""
    return cast(
        "object",
        yaml.compose(document, Loader=RepositoryLoader),  # type: ignore[reportUnknownMemberType]
    )
