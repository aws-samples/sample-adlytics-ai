# SPDX-License-Identifier: MIT-0
"""Presentation's read-only view of graph serving authority (S6 §2.9 "Presentation")."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domain.operations.execution_graph_release import ServingBinding

__all__ = ["PresentationGraphReadPort"]


@runtime_checkable
class PresentationGraphReadPort(Protocol):
    """The mode item's `GRAPH` entry instant and one grant's `SERVING#` binding, read only."""

    def graph_entered_at(self) -> str | None:
        """Return the normalized `GRAPH` entry instant, or None outside `GRAPH`.

        Read once per container once it is set (the CAS sets it once). An entry instant that
        can't be normalized is an `InvariantError`; a read failure is a `DependencyError`.
        """
        ...

    def serving_binding(self, grant_id: str) -> ServingBinding | None:
        """Return one grant's serving binding, or None while its cohort is open."""
        ...
