# SPDX-License-Identifier: MIT-0
"""Canonical Athena start-envelope parsing and §6 per-unit statement composition.

The canonical start envelope and the one §11 statement byte measure live in
``adapters.outbound.athena.start_envelope``; this module re-exports the composer
and parses a persisted envelope back, refusing any drift from the canonical form.

Per-unit composition delegates to the single §6 existing ordering helper and
packer — ``ordered_records``, ``pack_object_batches``, ``merge_statement``, and
``multi_select_statement`` — never a preflight-only implementation. A unit
composes ``M(u)`` merge statements followed by exactly one verification whose
parameters are the unit's declared identities. Merges are bounded only by the
``athenaMaxStatementBytes`` envelope bytes; there is no parameter-count split.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final, cast

from adapters.outbound.athena.semantic_storage import (
    UnitWorkload,
    merge_statement,
    multi_select_statement,
    ordered_records,
    pack_object_batches,
)
from adapters.outbound.athena.start_envelope import (
    ENVELOPE_MALFORMED,
    ComposeStartEnvelopeRequest,
    compose_start_envelope,
    envelope_fits,
)
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES, StatementKind
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "ENVELOPE_MALFORMED",
    "STATEMENT_UNPACKABLE",
    "ComposeStartEnvelopeRequest",
    "ComposeUnitStatementsRequest",
    "ComposedStatement",
    "ParsedStartEnvelope",
    "UnitStatementComposition",
    "compose_start_envelope",
    "compose_unit_statements",
    "parse_start_envelope",
]

STATEMENT_UNPACKABLE: Final = ErrorCode("ATHENA_START", 2)

_REQUIRED_KEYS: Final = frozenset({"Database", "QueryString", "WorkGroup"})
_OPTIONAL_KEYS: Final = frozenset({"ExecutionParameters"})


@dataclass(frozen=True, slots=True)
class ParsedStartEnvelope:
    """The decoded closed envelope; ``execution_parameters`` are already encoded."""

    workgroup: str
    database: str
    query_string: str
    execution_parameters: tuple[str, ...]


def _malformed(description: str) -> ValidationError:
    return ValidationError(ENVELOPE_MALFORMED, description)


def parse_start_envelope(text: str) -> ParsedStartEnvelope:
    """Decode one persisted canonical start envelope, refusing any drift.

    The closed key set, member types, and exact canonical rendering are all
    required: an envelope whose bytes do not reproduce from its own decoded form
    is not the envelope Begin persisted, and replaying it would break the
    byte-identical replay contract.
    """
    try:
        decoded: object = json.loads(text)
    except ValueError as error:
        raise _malformed("a start envelope must be one canonical JSON object.") from error
    if not isinstance(decoded, dict):
        raise _malformed("a start envelope must be one canonical JSON object.")
    members: dict[str, object] = {
        str(name): member for name, member in cast("dict[object, object]", decoded).items()
    }
    keys = frozenset(members)
    if not (_REQUIRED_KEYS <= keys <= _REQUIRED_KEYS | _OPTIONAL_KEYS):
        raise _malformed("a start envelope carries exactly the closed key set.")
    values: dict[str, str] = {}
    for name in sorted(_REQUIRED_KEYS):
        member = members[name]
        if not isinstance(member, str) or not member.strip():
            raise _malformed(f"envelope member {name} must be nonblank text.")
        values[name] = member
    parameters: tuple[str, ...] = ()
    if "ExecutionParameters" in members:
        raw = members["ExecutionParameters"]
        if not isinstance(raw, list) or not raw:
            raise _malformed("ExecutionParameters must be a nonempty list of text entries.")
        entries = cast("list[object]", raw)
        if any(not isinstance(entry, str) for entry in entries):
            raise _malformed("ExecutionParameters must be a nonempty list of text entries.")
        parameters = tuple(cast("list[str]", entries))
    canonical = json.dumps(
        members,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != text:
        raise _malformed("a start envelope must be its own exact canonical rendering.")
    return ParsedStartEnvelope(
        workgroup=values["WorkGroup"],
        database=values["Database"],
        query_string=values["QueryString"],
        execution_parameters=parameters,
    )


@dataclass(frozen=True, slots=True)
class ComposedStatement:
    """One planned statement: kind, rendered SQL, raw parameters, and its envelope."""

    kind: StatementKind
    sql: str
    parameters: tuple[str, ...]
    start_envelope: str


@dataclass(frozen=True, slots=True)
class ComposeUnitStatementsRequest:
    """One unit's complete workload beside the workgroup its statements run in.

    ``max_statement_bytes`` is the §11 ``athenaMaxStatementBytes`` value; entry points
    resolving ``AppConfig.ingestion`` pass it, and the default is the reviewed value.
    """

    workload: UnitWorkload
    workgroup: str
    max_statement_bytes: int = MAX_STATEMENT_BYTES

    def __post_init__(self) -> None:
        if not self.workgroup.strip():
            raise ValidationError(
                ENVELOPE_MALFORMED, "unit statement composition requires a workgroup."
            )
        if isinstance(self.max_statement_bytes, bool) or self.max_statement_bytes < 1:
            raise ValidationError(
                ENVELOPE_MALFORMED, "the statement byte bound must be a positive integer."
            )


@dataclass(frozen=True, slots=True)
class UnitStatementComposition:
    """The unit's closed statement roster: ``M(u)`` merges then one verification."""

    statements: tuple[ComposedStatement, ...]

    @property
    def merge_count(self) -> int:
        """Return ``M(u)``: every statement except the final verification."""
        return len(self.statements) - 1

    @property
    def merges(self) -> tuple[ComposedStatement, ...]:
        """Return the ordered merge statements."""
        return self.statements[:-1]

    @property
    def verification(self) -> ComposedStatement:
        """Return the exactly-one final verification statement."""
        return self.statements[-1]


