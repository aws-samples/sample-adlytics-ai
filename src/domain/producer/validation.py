# SPDX-License-Identifier: MIT-0
"""Pure conformance checks shared by producer tooling and finalization."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from domain.producer.models import (
    PACK_VARIANT_OPTION,
    PERIOD_GRAIN_OPTION,
    GovernedQueryPack,
    PeriodGrain,
    ResultSummary,
    SourceRegistration,
)
from domain.producer.parameters import (
    CommerceIdentityRequest,
    ParameterResolutionError,
    resolve_commerce_identities,
    resolve_period_parameters,
)
from domain.producer.periods import is_aligned
from domain.semantics.commerce_identity import CommerceIdentityBridge

DISCRIMINATOR_OPTION = "filteredMetricsDiscriminatorColumn"
REASON_OPTION = "filteredReasonColumn"
USER_COUNT_OPTION = "distinctUserCountColumn"
WINDOW_TYPE_OPTION = "timeWindowType"
WINDOW_START_OPTION = "timeWindowStart"
WINDOW_END_OPTION = "timeWindowEnd"
WINDOW_ZONE_OPTION = "timeWindowTimeZone"

REQUIRED_EXECUTION_OPTION_KEYS = frozenset(
    {
        DISCRIMINATOR_OPTION,
        REASON_OPTION,
        USER_COUNT_OPTION,
        WINDOW_TYPE_OPTION,
        WINDOW_ZONE_OPTION,
    }
)
CUSTOM_PARAMETER = re.compile(r"CUSTOM_PARAMETER\(\s*'([^']*)'\s*\)", re.IGNORECASE)
REQUIRED_RESULT_COLUMNS = frozenset(
    {
        "time_window_start",
        "time_window_end",
        "rows_suppressed",
        "suppression_reason",
        "distinct_user_count",
    }
)


@dataclass(frozen=True, slots=True)
class ConformanceFinding:
    """One stable machine-readable producer contract violation."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.detail.strip():
            raise ValueError("a conformance finding requires code and detail.")


@dataclass(frozen=True, slots=True)
class ConformanceReport:
    """Every finding from one validation pass, never only the first."""

    findings: tuple[ConformanceFinding, ...] = ()

    @property
    def valid(self) -> bool:
        """Whether the input satisfies every checked producer obligation."""
        return not self.findings

    def combined(self, other: ConformanceReport) -> ConformanceReport:
        """Combine independent checks without losing either failure set."""
        return ConformanceReport((*self.findings, *other.findings))


def _parameter_findings(pack: GovernedQueryPack) -> tuple[ConformanceFinding, ...]:
    """Bind declared `inputParameters` to the SQL's `CUSTOM_PARAMETER` references, both ways.

    A reference with no declaration has no governed value, and a declaration the SQL never reads
    fills nothing; either way the parameter isn't what the registration says it is.
    """
    referenced = frozenset(CUSTOM_PARAMETER.findall(pack.sql))
    declared = frozenset(parameter.name for parameter in pack.input_parameters)
    findings: list[ConformanceFinding] = []
    undeclared = sorted(referenced - declared)
    unused = sorted(declared - referenced)
    if undeclared:
        findings.append(
            ConformanceFinding(
                "PACK_PARAMETER_UNDECLARED",
                f"SQL reads CUSTOM_PARAMETER {', '.join(undeclared)} with no "
                "inputParameters entry.",
            )
        )
    if unused:
        findings.append(
            ConformanceFinding(
                "PACK_PARAMETER_UNUSED",
                f"inputParameters {', '.join(unused)} never appear as CUSTOM_PARAMETER in the SQL.",
            )
        )
    return tuple(findings)


