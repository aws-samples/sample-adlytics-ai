# SPDX-License-Identifier: MIT-0
r"""Read and write AMC's result CSV, which escapes an embedded quote with a backslash.

**Not RFC 4180.** In a STANDARD-instance result, a double quote inside a quoted free-text value
arrived as `\"`, not as a doubled `""`, and a reader using the default `csv` dialect split those
rows into extra fields [verified, STANDARD, 2026-10-09; docs/sources/amc.md §5 owns the
evidence]. So every AMC CSV read in `src/` goes through this one dialect: backslash escape,
no doubled quote.

**Fails closed on an ambiguous escape.** Before any row is parsed, the text is scanned once and
refused with `CSV_ESCAPE_AMBIGUOUS`, naming the physical line and the column, when:

- a quoted field holds a doubled quote. That is RFC 4180's escape. Read with this dialect it
  would silently become a different value, and read with the default dialect the backslash form
  splits rows, so neither reading can be trusted;
- a backslash is followed by anything other than a quote or a backslash. How AMC writes a literal
  backslash is [unverified], so `\\` is the only spelling this accepts for one;
- a quote sits inside an unquoted field, or text follows a closing quote. Neither is a form AMC
  was observed to write, and each reads differently under the two dialects.

**Writes the observed form.** A value holding a quote, a backslash, a comma or a line break is
quoted, with `\"` and `\\` inside, which is how the 2026-10-09 result spelled its quoted
description. Anything else is written bare. The fixture generator and every fixture re-chunking
step use this writer, so a committed fixture reads back to exactly the value it was built from.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from enum import Enum, auto
from typing import Final

__all__ = [
    "CSV_ESCAPE_AMBIGUOUS",
    "AmcCsvEscapeError",
    "CsvEscapeFinding",
    "dict_rows",
    "escape_finding",
    "read_rows",
    "write_rows",
]

CSV_ESCAPE_AMBIGUOUS: Final = "CSV_ESCAPE_AMBIGUOUS"

_QUOTE: Final = '"'
_ESCAPE: Final = "\\"
_DELIMITER: Final = ","
_LINE_BREAKS: Final = frozenset({"\n", "\r"})
_NEEDS_QUOTING: Final = frozenset({_QUOTE, _ESCAPE, _DELIMITER, *_LINE_BREAKS})


class _AmcDialect(csv.Dialect):
    """AMC's result CSV: comma-separated, quoted on demand, backslash-escaped, never doubled."""

    delimiter = _DELIMITER
    quotechar = _QUOTE
    escapechar = _ESCAPE
    doublequote = False
    skipinitialspace = False
    lineterminator = "\n"
    quoting = csv.QUOTE_MINIMAL
    strict = True


@dataclass(frozen=True, slots=True)
class CsvEscapeFinding:
    """One refused escape: the code, the 1-based physical line and the column it sits in."""

    line: int
    column: str
    problem: str
    code: str = CSV_ESCAPE_AMBIGUOUS

    def __str__(self) -> str:
        return f"{self.code} at line {self.line}, column {self.column}: {self.problem}."


class AmcCsvEscapeError(ValueError):
    """An AMC CSV holds an escape that could mean two different values."""

    def __init__(self, finding: CsvEscapeFinding) -> None:
        super().__init__(str(finding))
        self.finding = finding


class _State(Enum):
    START = auto()
    UNQUOTED = auto()
    QUOTED = auto()
    CLOSED = auto()


@dataclass(slots=True)
class _Scan:
    """Where the scan is: the physical line, the field ordinal, and the header once read."""

    line: int = 1
    field: int = 0
    header: tuple[str, ...] | None = None
    current: str = ""
    names: list[str] = dataclass_field(default_factory=list[str])

    def finding(self, problem: str) -> CsvEscapeFinding:
        header = self.header or ()
        column = header[self.field] if self.field < len(header) else f"#{self.field + 1}"
        return CsvEscapeFinding(line=self.line, column=column, problem=problem)

    def end_field(self) -> None:
        if self.header is None:
            self.names.append(self.current)
        self.current = ""
        self.field += 1

    def end_record(self) -> None:
        self.end_field()
        if self.header is None:
            self.header = tuple(self.names)
        self.field = 0
        self.line += 1