def compose_unit_statements(request: ComposeUnitStatementsRequest) -> UnitStatementComposition:
    """Compose one unit's merges and single verification through the §6 packer.

    Objects must arrive in the roster's canonical object order; this function
    preserves that order, packs whole objects greedily so each merge's canonical
    start envelope stays within ``max_statement_bytes``, and never reorders a
    fragment. One record whose own merge exceeds the bound raises
    ``STATEMENT_UNPACKABLE`` before authority; nothing is truncated.
    """
    table = request.workload.table
    objects = tuple(
        ordered_records(table, records) for _, records in request.workload.records_by_identity
    )
    fits = envelope_fits(request.workgroup, request.workload.database, request.max_statement_bytes)
    try:
        batches = pack_object_batches(table, objects, fits)
    except ValueError as error:
        raise ValidationError(
            STATEMENT_UNPACKABLE,
            f"one immutable record's merge exceeds the {request.max_statement_bytes}-byte "
            "§11 statement bound; the delivery is refused before authority.",
            context={"table": table.name, "max_statement_bytes": request.max_statement_bytes},
        ) from error
    statements: list[ComposedStatement] = []
    for batch in batches:
        sql, parameters = merge_statement(table, batch)
        statements.append(
            ComposedStatement(
                kind=StatementKind.MERGE,
                sql=sql,
                parameters=parameters,
                start_envelope=compose_start_envelope(
                    ComposeStartEnvelopeRequest(
                        workgroup=request.workgroup,
                        database=request.workload.database,
                        sql=sql,
                        parameters=parameters,
                    )
                ),
            )
        )
    identities = tuple(identity for identity, _ in request.workload.records_by_identity)
    verification_sql = multi_select_statement(table, len(identities))
    statements.append(
        ComposedStatement(
            kind=StatementKind.VERIFICATION,
            sql=verification_sql,
            parameters=identities,
            start_envelope=compose_start_envelope(
                ComposeStartEnvelopeRequest(
                    workgroup=request.workgroup,
                    database=request.workload.database,
                    sql=verification_sql,
                    parameters=identities,
                )
            ),
        )
    )
    return UnitStatementComposition(statements=tuple(statements))
