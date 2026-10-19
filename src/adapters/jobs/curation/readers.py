# SPDX-License-Identifier: MIT-0
"""Parse a delivered file into the structure the mapper works on.

Three readers because `source_format` is declared as data rather than written as code
(docs/capabilities/data-layer/architecture.md §4): adding a channel adds an enum value and a
field-mapping table, never a new pipeline. The formats differ only in how bytes become
records, which is why this module is thin and `domain/curation/mapping.py` is where the
judgement lives.

**Standard library only.** No dataframe library: the daily volumes are pre-aggregated AMC
output and vendor JSON for one advertiser, so `csv` and `json` are sufficient, and a
dependency added on the assumption of scale we have not measured would sit in every Lambda
bundle against the 250 MB quota.

**JSON numbers are parsed as `Decimal`.** `parse_float=Decimal` is the single most important
line here. A vendor `Amount.amount` may arrive as a bare JSON number, and Python's default
would make it a binary `float` — `11.37` is not representable, so the error is already present
before any divisor is applied and survives every later sum. Money must never pass through a
float, and this is the only place that can be guaranteed.

**Additive tolerance, per the `data-contract-definition` steering.** Nothing here rejects an
unrecognised field; the mapper takes what the contract declares and ignores the rest. A
delivered file that gained a column must still load.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from adapters.amc_csv import dict_rows
from domain.curation.mapping import SourceDocument
from domain.data_contracts.contract import SourceFormat

__all__ = ["DeliveredFile", "ReaderError", "read"]


class ReaderError(Exception):
    """A delivered file cannot be parsed in the format its contract declares."""


@dataclass(frozen=True, slots=True)
class DeliveredFile:
    """One landed object and the manifest that accompanies it.

    The manifest is separate and required-shaped rather than optional-looking: upstream is an
    untrusted producer, and a load with no manifest has nothing to validate against
    (docs/concepts/provenance-chain.md). An empty manifest is a legitimate *input* here so a
    test can exercise a reader alone, and the load assertions are what refuse it for real.
    """

    text: str
    source_format: SourceFormat
    manifest_text: str = ""


def _parse_json(text: str, *, where: str) -> Any:
    """Parse JSON with every number exact.

    `parse_float=Decimal` covers decimal literals. Integers stay `int`, which is correct —
    a scaled monetary value is an integer and dividing it is the mapper's job.
    """
    try:
        return json.loads(text, parse_float=Decimal)
    except json.JSONDecodeError as error:
        raise ReaderError(f"{where} is not valid JSON: {error}") from error


def _manifest(payload: DeliveredFile) -> dict[str, Any]:
    if not payload.manifest_text.strip():
        return {}
    parsed = _parse_json(payload.manifest_text, where="the manifest")
    if not isinstance(parsed, dict):
        raise ReaderError("the manifest must be a JSON object.")
    return cast("dict[str, Any]", parsed)


def _read_csv(text: str) -> tuple[dict[str, Any], ...]:
    """Read AMC workflow output.

    `csv.DictReader` rather than splitting on commas, because a delivered value may be quoted
    and contain a comma — a product title routinely does. Every value stays a string; the
    generated row model is what types them.

    **An empty field is absence, not an empty string.** CSV has no way to spell null, so an
    absent value arrives as `,,`. Left as `""` it would collapse two different facts: a
    nullable string column would store an empty string where the contract says the value was
    withheld, and a nullable integer column would fail to parse at all. AMC's own suppressed
    rows are the case that matters — `distinct_users_used` is empty on a merged row, because
    the count that drove the decision is not reported for it.

    The cost, accepted knowingly: a genuinely empty string in a delivered file becomes null.
    That is the right way round for this platform, since the four meanings of null are
    load-bearing and an empty string is not one of them.

    **AMC's dialect, not RFC 4180.** AMC escapes an embedded quote with a backslash, so this reads
    through `adapters.amc_csv`, which raises `AmcCsvEscapeError` (`CSV_ESCAPE_AMBIGUOUS`) on an
    escape that could mean two values rather than guess which one was meant. The load turns that
    into a blocked result (`ingest_delivery.semantic_ingestion`).
    """
    fieldnames, records = dict_rows(text)
    if fieldnames is None:
        raise ReaderError("the CSV has no header row, so no column can be identified.")
    rows: list[dict[str, Any]] = []
    for row in records:
        # DictReader yields None for a short row and puts surplus values under `restkey`.
        # Dropping the restkey keeps additive tolerance; a short row keeps its Nones so the
        # row model names the column that is missing rather than the reader guessing.
        rows.append(
            {key: None if value == "" else value for key, value in row.items() if key is not None}
        )
    return tuple(rows)


def _read_jsonl(text: str) -> tuple[dict[str, Any], ...]:
    """Read Vendor Data Kiosk output: one JSON object per line.

    Blank lines are skipped rather than treated as records, because a trailing newline is
    normal and an empty record is not.
    """
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        parsed = _parse_json(line, where=f"JSONL line {number}")
        if not isinstance(parsed, dict):
            raise ReaderError(f"JSONL line {number} is not a JSON object.")
        rows.append(cast("dict[str, Any]", parsed))
    return tuple(rows)


def _read_dual_array(text: str) -> dict[str, Any]:
    """Read a Vendor Central report: two sibling arrays at two grains per document.

    Returned whole rather than flattened. Which array this contract reads, and which
    document-level fields project onto every row, are contract questions — so the mapper
    answers them and this only parses.
    """
    parsed = _parse_json(text, where="the report document")
    if not isinstance(parsed, dict):
        raise ReaderError("a vendor report document must be a JSON object.")
    return cast("dict[str, Any]", parsed)


def read(payload: DeliveredFile) -> SourceDocument:
    """Parse one delivered file into the mapper's input."""
    manifest = _manifest(payload)

    if payload.source_format is SourceFormat.CSV:
        return SourceDocument(manifest=manifest, rows=_read_csv(payload.text))
    if payload.source_format is SourceFormat.JSONL:
        return SourceDocument(manifest=manifest, rows=_read_jsonl(payload.text))
    if payload.source_format is SourceFormat.JSON_DUAL_ARRAY:
        return SourceDocument(manifest=manifest, document=_read_dual_array(payload.text))

    # Unreachable while SourceFormat has exactly the three members above, and kept so that
    # adding a format without a reader fails loudly rather than returning an empty document.
    raise ReaderError(f"no reader for source format {payload.source_format}.")
