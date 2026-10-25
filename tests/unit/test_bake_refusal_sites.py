# SPDX-License-Identifier: MIT-0
"""Every isolated-refusal construction site uses its reason's own keys (ADR-142, 02 §9, M2).

An unknown context key raises `ValueError` at construction, which at runtime would retry and
then settle FAILED. So two halves hold it before a task can reach it:

- **Static scan.** An `ast` walk lists every `IsolatedRefusal(...)` and `note_isolated_refusal(...)`
  call under `src/adapters/` (the bake publisher, the selector, the rosters, the stage), reads its
  literal reason and keyword names, and fails on a keyword outside that reason's allowlist or a
  reason that isn't a literal. The few sites that forward an already-validated refusal are named.
  Paired control: a fixture module with one mis-keyed call fails the scan.
- **Real keys.** The shared seams each source calls are driven to their refusal, so the refusal
  constructs with the keys the source passes; each source's own fixture covers its page case
  (`test_retail_health_preset_source`, `test_publication_selection`, the roster tests).
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from adapters.jobs.preset_artifact_publisher.isolated_refusals import (
    capped_preset_artifact_json,
)
from adapters.jobs.preset_artifact_publisher.scorecard_source import parse_grouped_answer
from domain.operations.bake_refusals import (
    REFUSAL_CONTEXT,
    IsolatedRefusal,
    IsolatedRefusalReason,
    RefusalScope,
)
from domain.presentation.preset_artifacts import MAX_PRESET_ARTIFACT_BYTES
from domain.semantics.time import DateRange
from domain.shared_kernel.errors import InvariantError

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ROOT / "src" / "adapters"
_NON_CONTEXT = frozenset({"reason", "scope", "count", "section"})
FORWARDING: frozenset[tuple[str, str]] = frozenset(
    {
        # Forward a refusal already validated (or its fixed-key figures) to the constructor.
        (
            "src/adapters/jobs/preset_artifact_publisher/isolated_refusals.py",
            "note_isolated_refusal",
        ),
        ("src/adapters/jobs/preset_artifact_publisher/page_bake.py", "_refusal_of"),
        ("src/adapters/jobs/preset_artifact_publisher/page_bake.py", "classify_page_error"),
    }
)
"""Sites that pass a reason variable or `**` figures; each is reviewed, none is a source."""


def _reason(node: ast.expr) -> IsolatedRefusalReason | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "IsolatedRefusalReason"
    ):
        return IsolatedRefusalReason[node.attr]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return IsolatedRefusalReason(node.value)
    return None


def scan(path: Path, relative: str) -> list[str]:
    """Return one finding per mis-keyed or non-literal refusal construction in `path`."""
    findings: list[str] = []
    tree = ast.parse(path.read_text())

    class Visitor(ast.NodeVisitor):
        function = ""

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            held, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = held

        def visit_Call(self, node: ast.Call) -> None:
            name = (
                node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            )
            if name in {"IsolatedRefusal", "note_isolated_refusal"}:
                findings.extend(self._check(node))
            self.generic_visit(node)

        def _check(self, node: ast.Call) -> list[str]:
            where = f"{relative}:{node.lineno}"
            if (relative, self.function) in FORWARDING:
                return []
            literal = (
                node.args[0]
                if node.args
                else next((item.value for item in node.keywords if item.arg == "reason"), None)
            )
            reason = None if literal is None else _reason(literal)
            if reason is None:
                return [f"{where}: the refusal's reason is not a literal"]
            if any(item.arg is None for item in node.keywords):
                return [f"{where}: the refusal's context is a ** expansion"]
            keys = {item.arg for item in node.keywords} - _NON_CONTEXT
            unknown = sorted(cast("set[str]", keys) - REFUSAL_CONTEXT[reason])
            return (
                [f"{where}: {reason.value} does not carry {', '.join(unknown)}"] if unknown else []
            )

    Visitor().visit(tree)
    return findings


def _sites() -> list[tuple[Path, str]]:
    return [
        (path, path.relative_to(ROOT).as_posix())
        for path in sorted(SCANNED.rglob("*.py"))
        if "IsolatedRefusal" in path.read_text() or "note_isolated_refusal" in path.read_text()
    ]


def test_every_refusal_construction_in_the_tree_uses_its_reasons_own_keys() -> None:
    sites = _sites()
    # The scan reaches the sources, the stage and the rosters, not only the page execution.
    scanned = {relative.rsplit("/", 1)[-1] for _, relative in sites}
    assert {
        "audiences_source.py",
        "isolated_refusals.py",
        "page_bake.py",
        "preset_artifacts.py",
        "segment_roster.py",
    } <= scanned
    # ADR-152: Retail, the grouped-answer parser and the spend roster refuse nothing any more.
    assert not {"retail_source.py", "scorecard_source.py", "spend_roster.py"} & scanned
    assert [finding for path, relative in sites for finding in scan(path, relative)] == []


def test_control_a_mis_keyed_fixture_module_fails_the_scan(tmp_path: Path) -> None:
    fixture = tmp_path / "mis_keyed.py"
    fixture.write_text(
        "def stage(n):\n"
        "    raise IsolatedRefusal(\n"
        "        IsolatedRefusalReason.BODIES_OVER_INVOCATION, RefusalScope.PAGE,\n"
        "        observed=n, limit=8,\n"
        "    )\n"
        "def note(reason):\n"
        "    note_isolated_refusal(reason, RefusalScope.ITEM, section='s')\n"
        "def fine(n):\n"
        "    raise IsolatedRefusal(\n"
        "        IsolatedRefusalReason.BODIES_OVER_INVOCATION, RefusalScope.PAGE,\n"
        "        bodies=n, max_bodies=8,\n"
        "    )\n"
    )
    assert scan(fixture, "fixture/mis_keyed.py") == [
        "fixture/mis_keyed.py:2: bodies_over_invocation does not carry limit, observed",
        "fixture/mis_keyed.py:7: the refusal's reason is not a literal",
    ]


# --- real keys at the seams every source calls ----------------------------------------------

_SOURCES = (
    "campaign_source.py",
    "new_customers_source.py",
    "reach_source.py",
    "retail_source.py",
    "retention_source.py",
    "scorecard_source.py",
)


@pytest.mark.parametrize("source", _SOURCES)
def test_every_source_builds_each_body_through_the_cap_check(source: str) -> None:
    """02 §3.4: every source checks each body's encoded size before it builds the request."""
    text = (SCANNED / "jobs" / "preset_artifact_publisher" / source).read_text()
    calls = {
        node.func.id
        for node in ast.walk(ast.parse(text))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "capped_preset_artifact_json" in calls
    assert "canonical_preset_artifact_json" not in calls  # no body built unchecked


def test_an_over_cap_body_is_body_over_cap_with_its_size_and_at_the_cap_it_renders() -> None:
    over: dict[str, object] = {"k": "x" * MAX_PRESET_ARTIFACT_BYTES}
    with pytest.raises(IsolatedRefusal) as refused:
        capped_preset_artifact_json(over)
    assert (refused.value.reason, refused.value.scope) == (
        IsolatedRefusalReason.BODY_OVER_CAP,
        RefusalScope.PAGE,
    )
    assert dict(refused.value.refusal_context) == {
        "encoded_bytes": MAX_PRESET_ARTIFACT_BYTES + len('{"k":""}'),
        "max_bytes": MAX_PRESET_ARTIFACT_BYTES,
    }
    # Paired: exactly at the cap the body renders.
    at_cap: dict[str, object] = {"k": "x" * (MAX_PRESET_ARTIFACT_BYTES - len('{"k":""}'))}
    assert len(capped_preset_artifact_json(at_cap).encode()) == MAX_PRESET_ARTIFACT_BYTES


_PERIOD = DateRange(date(2026, 9, 1), date(2026, 9, 30))


def _slot(*, truncated: bool, rows: int) -> Any:
    metadata = SimpleNamespace(
        truncated=truncated,
        row_count=rows,
        requested_coverage_intervals=(_PERIOD,),
    )
    data = SimpleNamespace(grain=SimpleNamespace(dimensions=("fictional",)), values=())
    logical = SimpleNamespace(metric=SimpleNamespace(name="fictional_metric"))
    return SimpleNamespace(answer=SimpleNamespace(metadata=metadata, data=data), logical=logical)


def test_a_truncated_grouped_answer_is_a_defect_never_a_refusal() -> None:
    """ADR-152: New customers, Reach, Retention and Scorecard read axis-bounded answers here."""
    with pytest.raises(InvariantError) as raised:
        parse_grouped_answer(_slot(truncated=True, rows=50), _PERIOD, ())
    assert not isinstance(raised.value, IsolatedRefusal)
    assert (str(raised.value.code), raised.value.context["max_rows"]) == (
        "ADLY-PRESET_ARTIFACT_BAKE-0006",
        50,
    )
    # Paired: an untruncated but incoherent answer is still None (the page bakes nothing).
    assert parse_grouped_answer(_slot(truncated=False, rows=1), _PERIOD, ()) is None