def validate_pack(pack: GovernedQueryPack) -> ConformanceReport:
    """Check SQL and output invariants that every governed AMC pack shares."""
    findings: list[ConformanceFinding] = []
    sql_upper = pack.sql.upper()
    if "\n" in pack.sql or "\t" in pack.sql:
        findings.append(
            ConformanceFinding("PACK_SQL_NOT_SINGLE_LINE", "AMC execution SQL must be one line.")
        )
    if "SELECT *" in sql_upper:
        findings.append(
            ConformanceFinding("PACK_SELECT_STAR", "AMC query packs must never use SELECT *.")
        )
    if not sql_upper.startswith(("SELECT ", "WITH ")):
        findings.append(
            ConformanceFinding("PACK_SQL_SHAPE", "AMC SQL must begin with SELECT or WITH.")
        )
    if not any(token in sql_upper for token in ("SUM(", "COUNT(", "AVG(", "MIN(", "MAX(")):
        findings.append(
            ConformanceFinding("PACK_NO_AGGREGATION", "Every AMC query requires an aggregation.")
        )
    for parameter, alias in (
        ("TIME_WINDOW_START", "time_window_start"),
        ("TIME_WINDOW_END", "time_window_end"),
    ):
        required = f"BUILT_IN_PARAMETER('{parameter}') AS {alias}".upper()
        if required not in sql_upper:
            findings.append(
                ConformanceFinding(
                    "PACK_WINDOW_NOT_STAMPED",
                    f"query must select {parameter} as {alias}.",
                )
            )
    missing_common = sorted(REQUIRED_RESULT_COLUMNS - set(pack.expected_columns))
    if missing_common:
        findings.append(
            ConformanceFinding(
                "PACK_COMMON_COLUMNS_MISSING",
                f"expected columns omit {', '.join(missing_common)}.",
            )
        )
    findings.extend(_parameter_findings(pack))
    execution_options = dict(pack.execution_options)
    if frozenset(execution_options) != REQUIRED_EXECUTION_OPTION_KEYS:
        findings.append(
            ConformanceFinding(
                "PACK_EXECUTION_OPTIONS",
                "pack must bind the complete explicit-window and suppression option set.",
            )
        )
    elif (
        execution_options[WINDOW_TYPE_OPTION] != "EXPLICIT"
        or execution_options[WINDOW_ZONE_OPTION] != "UTC"
        or execution_options[DISCRIMINATOR_OPTION] != "rows_suppressed"
        or execution_options[REASON_OPTION] != "suppression_reason"
        or execution_options[USER_COUNT_OPTION] != "distinct_user_count"
    ):
        findings.append(
            ConformanceFinding(
                "PACK_EXECUTION_OPTION_VALUES",
                "pack execution options do not match the governed UTC/suppression contract.",
            )
        )
    return ConformanceReport(tuple(findings))


def validate_result(pack: GovernedQueryPack, result: ResultSummary) -> ConformanceReport:
    """Check one parsed CSV against its pack without depending on column order."""
    findings: list[ConformanceFinding] = []
    delivered = set(result.columns)
    expected = set(pack.expected_columns)
    missing = sorted(expected - delivered)
    extra = sorted(delivered - expected)
    if missing:
        findings.append(
            ConformanceFinding("RESULT_COLUMNS_MISSING", f"result is missing {', '.join(missing)}.")
        )
    if extra:
        findings.append(
            ConformanceFinding("RESULT_COLUMNS_UNEXPECTED", f"result adds {', '.join(extra)}.")
        )
    if len(result.columns) != len(delivered):
        findings.append(
            ConformanceFinding("RESULT_COLUMNS_DUPLICATE", "result header repeats a column.")
        )
    if pack.reject_suppressed_rows and result.suppressed_row_count > 0:
        findings.append(
            ConformanceFinding(
                "RESULT_SUPPRESSION_REFUSED",
                "pack policy refuses any result containing a suppressed row.",
            )
        )
    if result.row_count > 0:
        if len(result.time_window_starts) != 1 or "" in result.time_window_starts:
            findings.append(
                ConformanceFinding(
                    "RESULT_WINDOW_START_INCONSISTENT",
                    "every result row must carry one nonblank time_window_start.",
                )
            )
        if len(result.time_window_ends) != 1 or "" in result.time_window_ends:
            findings.append(
                ConformanceFinding(
                    "RESULT_WINDOW_END_INCONSISTENT",
                    "every result row must carry one nonblank time_window_end.",
                )
            )
    return validate_pack(pack).combined(ConformanceReport(tuple(findings)))


