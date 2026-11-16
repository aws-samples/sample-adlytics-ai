# SPDX-License-Identifier: MIT-0
"""Build artifact roster entries from the mounted model directory (design §4b).

The operator command builds the same entries from S3 (`demand_forecast_artifact_digest`), and
both feed `domain.forecasting.artifact_roster.roster_digest`, so the mounted copy's digest can be
compared with the configured one before the model loads.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from domain.forecasting.artifact_roster import RosterEntry

_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    """Stream one file through SHA-256."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def local_roster_entries(root: Path) -> tuple[RosterEntry, ...]:
    """Every regular file under `root`, as (relative POSIX path, size, SHA-256), sorted by path."""
    files = sorted(path for path in root.rglob("*") if path.is_file())
    return tuple(
        RosterEntry(path.relative_to(root).as_posix(), path.stat().st_size, file_sha256(path))
        for path in files
    )
