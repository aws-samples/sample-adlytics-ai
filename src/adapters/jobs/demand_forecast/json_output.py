# SPDX-License-Identifier: MIT-0
"""`ForecastOutputPort` on the Processing job's output mount (design §4b outputs).

Replaces the source engine's Parquet output: the publisher reads JSON records, so one shard
attempt's candidate is `series.jsonl.gz` (one `CandidateSeriesV1` per line, canonical JSON with
sorted keys, gzip with a zero timestamp so the same records give the same bytes) and then
`run-report.json`. Callers may rely on: the run report is written last and names the series
file's SHA-256, so a reader that finds the report finds a complete candidate.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

from domain.forecasting.boundary import CandidateSeriesV1, RunReportV1
from ports.outbound.forecast_output import WrittenCandidate

SERIES_FILE = "series.jsonl.gz"
REPORT_FILE = "run-report.json"


def canonical_json(document: object) -> str:
    """Sorted keys, no spaces: one byte form for one record."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def series_bytes(records: Sequence[CandidateSeriesV1]) -> bytes:
    """Return the records as deterministic gzip bytes, one canonical JSON line each."""
    lines = "".join(
        canonical_json(record.model_dump(mode="json", by_alias=True)) + "\n" for record in records
    )
    return gzip.compress(lines.encode("utf-8"), mtime=0)


class JsonCandidateOutput:
    """Writes one shard attempt's candidate under the output directory."""

    def __init__(self, output_dir: Path) -> None:
        self._output = output_dir

    def write_series(self, records: Sequence[CandidateSeriesV1]) -> WrittenCandidate:
        """Write every record; return the file's SHA-256 and record count."""
        body = series_bytes(records)
        self._output.mkdir(parents=True, exist_ok=True)
        (self._output / SERIES_FILE).write_bytes(body)
        return WrittenCandidate(hashlib.sha256(body).hexdigest(), len(records))

    def write_report(self, report: RunReportV1) -> None:
        """Write the run report, after the series."""
        document = report.model_dump(mode="json", by_alias=True)
        (self._output / REPORT_FILE).write_text(canonical_json(document) + "\n", encoding="utf-8")
