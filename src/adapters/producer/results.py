# SPDX-License-Identifier: MIT-0
"""Parse exact CSV result bytes into pure producer conformance evidence."""

from __future__ import annotations

from adapters.amc_csv import read_rows
from domain.producer.models import ResultEvidence, ResultSummary, digest_bytes


def parse_csv_result(body: bytes) -> tuple[ResultSummary, ResultEvidence]:
    """Read one complete CSV, preserving header order while validating row width."""
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("producer result must be UTF-8 CSV.") from error

    # AMC's dialect (backslash escape, no doubled quote). An ambiguous escape raises
    # `AmcCsvEscapeError`, a `ValueError`, so the Finalizer refuses the registration.
    reader = iter(read_rows(text))
    try:
        header = tuple(next(reader))
    except StopIteration as error:
        raise ValueError("producer result is empty and has no CSV header.") from error
    if not header or any(not name.strip() for name in header):
        raise ValueError("producer result header contains a blank column name.")

    starts: set[str] = set()
    ends: set[str] = set()
    start_index = header.index("time_window_start") if "time_window_start" in header else None
    end_index = header.index("time_window_end") if "time_window_end" in header else None
    suppressed_index = header.index("rows_suppressed") if "rows_suppressed" in header else None
    row_count = 0
    suppressed_row_count = 0
    for line_number, row in enumerate(reader, start=2):
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError(
                f"producer result row {line_number} has {len(row)} cells for {len(header)} columns."
            )
        row_count += 1
        if suppressed_index is not None:
            suppression = row[suppressed_index].strip().lower()
            if suppression not in {"true", "false"}:
                raise ValueError(
                    f"producer result row {line_number} has invalid rows_suppressed "
                    f"value {row[suppressed_index]!r}; expected true or false."
                )
            suppressed_row_count += int(suppression == "true")
        if start_index is not None:
            starts.add(row[start_index])
        if end_index is not None:
            ends.add(row[end_index])

    summary = ResultSummary(
        columns=header,
        row_count=row_count,
        suppressed_row_count=suppressed_row_count,
        time_window_starts=frozenset(starts),
        time_window_ends=frozenset(ends),
    )
    evidence = ResultEvidence(
        byte_count=len(body),
        checksum=digest_bytes(body),
        declared_columns=header,
    )
    return summary, evidence
