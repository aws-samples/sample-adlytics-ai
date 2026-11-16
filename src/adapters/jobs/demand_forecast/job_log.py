# SPDX-License-Identifier: MIT-0
"""The job's JSON log lines and its exit message (adapted from the source engine's job log).

Every line matches one entry of `job_log_schema.json` exactly: IDs, counts, digests, reasons and
durations only, never a series value or an ASIN (AC-4.10, NFR-12). The exit message is the typed
error envelope SageMaker returns as `ExitMessage` (its first 1 KB).
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO, cast

SCHEMA_PATH = Path(__file__).with_name("job_log_schema.json")
EXIT_MESSAGE_BYTES = 1024
SERVICE = "demand-forecast-job"


class JobLogError(ValueError):
    """A log line outside the schema; a defect in the job, never logged."""


def _schema() -> dict[str, object]:
    return cast("dict[str, object]", json.loads(SCHEMA_PATH.read_text(encoding="utf-8")))


def _shape_ok(shape: str, value: object) -> bool:  # noqa: PLR0911 - one branch per declared shape
    if shape == "string":
        return isinstance(value, str)
    if shape == "string_or_null":
        return value is None or isinstance(value, str)
    if shape == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if shape == "boolean":
        return isinstance(value, bool)
    if shape == "integer_map":
        return isinstance(value, dict) and all(
            isinstance(v, int) and not isinstance(v, bool)
            for v in cast("dict[str, object]", value).values()
        )
    if shape == "string_list":
        return isinstance(value, list) and all(
            isinstance(v, str) for v in cast("list[object]", value)
        )
    if shape == "error":
        return isinstance(value, dict) and set(cast("dict[str, object]", value)) == {
            "code",
            "cause",
        }
    return False


def check_line(record: Mapping[str, object], schema: Mapping[str, object] | None = None) -> None:
    """Refuse a record that matches no schema entry exactly."""
    document = schema or _schema()
    common = cast("dict[str, str]", document["common"])
    lines = cast("list[dict[str, object]]", document["lines"])
    for line in lines:
        if line["operation"] != record.get("operation") or line["level"] != record.get("level"):
            continue
        fields = cast("dict[str, str]", line["fields"])
        expected = {**common, **fields}
        if set(record) == set(expected) and all(
            _shape_ok(shape, record[name]) for name, shape in expected.items()
        ):
            return
    raise JobLogError(f"log line outside the schema: {record.get('operation')}")


class JobLog:
    """One JSON object per line on stdout, each correlated to the run."""

    def __init__(self, correlation_id: str, stream: TextIO | None = None) -> None:
        self._correlation_id = correlation_id
        self._stream = stream or sys.stdout
        self._schema = _schema()

    def write(self, level: str, operation: str, **fields: object) -> None:
        """Write one schema-checked line and flush it."""
        record: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": level,
            "service": SERVICE,
            "operation": operation,
            "correlation_id": self._correlation_id,
            **fields,
        }
        check_line(record, self._schema)
        self._stream.write(json.dumps(record, sort_keys=True) + "\n")
        self._stream.flush()


def structured_error(code: str, cause: tuple[str, ...], run_id: str | None) -> str:
    """Return the exit message: the code, the cause chain and the run ID, within 1 KB.

    To fit, the run ID goes first, then the cause chain loses its outermost entries down to its
    root; the code is never dropped.
    """
    document: dict[str, object] = {"code": code, "cause": list(cause), "run_id": run_id}

    def fits() -> bool:
        return len(json.dumps(document, sort_keys=True).encode("utf-8")) <= EXIT_MESSAGE_BYTES

    if not fits():
        document["run_id"] = None
    chain = list(cause)
    while not fits() and len(chain) > 1:
        chain = chain[1:]
        document["cause"] = chain
    return json.dumps(document, sort_keys=True)
