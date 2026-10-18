# SPDX-License-Identifier: MIT-0
"""The bake statement deadline: the instant a bake's Athena statements must finish by (ADR-144).

The page execution (`page_bake.render_page`) sets it from the invocation's remaining time minus
its row's write term and the contract's tail, and resets it when the page returns. Every bake
statement runs inside that scope on either bake entry, and the statement threads copy the
caller's context, so each reads the same instant. Outside the scope it is unset, and a bake
statement asking for it is a defect: no bake statement runs without a deadline.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Final

from domain.shared_kernel.errors import ErrorCode, InvariantError

__all__ = [
    "BAKE_DEADLINE_UNSET",
    "bake_statement_deadline",
    "reset_bake_statement_deadline",
    "set_bake_statement_deadline",
]

BAKE_DEADLINE_UNSET: Final = ErrorCode("ATHENA", 5)
"""A bake statement asked for its deadline outside the page execution that sets it."""

_DEADLINE: ContextVar[float | None] = ContextVar("bake_statement_deadline", default=None)


def set_bake_statement_deadline(deadline: float) -> Token[float | None]:
    """Bind the monotonic instant this page's bake statements must finish by."""
    return _DEADLINE.set(deadline)


def reset_bake_statement_deadline(token: Token[float | None]) -> None:
    """Restore the deadline the page execution found (unset, outside any page)."""
    _DEADLINE.reset(token)


def bake_statement_deadline() -> float:
    """Return the bound deadline (monotonic seconds); unset is an invariant (ATHENA/5)."""
    deadline = _DEADLINE.get()
    if deadline is None:
        raise InvariantError(
            BAKE_DEADLINE_UNSET,
            "A bake statement ran outside the page execution that sets its deadline.",
            context={"wait_policy": "bake_invocation"},
        )
    return deadline
