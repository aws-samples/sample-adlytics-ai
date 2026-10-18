# SPDX-License-Identifier: MIT-0
"""The Baker's composed Athena target waits within its invocation (ADR-144)."""

from __future__ import annotations

import pytest
from bake_application_support import compose_baker

from adapters.outbound.athena.query import BAKE_INVOCATION_WAIT, INTERACTIVE_WAIT, ResultTransport

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("graph_table", ["", "fictional-graph"], ids=["legacy", "graph"])
def test_the_baker_target_uses_the_bake_invocation_wait(
    monkeypatch: pytest.MonkeyPatch, graph_table: str
) -> None:
    _, targets = compose_baker(monkeypatch, graph_table=graph_table)
    (target,) = targets
    # The one shared object, on both returns: never the interactive 30 s wait.
    assert target.wait is BAKE_INVOCATION_WAIT
    assert target.wait is not INTERACTIVE_WAIT and target.wait.name == "bake_invocation"


@pytest.mark.covers("ADR-143")
@pytest.mark.parametrize("graph_table", ["", "fictional-graph"], ids=["legacy", "graph"])
def test_the_baker_target_reads_the_result_object(
    monkeypatch: pytest.MonkeyPatch, graph_table: str
) -> None:
    """Both bake entries stream result objects; neither pages GetQueryResults (chat does)."""
    _, targets = compose_baker(monkeypatch, graph_table=graph_table)
    (target,) = targets
    assert target.results is ResultTransport.OBJECT
    assert target.results is not ResultTransport.PAGED
