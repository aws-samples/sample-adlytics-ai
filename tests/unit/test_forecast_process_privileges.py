# SPDX-License-Identifier: MIT-0
"""The forecast job's start-line privilege fields, read from /proc/self/status.

They are the live evidence that the entrypoint dropped root before any job code ran
(O-DF-JOB-ROOT-USER-001). A misread field would record a privilege drop that did not happen, and a
read failure that stopped the job would turn missing evidence into a failed forecast.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from adapters.jobs.demand_forecast.job_log import JobLog, JobLogError, check_line
from adapters.jobs.demand_forecast.process_privileges import (
    STATUS_INCOMPLETE,
    STATUS_UNREADABLE,
    parse_status,
    privilege_fields,
)

pytestmark = pytest.mark.unit

# A process after `setpriv --reuid=forecast --no-new-privs --inh-caps=-all`, in the kernel's
# own layout. Near-miss names (CapAmb, NoNewPrivsX) must not be read as the fields.
DROPPED = (
    "Name:\tpython\n"
    "Uid:\t10001\t10001\t10001\t10001\n"
    "CapInh:\t0000000000000000\n"
    "CapPrm:\t0000000000000000\n"
    "CapEff:\t0000000000000000\n"
    "CapBnd:\t000001ffffffffff\n"
    "CapAmb:\t0000000000000000\n"
    "NoNewPrivsX:\t0\n"
    "NoNewPrivs:\t1\n"
    "Seccomp:\t0\n"
)
# The paired control: a root process with full capabilities and no-new-privs unset.
ROOT = (
    "Uid:\t0\t0\t0\t0\n"
    "CapPrm:\t000001ffffffffff\n"
    "CapEff:\t000001ffffffffff\n"
    "CapBnd:\t000001ffffffffff\n"
    "NoNewPrivs:\t0\n"
)
PRIVILEGE_FIELDS = ("cap_eff", "cap_prm", "cap_bnd", "no_new_privs", "privilege_error")


def _status(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "status"
    path.write_text(text, encoding="ascii")
    return path


def test_a_dropped_process_reads_as_no_effective_capabilities_and_no_new_privs(
    tmp_path: Path,
) -> None:
    assert privilege_fields(_status(tmp_path, DROPPED)) == {
        "cap_eff": "0000000000000000",
        "cap_prm": "0000000000000000",
        "cap_bnd": "000001ffffffffff",
        "no_new_privs": "1",
        "privilege_error": None,
    }


def test_a_root_process_is_reported_verbatim_not_as_dropped(tmp_path: Path) -> None:
    assert privilege_fields(_status(tmp_path, ROOT)) == {
        "cap_eff": "000001ffffffffff",
        "cap_prm": "000001ffffffffff",
        "cap_bnd": "000001ffffffffff",
        "no_new_privs": "0",
        "privilege_error": None,
    }


def test_a_missing_field_is_null_and_named_incomplete(tmp_path: Path) -> None:
    text = DROPPED.replace("NoNewPrivs:\t1\n", "")
    assert parse_status(text)["no_new_privs"] is None
    fields = privilege_fields(_status(tmp_path, text))
    assert fields["privilege_error"] == STATUS_INCOMPLETE
    assert fields["cap_eff"] == "0000000000000000"


@pytest.mark.parametrize("content", [None, b"CapEff:\t\xff\n"], ids=["absent", "undecodable"])
def test_an_unreadable_status_is_null_and_named_unreadable_without_raising(
    tmp_path: Path, content: bytes | None
) -> None:
    path = tmp_path / "status"
    if content is not None:
        path.write_bytes(content)
    assert privilege_fields(path) == {
        "cap_eff": None,
        "cap_prm": None,
        "cap_bnd": None,
        "no_new_privs": None,
        "privilege_error": STATUS_UNREADABLE,
    }


START_LINES = {
    "self_check.start": {"uid": 10001, "model_id": "amazon/chronos-2"},
    "forecast.start": {
        "uid": 10001,
        "model_id": "amazon/chronos-2",
        "shard": 0,
        "attempt_id": "r0a0",
        "input_attempt_id": "r0a0",
    },
}


@pytest.mark.parametrize("operation", sorted(START_LINES))
@pytest.mark.parametrize("text", [DROPPED, None], ids=["read", "unreadable"])
def test_each_start_line_carries_the_privilege_fields_in_the_schema(
    tmp_path: Path, operation: str, text: str | None
) -> None:
    path = _status(tmp_path, text) if text is not None else tmp_path / "absent"
    stream = io.StringIO()
    fields = privilege_fields(path)
    JobLog("run-1", stream).write("INFO", operation, **START_LINES[operation], **fields)
    line = json.loads(stream.getvalue())
    assert {name: line[name] for name in PRIVILEGE_FIELDS} == fields


@pytest.mark.parametrize("operation", sorted(START_LINES))
def test_a_start_line_without_the_privilege_fields_is_refused(operation: str) -> None:
    record: dict[str, object] = {
        "timestamp": "2026-11-15T00:00:00.000+00:00",
        "level": "INFO",
        "service": "demand-forecast-job",
        "operation": operation,
        "correlation_id": "run-1",
        **START_LINES[operation],
    }
    with pytest.raises(JobLogError, match=f"log line outside the schema: {operation}"):
        check_line(record)
