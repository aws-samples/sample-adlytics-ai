# SPDX-License-Identifier: MIT-0
"""The canonical Athena start envelope and the one §11 statement byte measure.

The canonical start envelope is the adapter-owned §3.1 form each attempt stores
immutably and replays byte-identically after process loss. It is one canonical
JSON object — key-sorted, minimal separators, UTF-8, never ASCII-escaped — with
exactly the closed key set ``Database``, ``QueryString``, ``WorkGroup``, and,
only when the statement has parameters, ``ExecutionParameters``. Athena rejects
an empty ``ExecutionParameters`` list, so a parameterless envelope omits the key
entirely. Every parameter is encoded with ``sql_literal`` here, at composition —
the envelope stores the exact ``ExecutionParameters`` entries the SDK receives,
so replay needs no second encoder and the stored bytes are the complete request
apart from the client request token and the constant disabled result reuse. The
envelope contains no result rows and no credentials.

The §11 ``athenaMaxStatementBytes`` bound is measured on these exact UTF-8 bytes:
the SQL text and every encoded parameter together. There is no parameter-count
bound. Athena documents none for ``ExecutionParameters`` (each entry at most 1024
characters) and a 262144-character ``QueryString`` ceiling, and the same bytes are
persisted in the DynamoDB ``ATTEMPT`` item, so the one byte measure bounds the
Athena request and the stored attempt together (write-discipline §11 amendment,
2026-10-10). It lives here, below both the §6 packer and the §5 statement
composer, so the packer that sets ``M(u)`` and the step-2 validator that refuses
an oversized start can never measure differently.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from adapters.outbound.athena.parameters import sql_literal
from domain.shared_kernel.errors import ErrorCode, ValidationError

__all__ = [
    "ENVELOPE_MALFORMED",
    "ComposeStartEnvelopeRequest",
    "StatementFits",
    "compose_start_envelope",
    "envelope_fits",
]

ENVELOPE_MALFORMED: Final = ErrorCode("ATHENA_START", 1)

type StatementFits = Callable[[str, tuple[str, ...]], bool]
"""Whether one rendered statement (SQL text, raw parameters) fits the §11 byte bound."""


@dataclass(frozen=True, slots=True)
class ComposeStartEnvelopeRequest:
    """One statement's exact start facts; parameters are the renderer's raw values."""

    workgroup: str
    database: str
    sql: str
    parameters: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("workgroup", "database", "sql"):
            value: str = getattr(self, name)
            if not value.strip():
                raise ValidationError(
                    ENVELOPE_MALFORMED, f"a start envelope requires a nonblank {name}."
                )


def compose_start_envelope(request: ComposeStartEnvelopeRequest) -> str:
    """Render the one canonical start envelope for a statement, byte-deterministically."""
    payload: dict[str, object] = {
        "Database": request.database,
        "QueryString": request.sql,
        "WorkGroup": request.workgroup,
    }
    if request.parameters:
        payload["ExecutionParameters"] = [sql_literal(value) for value in request.parameters]
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def envelope_fits(workgroup: str, database: str, max_statement_bytes: int) -> StatementFits:
    """Return the §11 byte test: the statement's canonical envelope is within the bound."""
    if isinstance(max_statement_bytes, bool) or max_statement_bytes < 1:
        raise ValueError("the statement byte bound must be a positive integer.")

    def fits(sql: str, parameters: tuple[str, ...]) -> bool:
        envelope = compose_start_envelope(
            ComposeStartEnvelopeRequest(
                workgroup=workgroup, database=database, sql=sql, parameters=parameters
            )
        )
        return len(envelope.encode("utf-8")) <= max_statement_bytes

    return fits
