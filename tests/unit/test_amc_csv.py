# SPDX-License-Identifier: MIT-0
r"""AMC's result CSV escapes an embedded quote with a backslash, and every AMC reader says so.

[verified, STANDARD, 2026-10-09] a quote inside a quoted segment description arrived as `\"`,
and the default `csv` dialect split those rows (docs/sources/amc.md §5). Each refusal here is
paired with the control that the same value in AMC's own form reads back exactly, at the dialect,
at the load's reader, at the producer's result parser, at the Finalizer and at the load.
"""

from __future__ import annotations

import pytest
from test_new_to_brand_buyers import (
    KEPT,
    WITHHELD,
    _csv,  # pyright: ignore[reportPrivateUsage]
    _partition_actions,  # pyright: ignore[reportPrivateUsage]
)
from test_producer_finalizer import (
    BUCKET,
    NOW,
    _finalizer,  # pyright: ignore[reportPrivateUsage]
    _Landing,  # pyright: ignore[reportPrivateUsage]
    _registration,  # pyright: ignore[reportPrivateUsage]
    _result,  # pyright: ignore[reportPrivateUsage]
    _Statuses,  # pyright: ignore[reportPrivateUsage]
)

from adapters.amc_csv import (
    CSV_ESCAPE_AMBIGUOUS,
    AmcCsvEscapeError,
    CsvEscapeFinding,
    dict_rows,
    escape_finding,
    read_rows,
    write_rows,
)
from adapters.jobs.curation.readers import DeliveredFile, read
from adapters.producer.results import parse_csv_result
from domain.data_contracts.contract import SourceFormat

pytestmark = [pytest.mark.unit]

HEADER = "segment_id,segment_name,distinct_user_count"
ESCAPED = '7,"In-market \\"Home\\" shoppers, US",2'
"""A free-text value with an embedded quote and a comma, in AMC's backslash form."""
DOUBLED = '7,"In-market ""Home"" shoppers, US",2'
"""The same value spelled RFC 4180's way, which AMC was not observed to write."""
VALUE = 'In-market "Home" shoppers, US'


def _text(*lines: str) -> str:
    return "".join(f"{line}\n" for line in (HEADER, *lines))


# --- the dialect ----------------------------------------------------------------------------------


def test_a_backslash_escaped_quote_reads_to_the_exact_value() -> None:
    assert read_rows(_text(ESCAPED)) == (
        ("segment_id", "segment_name", "distinct_user_count"),
        ("7", VALUE, "2"),
    )
    assert escape_finding(_text(ESCAPED)) is None


def test_the_rfc_4180_doubled_quote_is_refused_naming_line_and_column() -> None:
    with pytest.raises(AmcCsvEscapeError) as refused:
        read_rows(_text(ESCAPED, DOUBLED))
    assert refused.value.finding == CsvEscapeFinding(
        line=3,
        column="segment_name",
        problem=(
            "a quoted field holds a doubled quote, which is RFC 4180's escape and not AMC's "
            "backslash"
        ),
    )
    assert refused.value.finding.code == CSV_ESCAPE_AMBIGUOUS
    assert str(refused.value) == (
        "CSV_ESCAPE_AMBIGUOUS at line 3, column segment_name: a quoted field holds a doubled "
        "quote, which is RFC 4180's escape and not AMC's backslash."
    )


def test_a_stray_backslash_is_refused_and_a_doubled_backslash_is_one_backslash() -> None:
    stray = escape_finding(_text('7,"C:\\path",2'))
    assert stray == CsvEscapeFinding(
        line=2,
        column="segment_name",
        problem=(
            "a backslash is followed by neither a quote nor a backslash, and how AMC writes a "
            "literal backslash is unverified"
        ),
    )
    # Control: `\\` is the one accepted spelling of a literal backslash.
    assert read_rows(_text('7,"C:\\\\path",2'))[1] == ("7", "C:\\path", "2")


@pytest.mark.parametrize(
    ("line", "problem"),
    [
        ('7,In-market "Home",2', "a quote sits inside an unquoted field"),
        ('7,"In-market"x,2', "text follows a closing quote"),
    ],
)
def test_a_quote_outside_a_quoted_field_is_refused(line: str, problem: str) -> None:
    assert escape_finding(_text(line)) == CsvEscapeFinding(2, "segment_name", problem)


def test_plain_and_comma_values_and_an_empty_quoted_field_read_as_before() -> None:
    """Controls: nothing an RFC 4180 reader and AMC agree on is refused."""
    text = _text('7,"Home, garden",2', '8,"",2', "9,Plain,2")
    assert read_rows(text)[1:] == (("7", "Home, garden", "2"), ("8", "", "2"), ("9", "Plain", "2"))