def _window_moment(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace(" ", "T")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _period_window_findings(registration: SourceRegistration) -> tuple[ConformanceFinding, ...]:
    start_moment = _window_moment(registration.option_set.get(WINDOW_START_OPTION))
    end_moment = _window_moment(registration.option_set.get(WINDOW_END_OPTION))
    if start_moment is None or end_moment is None:
        return (
            ConformanceFinding(
                "REGISTRATION_PERIOD_WINDOW",
                "registration window bounds must be parseable UTC timestamps.",
            ),
        )
    inclusive_end = end_moment - timedelta(microseconds=1)
    if (
        end_moment > start_moment
        and registration.period_start == start_moment.date()
        and registration.period_end == inclusive_end.date()
    ):
        return ()
    return (
        ConformanceFinding(
            "REGISTRATION_PERIOD_WINDOW",
            "registration period must match the explicit half-open UTC window.",
        ),
    )


def _registration_option_findings(
    pack: GovernedQueryPack,
    registration: SourceRegistration,
) -> tuple[ConformanceFinding, ...]:
    options = registration.option_set
    expected_option_names = {
        *(name for name, _value in pack.execution_options),
        WINDOW_START_OPTION,
        WINDOW_END_OPTION,
        *((PERIOD_GRAIN_OPTION,) if pack.period_policy is not None else ()),
        *((PACK_VARIANT_OPTION,) if pack.variant_id is not None else ()),
        *(parameter.name for parameter in pack.input_parameters),
    }
    findings: list[ConformanceFinding] = []
    if set(options) != expected_option_names:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_OPTION_KEYS",
                "registration options must exactly match the installed pack's options, window "
                "bounds, period grain and parameters.",
            )
        )
    if pack.variant_id is not None and options.get(PACK_VARIANT_OPTION) != pack.variant_id:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_PACK_VARIANT",
                f"option {PACK_VARIANT_OPTION} must name variant {pack.variant_id!r}, the one "
                "its pack version binds.",
            )
        )
    for name, expected in pack.execution_options:
        if options.get(name) != expected:
            findings.append(
                ConformanceFinding(
                    "REGISTRATION_OPTION",
                    f"option {name} must equal {expected!r}.",
                )
            )
    for name in (WINDOW_START_OPTION, WINDOW_END_OPTION):
        value = options.get(name)
        if not isinstance(value, str) or not value.strip():
            findings.append(
                ConformanceFinding(
                    "REGISTRATION_WINDOW_OPTION",
                    f"option {name} must be a nonblank string.",
                )
            )
    return tuple(findings)


def _period_grain_findings(
    pack: GovernedQueryPack, registration: SourceRegistration
) -> tuple[ConformanceFinding, ...]:
    """Require a periodic delivery to name one declared grain and cover one whole such period."""
    policy = pack.period_policy
    if policy is None:
        return ()
    declared = registration.option_set.get(PERIOD_GRAIN_OPTION)
    grain = next((item for item in policy.grains if item.value == declared), None)
    if grain is None:
        return (
            ConformanceFinding(
                "REGISTRATION_PERIOD_GRAIN",
                f"option {PERIOD_GRAIN_OPTION} must be one of "
                f"{', '.join(item.value for item in policy.grains)}.",
            ),
        )
    if is_aligned(grain, (registration.period_start, registration.period_end)):
        return ()
    return (
        ConformanceFinding(
            "REGISTRATION_PERIOD_ALIGNMENT",
            f"period {registration.period_start}..{registration.period_end} is not one whole "
            f"{_ALIGNMENT_LABELS[grain]}.",
        ),
    )


_ALIGNMENT_LABELS = {
    PeriodGrain.WEEK: "ISO week (Monday to Sunday, UTC)",
    PeriodGrain.MONTH: "calendar month (UTC)",
    PeriodGrain.QUARTER: "calendar quarter (UTC)",
    PeriodGrain.CUSTOM: "cohort vintage (13 calendar months ending at a month end, UTC)",
}


def _parameter_value_findings(
    pack: GovernedQueryPack,
    registration: SourceRegistration,
    bridge: CommerceIdentityBridge | None,
) -> tuple[ConformanceFinding, ...]:
    """Re-resolve every parameter from the governed bridge and refuse a mismatch (ADR-110 pt 2).

    A period-derived parameter (a cohort vintage's `as_of_month`) is re-derived from the
    registration's own period, so a delivery can't name one vintage's window with another's month.
    """
    if not pack.input_parameters:
        return ()
    period = (registration.period_start, registration.period_end)
    derived = resolve_period_parameters(pack, period)
    findings = tuple(
        ConformanceFinding(
            "REGISTRATION_PARAMETER",
            f"parameter {name} must equal {value!r}, derived from the registration's period.",
        )
        for name, value in derived.items()
        if registration.option_set.get(name) != value
    )
    if all(parameter.commerce_identity is None for parameter in pack.input_parameters):
        return findings
    if bridge is None:
        return (
            ConformanceFinding(
                "REGISTRATION_PARAMETER_UNRESOLVED",
                "pack declares inputParameters but no governed commerce identities were loaded.",
            ),
        )
    try:
        resolved = resolve_commerce_identities(
            bridge,
            CommerceIdentityRequest(
                pack=pack,
                marketplace=registration.marketplace,
                period=(registration.period_start, registration.period_end),
            ),
        )
    except ParameterResolutionError as error:
        return (*findings, ConformanceFinding("REGISTRATION_PARAMETER_UNRESOLVED", str(error)))
    return (
        *findings,
        *(
            ConformanceFinding(
                "REGISTRATION_PARAMETER",
                f"parameter {parameter.name} must equal the governed "
                f"{parameter.commerce_identity} value {resolved[parameter.commerce_identity]!r}.",
            )
            for parameter in pack.input_parameters
            if parameter.commerce_identity is not None
            and registration.option_set.get(parameter.name) != resolved[parameter.commerce_identity]
        ),
    )


