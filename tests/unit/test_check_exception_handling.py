# SPDX-License-Identifier: MIT-0
"""The except-block gate refuses a handler that discards its error and accepts each sanctioned form.

Each refusal compares the whole finding list and is paired with an accepted control, so a rule that
stopped refusing and a rule that started refusing compliant code both fail here.
"""

import ast
import importlib.util
from pathlib import Path
from textwrap import dedent
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-133")]

ROOT = Path(__file__).resolve().parents[2]
REL = "src/x.py"
DISCARDS = (
    "discards its error; re-raise, translate, report_error, or justify with "
    "'# error-propagation: <reason>'"
)
RECONCILER = ROOT / "src/adapters/jobs/operational_status/metric_reconciler.py"


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """Load the hyphenated script as a module so `findings` is called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_exception_handling", ROOT / "scripts" / "check-exception-handling.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _findings(checker: ModuleType, source: str) -> list[str]:
    return checker.findings(REL, dedent(source)).findings


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception:
                    pass
            """,
            [f"{REL}:5: except Exception {DISCARDS}"],
            id="pass",
        ),
        pytest.param(
            """
            def f(items):
                for item in items:
                    try:
                        g(item)
                    except Exception:
                        continue
            """,
            [f"{REL}:6: except Exception {DISCARDS}"],
            id="continue",
        ),
        pytest.param(
            """
            def f():
                try:
                    return g()
                except Exception:
                    return None
            """,
            [f"{REL}:5: except Exception {DISCARDS}"],
            id="return-none",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except BaseException as error:
                    return type(error).__name__
            """,
            [f"{REL}:5: except BaseException {DISCARDS}"],
            id="only-type-name",
        ),
        pytest.param(
            """
            def f(logger):
                try:
                    g()
                except Exception as error:
                    logger.warning("failed", error=error)
            """,
            [f"{REL}:5: except Exception {DISCARDS}"],
            id="only-logged",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception as error:
                    describe(error)
            """,
            [f"{REL}:5: except Exception {DISCARDS}"],
            id="passed-to-an-unresolved-call",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except:
                    pass
            """,
            [f"{REL}:5: except BaseException {DISCARDS}"],
            id="bare-except",
        ),
        pytest.param(
            """
            def f():
                try:
                    x = g()
                except DependencyError:
                    x = None
                return x
            """,
            [f"{REL}:5: except DependencyError {DISCARDS}"],
            id="alarm-class-to-none",
        ),
        pytest.param(
            """
            import contextlib
            def f():
                with contextlib.suppress(Exception):
                    g()
            """,
            [f"{REL}:4: suppress(Exception) {DISCARDS}"],
            id="contextlib-suppress",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception:
                    raise
            """.replace("raise", "raise  # noqa: " + "BLE001"),
            [f"{REL}:6: suppresses BLE001; make check-exception-handling owns it in src/"],
            id="ruff-suppression",
        ),
        pytest.param(
            "# ruff: noqa: " + "S110, PLR0913\n",
            [f"{REL}:1: suppresses S110; make check-exception-handling owns it in src/"],
            id="file-level-ruff-suppression",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                # error-propagation: the caller retries
                except Exception:
                    raise
            """,
            [
                f"{REL}:6: except Exception already propagates its error; remove the stale "
                "error-propagation justification"
            ],
            id="stale-marker",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except ValueError:  # error-propagation: a typed miss
                    return None
            """,
            [f"{REL}:5: error-propagation justification is attached to no examined handler"],
            id="orphan-marker",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception:  # error-propagation:
                    return None
            """,
            [
                f"{REL}:5: except Exception carries an error-propagation justification "
                "with no reason"
            ],
            id="marker-without-reason",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception as error:
                    def later():
                        raise error
                    return later
            """,
            [f"{REL}:5: except Exception {DISCARDS}"],
            id="raise-only-in-nested-function",
        ),
    ],
)
def test_a_discarding_or_mis_justified_handler_is_refused(
    checker: ModuleType, source: str, expected: list[str]
) -> None:
    assert _findings(checker, source) == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception:
                    cleanup()
                    raise
            """,
            id="bare-raise",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except ClientError:
                    raise DependencyError(CODE, "the call failed.") from None
            """,
            id="translate-from-none",
        ),
        pytest.param(
            """
            def f(report):
                try:
                    g()
                except Exception as error:
                    report_error(error, report)
                    return None
            """,
            id="report-error",
        ),
        pytest.param(
            """
            def _dependency_failure():
                raise DependencyError(CODE, "the call failed.")

            def f():
                try:
                    g()
                except Exception:
                    _dependency_failure()
            """,
            id="same-module-helper-that-raises",
        ),
        pytest.param(
            """
            class Store:
                def _read_error(self, error):
                    return self._wrapped(error)

                def _wrapped(self, error):
                    report_error(error, REPORT)

                def read(self):
                    try:
                        g()
                    except Exception as error:
                        self._read_error(error)
            """,
            id="method-helper-resolved-transitively",
        ),
        pytest.param(
            """
            from typing import NoReturn

            def _refuse() -> NoReturn:
                sys.exit(2)

            def f():
                try:
                    g()
                except Exception:
                    _refuse()
            """,
            id="no-return-helper",
        ),
        pytest.param(
            """
            def f(context, items):
                for item in items:
                    try:
                        g(item)
                    except Exception as error:
                        context.on_failure(error)
                        continue
            """,
            id="failure-callback",
        ),
        pytest.param(
            """
            def isolated(action, sink):
                try:
                    action()
                except Exception as error:
                    sink(error)
                    return False
                return True
            """,
            id="isolation-failure-sink",
        ),
        pytest.param(
            """
            def f():
                try:
                    return g()
                except Exception as error:
                    return LookupFailed(error)
            """,
            id="carried-in-a-constructor",
        ),
        pytest.param(
            """
            def f(items):
                failures = []
                for item in items:
                    try:
                        g(item)
                    except Exception as error:
                        failures.append(error)
                if failures:
                    raise ExceptionGroup("cleanup failed", failures)
            """,
            id="collected-then-raised",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                # error-propagation: a guard on the telemetry seam itself
                except Exception:
                    return None
            """,
            id="justified-on-the-line-above",
        ),
        pytest.param(
            """
            def f():
                try:
                    g()
                except Exception:  # error-propagation: the poll's timeout raises below
                    return None
            """,
            id="justified-on-the-except-line",
        ),
        pytest.param(
            """
            def f():
                try:
                    return int(g())
                except ValueError:
                    return None
            """,
            id="typed-handler-out-of-scope",
        ),
        pytest.param(
            """
            def f():
                text = "# noqa: BLE001 and # error-propagation: in a string are not comments"
                return text
            """,
            id="marker-text-inside-a-string",
        ),
    ],
)
def test_a_propagating_justified_or_out_of_scope_handler_is_accepted(
    checker: ModuleType, source: str
) -> None:
    assert _findings(checker, source) == []


def test_a_justified_handler_is_listed_with_its_reason(checker: ModuleType) -> None:
    result = checker.findings(
        REL,
        dedent(
            """
            def f():
                try:
                    g()
                except Exception:  # error-propagation: the poll's timeout raises below
                    return None
            """
        ),
    )
    assert result.findings == []
    assert result.justified == [f"{REL}:5: except Exception — the poll's timeout raises below"]


def _reconcile_handler_lines() -> list[int]:
    tree = ast.parse(RECONCILER.read_text(encoding="utf-8"))
    reconcile = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "reconcile"
    )
    return [node.lineno for node in ast.walk(reconcile) if isinstance(node, ast.ExceptHandler)]


def test_the_metric_reconciler_reports_each_item_failure_through_its_callback(
    checker: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S5A-3: a swallowed MeasurementCorruptError left Lambda Errors at 0 with no alarm.

    The two per-item handlers in `reconcile` comply only through the `on_failure` callback. If that
    entry left the closed callback set, both would be refused, so the seam is what this proves.
    """
    lines = _reconcile_handler_lines()
    assert len(lines) == 2
    rel = str(RECONCILER.relative_to(ROOT))
    text = RECONCILER.read_text(encoding="utf-8")
    assert checker.findings(rel, text).findings == []

    monkeypatch.setattr(checker, "CALLBACKS", checker.CALLBACKS - {"on_failure"})
    assert checker.findings(rel, text).findings == [
        f"{rel}:{line}: except Exception {DISCARDS}" for line in lines
    ]