def _escaped(text: str, index: int, scan: _Scan) -> CsvEscapeFinding | str:
    following = text[index + 1] if index + 1 < len(text) else ""
    if following not in (_QUOTE, _ESCAPE):
        return scan.finding(
            "a backslash is followed by neither a quote nor a backslash, and how AMC writes a "
            "literal backslash is unverified"
        )
    return following


def escape_finding(text: str) -> CsvEscapeFinding | None:  # noqa: PLR0912 - one state machine
    """Return the first ambiguous escape in an AMC CSV, or None when every escape is AMC's own."""
    scan = _Scan()
    state = _State.START
    index = 0
    while index < len(text):
        char = text[index]
        if state is _State.QUOTED:
            if char == _ESCAPE:
                escaped = _escaped(text, index, scan)
                if isinstance(escaped, CsvEscapeFinding):
                    return escaped
                scan.current += escaped
                index += 2
                continue
            if char == _QUOTE:
                state = _State.CLOSED
            else:
                if char == "\n":
                    scan.line += 1
                scan.current += char
            index += 1
            continue
        if state is _State.CLOSED and char not in (_DELIMITER, *_LINE_BREAKS):
            if char == _QUOTE:
                return scan.finding(
                    "a quoted field holds a doubled quote, which is RFC 4180's escape and not "
                    "AMC's backslash"
                )
            return scan.finding("text follows a closing quote")
        if char == _DELIMITER:
            scan.end_field()
            state = _State.START
        elif char in _LINE_BREAKS:
            if char == "\r" and text[index + 1 : index + 2] == "\n":
                index += 1
            scan.end_record()
            state = _State.START
        elif char == _QUOTE:
            if state is not _State.START:
                return scan.finding("a quote sits inside an unquoted field")
            state = _State.QUOTED
        elif char == _ESCAPE:
            escaped = _escaped(text, index, scan)
            if isinstance(escaped, CsvEscapeFinding):
                return escaped
            scan.current += escaped
            state = _State.UNQUOTED
            index += 2
            continue
        else:
            scan.current += char
            state = _State.UNQUOTED
        index += 1
    return None


def _checked(text: str) -> str:
    finding = escape_finding(text)
    if finding is not None:
        raise AmcCsvEscapeError(finding)
    return text


def read_rows(text: str) -> tuple[tuple[str, ...], ...]:
    """Read every record, header first, refusing an ambiguous escape before parsing anything."""
    reader = csv.reader(io.StringIO(_checked(text)), dialect=_AmcDialect)
    return tuple(tuple(row) for row in reader)


def dict_rows(text: str) -> tuple[tuple[str, ...] | None, tuple[dict[str | None, str], ...]]:
    """Read the header and each record as a mapping, with `csv.DictReader`'s short-row rules."""
    reader = csv.DictReader(io.StringIO(_checked(text)), dialect=_AmcDialect)
    rows = tuple(dict(row) for row in reader)
    return (None if reader.fieldnames is None else tuple(reader.fieldnames)), rows


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    if not any(char in _NEEDS_QUOTING for char in text):
        return text
    escaped = text.replace(_ESCAPE, _ESCAPE * 2).replace(_QUOTE, _ESCAPE + _QUOTE)
    return f"{_QUOTE}{escaped}{_QUOTE}"


def write_rows(
    header: Sequence[str], rows: Iterable[Sequence[object] | Mapping[str, object]]
) -> str:
    """Write a header and its rows in the form AMC delivers, one LF-terminated line each.

    A mapping row is written in header order, so a re-chunked fixture keeps its column order.
    """
    lines = [_DELIMITER.join(_cell(name) for name in header)]
    for row in rows:
        values = [row.get(name) for name in header] if isinstance(row, Mapping) else list(row)
        lines.append(_DELIMITER.join(_cell(value) for value in values))
    return "".join(f"{line}\n" for line in lines)
