# SPDX-License-Identifier: MIT-0
"""Canonical Athena start-envelope parsing and §6 per-unit statement composition.

The canonical start envelope and the one §11 statement byte measure live in
``adapters.outbound.athena.start_envelope``; this module re-exports the composer
and parses a persisted envelope back, refusing any drift from the canonical form.

Per-unit composition (ADR-149) emits exactly two statements whatever the unit's
row count: one insert-only ``MERGE`` that reads the unit's content-addressed
staged object, and one set-based verification whose parameters are the unit's
declared identities. ``stage_unit`` computes the staged object's key and counts
here without writing it; the ``MERGE`` carries that summary and a ``render``
callable, and the statement protocol writes the body just before its ``Begin``.
The composer measures no envelope: the step-2 validator owns that bound. Its one
byte check is the per-record staged-line bound.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import Final, cast

from adapters.outbound.athena.semantic_storage import (
    StagedSource,
    UnitWorkload,
    staged_merge_statement,
    verification_statement,
)
from adapters.outbound.athena.staging import render_staged_body, stage_unit
from adapters.outbound.athena.start_envelope import (
    ENVELOPE_MALFORMED,
    ComposeStartEnvelopeRequest,
    compose_start_envelope,
)
from domain.operations.statement_protocol import MAX_STATEMENT_BYTES, StatementKind
from domain.shared_kernel.errors import ErrorCode, InvariantError, ValidationError
from ports.outbound.unit_staging import STAGING_COMPOSITION_SHAPE, StagedUnit

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

_IDENTIFIER: Final = re.compile(r"^[a-z][a-z0-9_]*$")
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
    """One planned statement: kind, rendered SQL, raw parameters, and its envelope.

    Only the ``MERGE`` carries ``staged`` (the unit's staged summary) and ``render`` (which
    renders its body once). Both stay out of equality: the SQL names the staging key, and the
    summary's length and counts are functions of the bytes that key names.
    """

    kind: StatementKind
    sql: str
    parameters: tuple[str, ...]
    start_envelope: str
    staged: StagedUnit | None = field(default=None, compare=False)
    render: Callable[[], bytes] | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class ComposeUnitStatementsRequest:
    """One unit's complete workload beside the workgroup and staging database it runs with.

    ``max_statement_bytes`` is the §11 ``athenaMaxStatementBytes`` value; entry points
    resolving ``AppConfig.ingestion`` pass it, and the default is the reviewed value. It also
    bounds one record's staged line.
    """

    workload: UnitWorkload
    workgroup: str
    staging_database: str
    max_statement_bytes: int = MAX_STATEMENT_BYTES

    def __post_init__(self) -> None:
        if not self.workgroup.strip():
            raise ValidationError(
                ENVELOPE_MALFORMED, "unit statement composition requires a workgroup."
            )
        if not _IDENTIFIER.fullmatch(self.staging_database):
            raise ValidationError(
                ENVELOPE_MALFORMED,
                "unit statement composition requires a generated staging database name.",
            )
        if isinstance(self.max_statement_bytes, bool) or self.max_statement_bytes < 1:
            raise ValidationError(
                ENVELOPE_MALFORMED, "the statement byte bound must be a positive integer."
            )


@dataclass(frozen=True, slots=True)
class UnitStatementComposition:
    """The unit's closed statement roster: one staged ``MERGE`` then one verification."""

    statements: tuple[ComposedStatement, ...]

    def __post_init__(self) -> None:
        shape = tuple(statement.kind for statement in self.statements)
        if shape != (StatementKind.MERGE, StatementKind.VERIFICATION):
            raise _shape_error()
        merge, verification = self.statements
        if merge.staged is None or merge.render is None:
            raise _shape_error()
        if verification.staged is not None or verification.render is not None:
            raise _shape_error()

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


def _shape_error() -> InvariantError:
    return InvariantError(
        STAGING_COMPOSITION_SHAPE,
        "a unit composition is exactly one staged MERGE then one verification.",
    )


def compose_unit_statements(request: ComposeUnitStatementsRequest) -> UnitStatementComposition:
    """Compose one unit's staged ``MERGE`` and its single set-based verification.

    Identities keep the roster's canonical object order, and each identity's records are
    staged in grain order. A record that cannot be staged, including one whose staged line
    exceeds ``max_statement_bytes``, raises ``STATEMENT_UNPACKABLE`` before authority;
    nothing is truncated.
    """
    workload = request.workload
    table = workload.table
    try:
        staged = stage_unit(workload, max_record_bytes=request.max_statement_bytes)
    except ValueError as error:
        raise ValidationError(
            STATEMENT_UNPACKABLE,
            f"one record can't be staged within the {request.max_statement_bytes}-byte "
            "§11 statement bound; the delivery is refused before authority.",
            context={"table": table.name, "max_statement_bytes": request.max_statement_bytes},
        ) from error
    source = StagedSource(request.staging_database, staged.staging_key)
    merge_sql = staged_merge_statement(table, source)
    merge = ComposedStatement(
        kind=StatementKind.MERGE,
        sql=merge_sql,
        parameters=(),
        start_envelope=compose_start_envelope(
            ComposeStartEnvelopeRequest(
                workgroup=request.workgroup,
                database=workload.database,
                sql=merge_sql,
                parameters=(),
            )
        ),
        staged=staged,
        render=partial(render_staged_body, workload, staged),
    )
    identities = tuple(identity for identity, _ in workload.records_by_identity)
    verification_sql = verification_statement(table, source, len(identities))
    verification = ComposedStatement(
        kind=StatementKind.VERIFICATION,
        sql=verification_sql,
        parameters=identities,
        start_envelope=compose_start_envelope(
            ComposeStartEnvelopeRequest(
                workgroup=request.workgroup,
                database=workload.database,
                sql=verification_sql,
                parameters=identities,
            )
        ),
    )
    return UnitStatementComposition(statements=(merge, verification))