def test_the_writer_emits_the_form_the_reader_accepts() -> None:
    written = write_rows(
        ("segment_id", "segment_name", "distinct_user_count"),
        (("7", VALUE, 2), {"segment_id": "8", "segment_name": "C:\\x", "distinct_user_count": 2}),
    )
    assert written == _text(ESCAPED, '8,"C:\\\\x",2')
    assert dict_rows(written)[1][1]["segment_name"] == "C:\\x"


# --- every AMC reader -----------------------------------------------------------------------------


def test_the_load_reader_maps_the_escaped_value_and_refuses_the_doubled_one() -> None:
    document = read(DeliveredFile(_text(ESCAPED), SourceFormat.CSV))
    assert document.rows == (
        {"segment_id": "7", "segment_name": VALUE, "distinct_user_count": "2"},
    )
    with pytest.raises(AmcCsvEscapeError):
        read(DeliveredFile(_text(DOUBLED), SourceFormat.CSV))


def test_the_producer_result_parser_counts_the_escaped_row_and_refuses_the_doubled_one() -> None:
    header = "segment_name,time_window_start,time_window_end,rows_suppressed"
    window = "2026-09-01T00:00:00.000Z,2026-10-01T00:00:00.000Z"
    escaped = f'"In-market \\"Home\\", US",{window},false'
    summary, _evidence = parse_csv_result(f"{header}\n{escaped}\n".encode())
    assert (summary.row_count, summary.columns) == (1, tuple(header.split(",")))
    with pytest.raises(AmcCsvEscapeError):
        parse_csv_result(f"{header}\n{escaped.replace(chr(92) + chr(34), chr(34) * 2)}\n".encode())


def test_a_doubled_quote_delivery_is_blocked_at_load_and_counted_in_blocked_partitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A decided refusal: IngestPartitions Action=blocked, the BlockedPartitions alarm's series."""
    doubled = KEPT.replace("AMAZON.COM", '"AMAZON""COM"', 1)
    assert _partition_actions(monkeypatch, _csv(doubled)) == [[("Action", "blocked")]]
    # The same refusal on the free-text reason column of a withheld row, and its positive
    # control: the reason in AMC's backslash form loads end to end and publishes.
    reason = "1 amazon_attributed_events_by_conversion_time users < 2 required"
    doubled_reason = WITHHELD.replace(reason, f'"{reason} ""path"""', 1)
    escaped_reason = WITHHELD.replace(reason, f'"{reason} \\"path\\""', 1)
    assert _partition_actions(monkeypatch, _csv(KEPT, doubled_reason)) == [[("Action", "blocked")]]
    assert escape_finding(_csv(KEPT, escaped_reason)) is None
    assert _partition_actions(monkeypatch, _csv(KEPT, escaped_reason)) == [
        [("Action", "published")]
    ]


def test_the_finalizer_rejects_a_doubled_quote_as_csv_escape_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _finalizer(monkeypatch)
    pack, registration = _registration()
    version = "registration-version-escape"
    landing = _Landing(registration.registration_key, version)
    header, row = _result().decode().splitlines()
    cells = row.split(",")
    index = header.split(",").index("campaign_id_string")
    cells[index] = '"TRAP""X"'
    landing.objects[(registration.object.key, registration.object.version_id)] = (
        f"{header}\n{','.join(cells)}\n".encode()
    )
    statuses = _Statuses()
    runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, landing, statuses, module.COMMERCE_IDENTITIES
    )
    message = module.FinalizerMessage(BUCKET, registration.registration_key, version)
    with pytest.raises(module.RegistrationContentError) as raised:
        module.finalize_registration(runtime, message, NOW)
    assert raised.value.code == CSV_ESCAPE_AMBIGUOUS
    assert raised.value.detail == (
        "CSV_ESCAPE_AMBIGUOUS at line 2, column campaign_id_string: a quoted field holds a "
        "doubled quote, which is RFC 4180's escape and not AMC's backslash."
    )
    assert landing.puts == []
    # Positive control: the same cell in AMC's backslash form finalizes.
    cells[index] = '"TRAP\\"X"'
    escaped_version = "registration-version-escaped"
    escaped_landing = _Landing(registration.registration_key, escaped_version)
    escaped_landing.objects[(registration.object.key, registration.object.version_id)] = (
        f"{header}\n{','.join(cells)}\n".encode()
    )
    escaped_runtime = module.FinalizerRuntime(
        BUCKET, {pack.pack_id: pack}, escaped_landing, _Statuses(), module.COMMERCE_IDENTITIES
    )
    module.finalize_registration(
        escaped_runtime,
        module.FinalizerMessage(BUCKET, registration.registration_key, escaped_version),
        NOW,
    )
    assert len(escaped_landing.puts) == 2
    assert escaped_landing.puts[0][1] == f"{header}\n{','.join(cells)}\n".encode()