def validate_registration(
    pack: GovernedQueryPack,
    registration: SourceRegistration,
    bridge: CommerceIdentityBridge | None = None,
) -> ConformanceReport:
    """Check source, pack, options, path, and exact-version identity together.

    `bridge` is the loaded commerce identity bridge. Only a pack with `inputParameters` needs it,
    and such a pack fails closed without it.
    """
    findings: list[ConformanceFinding] = []
    for blocker in pack.safety_blockers:
        findings.append(
            ConformanceFinding(
                "PACK_SAFETY_BLOCKED",
                f"query pack is blocked until {blocker} is resolved.",
            )
        )
    if registration.source_system != pack.source_system:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_SOURCE_SYSTEM",
                f"registration source_system must be {pack.source_system!r}.",
            )
        )
    if registration.source_dataset != pack.source_dataset:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_SOURCE_DATASET",
                f"registration source_dataset must be {pack.source_dataset!r}.",
            )
        )
    if pack.source_system == "amc" and registration.source_scope.namespace != "amc.entity":
        findings.append(
            ConformanceFinding(
                "REGISTRATION_SOURCE_SCOPE_NAMESPACE",
                "AMC registration source_scope namespace must be 'amc.entity'.",
            )
        )
    if pack.source_system == "amc" and registration.source_watermark is None:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_WATERMARK_REQUIRED",
                "AMC registration requires the execution-date source watermark.",
            )
        )
    selection = registration.query_pack
    if selection.pack_id != pack.pack_id or selection.version != pack.pack_version:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_PACK_IDENTITY",
                f"registration must bind {pack.pack_id}@{pack.pack_version}.",
            )
        )
    if selection.query_digest != pack.query_digest:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_QUERY_DIGEST",
                "registration query digest does not match the installed pack.",
            )
        )
    expected_key = f"{registration.incoming_prefix}result.csv"
    if registration.object.key != expected_key:
        findings.append(
            ConformanceFinding(
                "REGISTRATION_OBJECT_KEY",
                f"registered object must be the sibling key {expected_key!r}.",
            )
        )
    findings.extend(_registration_option_findings(pack, registration))
    findings.extend(_period_window_findings(registration))
    findings.extend(_period_grain_findings(pack, registration))
    findings.extend(_parameter_value_findings(pack, registration, bridge))
    retrieved_utc = registration.retrieved_at.astimezone(UTC)
    if (
        registration.source_watermark is not None
        and registration.source_watermark > retrieved_utc.date()
    ):
        findings.append(
            ConformanceFinding(
                "REGISTRATION_WATERMARK_FUTURE",
                "source_watermark cannot be later than retrieved_at.",
            )
        )
    return validate_pack(pack).combined(ConformanceReport(tuple(findings)))


def validate_result_window(
    registration: SourceRegistration, result: ResultSummary
) -> ConformanceReport:
    """Bind stamped result windows to the exact options declared by registration."""
    if result.row_count == 0:
        return ConformanceReport()
    findings: list[ConformanceFinding] = []
    expected_start = _window_moment(registration.option_set.get(WINDOW_START_OPTION))
    expected_end = _window_moment(registration.option_set.get(WINDOW_END_OPTION))
    actual_starts = {_window_moment(value) for value in result.time_window_starts}
    actual_ends = {_window_moment(value) for value in result.time_window_ends}
    if expected_start is None or actual_starts != {expected_start}:
        findings.append(
            ConformanceFinding(
                "RESULT_WINDOW_START_MISMATCH",
                "result time_window_start does not match registration options.",
            )
        )
    if expected_end is None or actual_ends != {expected_end}:
        findings.append(
            ConformanceFinding(
                "RESULT_WINDOW_END_MISMATCH",
                "result time_window_end does not match registration options.",
            )
        )
    return ConformanceReport(tuple(findings))
