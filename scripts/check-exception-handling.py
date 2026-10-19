#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Refuse an except block in src/ that discards the error it caught.

Owns the error-propagation invariant: every caught exception is re-raised,
translated to a typed taxonomy error, reported through report_error, or carried
onward as a value, and never discarded. backend-observability-lambda states the
rule; this gate enforces it, because ruff's BLE001 cannot see the repository's
two sanctioned forms (`raise <TaxonomyError> from None`, which keeps provider
text out of chained tracebacks, and the report_error seam).

Scope. A handler is examined when it catches a broad or alarm-class type
(BROAD below, or a bare except), and so is contextlib.suppress over one. Typed
domain, validation and parse handlers are expected control flow and are not
examined.

A handler complies when its body, not counting nested function bodies, has:
  1. any raise statement;
  2. a call to report_error;
  3. a call to a same-module function or method that itself raises or calls
     report_error (resolved transitively to depth 3), or is annotated
     NoReturn or Never;
  4. a call to a declared failure callback (CALLBACKS below);
  5. the bound exception carried onward as a value: returned, yielded or
     assigned (alone or in a container), passed to a constructor
     (LookupFailed(error)), or collected (CARRY_METHODS below, such as
     failures.append(error)). Passing it to any other call does not count:
     that call is a propagating helper (rule 3), a callback (rule 4), or a
     read such as type(error).__name__ or a log line, which discards it;
  6. a justification `# error-propagation: <reason>` on the except line or the
     line directly above it.

