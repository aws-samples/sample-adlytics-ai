# SPDX-License-Identifier: MIT-0
"""The job process's privilege state as the kernel reports it, logged on each mode's start line.

`entrypoint.sh` drops root with `setpriv` before the job runs (ADR-160). The start line carries
the effective, permitted and bounding capability sets and the no-new-privs flag from
`/proc/self/status`, verbatim, so every run's first line is live evidence of that drop
(O-DF-JOB-ROOT-USER-001 in docs/security-dispositions.md). A failed read never stops the job: the
values are null and `privilege_error` names the code.
"""

from __future__ import annotations

from pathlib import Path

STATUS_PATH = Path("/proc/self/status")
# /proc/self/status field name -> log field name.
STATUS_FIELDS = {
    "CapEff": "cap_eff",
    "CapPrm": "cap_prm",
    "CapBnd": "cap_bnd",
    "NoNewPrivs": "no_new_privs",
}
STATUS_UNREADABLE = "PROCESS_STATUS_UNREADABLE"
"""The status file could not be read or decoded."""
STATUS_INCOMPLETE = "PROCESS_STATUS_INCOMPLETE"
"""The status file was read but lacks at least one of `STATUS_FIELDS`."""


def parse_status(text: str) -> dict[str, str | None]:
    """Return each `STATUS_FIELDS` value as the kernel wrote it, or None when the line is absent."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        name, separator, value = line.partition(":")
        if separator and name in STATUS_FIELDS:
            found[STATUS_FIELDS[name]] = value.strip()
    return {field: found.get(field) for field in STATUS_FIELDS.values()}


def privilege_fields(path: Path = STATUS_PATH) -> dict[str, str | None]:
    """Return the start line's privilege fields and `privilege_error` (None when all were read)."""
    try:
        text = path.read_text(encoding="ascii")
    except OSError, UnicodeDecodeError:
        return {**dict.fromkeys(STATUS_FIELDS.values()), "privilege_error": STATUS_UNREADABLE}
    fields = parse_status(text)
    complete = all(value is not None for value in fields.values())
    return {**fields, "privilege_error": None if complete else STATUS_INCOMPLETE}