The gate also refuses a ruff suppression of BLE001, S110 or S112 in src/ (that
delegation is this gate's), a justification on a handler that already complies
through rules 1-5, and a justification attached to no examined handler, so
stale justifications cannot accumulate. Every run lists the justified sites.

Accepted residuals (FEAT-1 review, 2026-10-09). The rules check presence, not
paths: one propagating branch satisfies rules 1-3 even if another branch
returns, rule 5 accepts `_ = error`, and a helper satisfies rule 3 through any
raise. Rule 4 trusts the callback's name, not what it is bound to. BROAD
matches the caught name as written, so an aliased import or a tuple constant
is not examined, and neither is a typed builtin such as ValueError, which can
still hide a decode failure behind a typed-absence branch. Review owns those
cases; none discarded a failure in src/ when this was written.

Usage:  python3 scripts/check-exception-handling.py
Exits non-zero on any finding.
"""

import ast
import io
import pathlib
import re
import sys
import tokenize
from collections.abc import Iterator
from dataclasses import dataclass, field

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

# The types whose loss hides an alarmable failure: the broad builtins, the taxonomy's
# alarming classes (ADR-009, ADR-010) and the AWS SDK's base exceptions.
BROAD = frozenset(
    {
        "Exception",
        "BaseException",
        "AdlyticsError",
        "DependencyError",
        "InvariantError",
        "ClientError",
        "BotoCoreError",
    }
)

# Declared failure callbacks. Closed: a new entry names its binding site.
CALLBACKS = frozenset(
    {
        # ReconcileContext.on_failure, bound to metric_reconciler._report → report_error
        # (src/adapters/jobs/operational_status/metric_reconciler.py, _live_context).
        "on_failure",
        # isolation.isolated's FailureSink, bound by each graph worker handler to
        # partial(workers._report, operation) → report_error
        # (src/adapters/jobs/execution_graph/workers.py).
        "sink",
    }
)

# Calls that carry the exception onward as a value: collecting it for a later raise or
# handing it to a waiter. Any other call either is a propagating helper (rule 3) or a
# declared callback (rule 4), or it only reads the exception, which does not carry it.
CARRY_METHODS = frozenset(
    {"append", "appendleft", "add", "extend", "insert", "put", "put_nowait", "set_exception"}
)
NO_RETURN = frozenset({"NoReturn", "Never"})
_NESTED_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
HELPER_DEPTH = 3

MARKER = re.compile(r"#\s*error-propagation:(?P<reason>.*)$")
SUPPRESSION = re.compile(r"#\s*(?:ruff:\s*)?noqa:(?P<codes>[^#]*)", re.IGNORECASE)
DELEGATED_CODES = ("BLE001", "S110", "S112")


@dataclass
class Result:
    """Findings and justified sites for one or more files."""

    findings: list[str] = field(default_factory=list)
    justified: list[str] = field(default_factory=list)


def _comments(text: str) -> dict[int, str]:
    """Return each line's comment text, read from tokens so strings never count."""
    comments: dict[int, str] = {}
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            comments[token.start[0]] = token.string
    return comments


def _walk_body(nodes: list[ast.stmt]) -> Iterator[ast.AST]:
    """Yield every node under these statements except nested function and class bodies."""
    stack: list[ast.AST] = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if isinstance(node, _NESTED_SCOPES):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _name(node: ast.expr) -> str | None:
    """Return the trailing identifier of a Name or Attribute expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _caught(handler: ast.ExceptHandler) -> list[str]:
    """Return the names a handler catches; a bare except catches BaseException."""
    if handler.type is None:
        return ["BaseException"]
    elements = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return [n for n in (_name(e) for e in elements) if n is not None]


class _Module:
    """One parsed module: its same-module helpers and the compliance rules over them."""

    def __init__(self, tree: ast.Module) -> None:
        self.helpers: dict[str, list[ast.FunctionDef | ast.AsyncFunctionDef]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                self.helpers.setdefault(node.name, []).append(node)

    def _helper_propagates(self, name: str, depth: int) -> bool:
        defs = self.helpers.get(name)
        if not defs or depth > HELPER_DEPTH:
            return False
        return all(self._def_propagates(d, depth) for d in defs)

    def _def_propagates(self, fn: ast.FunctionDef | ast.AsyncFunctionDef, depth: int) -> bool:
        if fn.returns is not None and _name(fn.returns) in NO_RETURN:
            return True
        return any(self._propagates(node, depth) for node in _walk_body(fn.body))

    def _propagates(self, node: ast.AST, depth: int) -> bool:
        """Rules 1-3 for one node: a raise, report_error, or a propagating helper."""
        if isinstance(node, ast.Raise):
            return True
        if not isinstance(node, ast.Call):
            return False
        called = _name(node.func)
        if called == "report_error":
            return True
        same_module = isinstance(node.func, ast.Name) or (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"self", "cls"}
        )
        return same_module and called is not None and self._helper_propagates(called, depth + 1)

    def complies(self, handler: ast.ExceptHandler) -> bool:
        """Return True when the handler satisfies rules 1-5."""
        bound = handler.name
        for node in _walk_body(handler.body):
            if self._propagates(node, 0):
                return True
            if isinstance(node, ast.Call) and _name(node.func) in CALLBACKS:
                return True
            if bound is not None and _carries(node, bound):
                return True
        return False


def _is_bound(node: ast.expr | None, bound: str) -> bool:
    """Return True when the expression is the bound name or a container holding it."""
    if isinstance(node, ast.Name):
        return node.id == bound
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return any(_is_bound(e, bound) for e in node.elts)
    if isinstance(node, ast.Dict):
        return any(_is_bound(v, bound) for v in node.values)
    return False


def _carries(node: ast.AST, bound: str) -> bool:
    """Rule 5: the bound exception is passed onward rather than only read."""
    if isinstance(node, ast.Call):
        called = _name(node.func) or ""
        constructor = called[:1].isupper()
        collected = isinstance(node.func, ast.Attribute) and called in CARRY_METHODS
        if not (constructor or collected):
            return False
        values = [*node.args, *(k.value for k in node.keywords)]
        return any(_is_bound(v, bound) for v in values)
    if isinstance(node, ast.Return | ast.Yield):
        return _is_bound(node.value, bound)
    if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
        return _is_bound(node.value, bound)
    return False


def _suppress_lines(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Yield (line, caught) for each contextlib.suppress over a broad type."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.With | ast.AsyncWith):
            continue
        for item in node.items:
            call = item.context_expr
            if isinstance(call, ast.Call) and _name(call.func) == "suppress":
                broad = [n for n in (_name(a) for a in call.args) if n in BROAD]
                if broad:
                    yield call.lineno, ", ".join(broad)


def _marker_at(comments: dict[int, str], line: int) -> tuple[int, str] | None:
    """Return the justification attached to a statement line: same line, then line above."""
    for at in (line, line - 1):
        match = MARKER.search(comments.get(at, ""))
        if match is not None:
            return at, match.group("reason").strip()
    return None


def _suppression_findings(rel: str, comments: dict[int, str]) -> list[str]:
    """Return a finding for each ruff suppression of a code this gate owns in src/."""
    found: list[str] = []
    for line, comment in sorted(comments.items()):
        match = SUPPRESSION.search(comment)
        if match is None:
            continue
        found += [
            f"{rel}:{line}: suppresses {code}; make check-exception-handling owns it in src/"
            for code in DELEGATED_CODES
            if re.search(rf"\b{code}\b", match.group("codes"), re.IGNORECASE)
        ]
    return found


def _sites(tree: ast.Module) -> list[tuple[int, str, bool]]:
    """Return (line, what, complies) for every examined handler and suppress."""
    module = _Module(tree)
    sites: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            broad = [n for n in _caught(node) if n in BROAD]
            if broad:
                sites.append((node.lineno, f"except {', '.join(broad)}", module.complies(node)))
    sites += [(line, f"suppress({caught})", False) for line, caught in _suppress_lines(tree)]
    return sorted(sites)


def _judge(where: str, complies: bool, marker: tuple[int, str] | None) -> tuple[str, str]:
    """Return ("finding" or "justified" or "", text) for one examined site."""
    if complies:
        if marker is None:
            return "", ""
        return "finding", (
            f"{where} already propagates its error; remove the stale "
            "error-propagation justification"
        )
    if marker is None:
        return "finding", (
            f"{where} discards its error; re-raise, translate, report_error, "
            "or justify with '# error-propagation: <reason>'"
        )
    if not marker[1]:
        return "finding", f"{where} carries an error-propagation justification with no reason"
    return "justified", f"{where} — {marker[1]}"


def findings(rel: str, text: str) -> Result:
    """Return every finding and justified site in one src/ file's text."""
    tree = ast.parse(text)
    comments = _comments(text)
    result = Result(findings=_suppression_findings(rel, comments))
    used_markers: set[int] = set()
    for line, what, complies in _sites(tree):
        marker = _marker_at(comments, line)
        if marker is not None:
            used_markers.add(marker[0])
        kind, message = _judge(f"{rel}:{line}: {what}", complies, marker)
        if kind == "finding":
            result.findings.append(message)
        elif kind == "justified":
            result.justified.append(message)
    result.findings += [
        f"{rel}:{line}: error-propagation justification is attached to no examined handler"
        for line, comment in sorted(comments.items())
        if MARKER.search(comment) is not None and line not in used_markers
    ]
    return result


def main() -> int:
    """Check every module under src/ and report; return the exit status."""
    total = Result()
    files = sorted(SRC.rglob("*.py"))
    for path in files:
        one = findings(str(path.relative_to(ROOT)), path.read_text(encoding="utf-8"))
        total.findings += one.findings
        total.justified += one.justified

    lines = ["EXCEPTION HANDLING CHECK", "=" * 60, f"  files:  {len(files)} under src/", ""]
    if total.findings:
        lines.append(f"FINDINGS ({len(total.findings)})")
        lines += [f"    ✗ {finding}" for finding in total.findings]
        lines.append("")
    lines.append(f"JUSTIFIED ({len(total.justified)})")
    lines += [f"    · {site}" for site in total.justified]
    lines.append("")
    if total.findings:
        lines.append(f"{len(total.findings)} except blocks discard or mis-justify their error.")
    else:
        lines.append(
            f"Every examined except block propagates its error; {len(total.justified)} justified."
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if total.findings else 0


if __name__ == "__main__":
    sys.exit(main())
