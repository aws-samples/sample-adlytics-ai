# SPDX-License-Identifier: MIT-0
"""AST conformance checks for the static trace registry and telemetry import boundary."""

from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from adapters.jobs.trace_contract.contract import (
    SpanDeclaration,
    TraceContract,
    forbidden_attribute,
)

_OTEL_IMPORT_EXEMPTIONS: Final = frozenset(
    {
        "src/adapters/outbound/telemetry/bootstrap.py",
        "src/adapters/outbound/telemetry/tracing.py",
        "src/adapters/outbound/telemetry/generated_spans.py",
        "src/adapters/outbound/agentcore/harness.py",
    }
)
_TRACING_SEAM_PATH: Final = "src/adapters/outbound/telemetry/tracing.py"
_REPORTING_SEAM_PATH: Final = "src/adapters/outbound/telemetry/reporting.py"
_REPORTING_MODULE: Final = "adapters.outbound.telemetry.reporting"
_ADAPTERS_PREFIX: Final = "src/adapters/"
_LOG_METHODS: Final = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical"}
)


@dataclass(frozen=True, slots=True)
class _ReportingBindings:
    """Local names proven to originate from the shared reporting module."""

    symbols: dict[str, frozenset[str]]
    modules: frozenset[str]


@dataclass(frozen=True, slots=True)
class _BoundaryOwner:
    """One static span constant colocated with its implementing function."""

    constant: str
    function: ast.FunctionDef | ast.AsyncFunctionDef
    relative_path: str
    line: int


@dataclass(frozen=True, slots=True)
class _SpanCall:
    """One statically inspectable `with span(...)` call and its attached metadata."""

    constant: str
    initial_attributes: frozenset[str]
    attributes: frozenset[str]
    line: int


@dataclass(frozen=True, slots=True)
class _ScanContext:
    """Shared diagnostics state for one source scan."""

    relative_path: str
    findings: list[str]


def conformance_findings(root: Path, contract: TraceContract) -> tuple[str, ...]:
    """Return stable findings for decorated calls, static calls, logs, and OTel imports."""
    findings: list[str] = []
    parsed = _parse_sources(root, findings)
    owners_by_constant: dict[str, list[_BoundaryOwner]] = defaultdict(list)
    for relative_path, tree in sorted(parsed.items()):
        owners, _has_boundary = _trace_boundary_owners(relative_path, tree, findings)
        for owner in owners:
            owners_by_constant[owner.constant].append(owner)

    declarations = {declaration.constant: declaration for declaration in contract.spans}
    for constant, owners in sorted(owners_by_constant.items()):
        if constant not in declarations:
            for owner in owners:
                findings.append(
                    f"{owner.relative_path}:{owner.line}: trace_boundary declares "
                    f"undeclared static span constant {constant}."
                )

    for declaration in contract.spans:
        owners = owners_by_constant.get(declaration.constant, [])
        if not owners:
            findings.append(f"{declaration.constant}: missing trace_boundary implementation owner.")
            continue
        if len(owners) > 1:
            locations = ", ".join(
                f"{owner.relative_path}:{owner.line}"
                for owner in sorted(owners, key=lambda item: (item.relative_path, item.line))
            )
            findings.append(
                f"{declaration.constant}: duplicate trace_boundary implementation ownership: "
                f"{locations}."
            )
            continue
        _validate_owner(declaration, owners[0], findings)

    declared = contract.constants
    tracing_tree = parsed.get(_TRACING_SEAM_PATH)
    if tracing_tree is not None:
        findings.extend(_reexport_findings(tracing_tree, declared))
    for relative_path, tree in sorted(parsed.items()):
        findings.extend(_telemetry_alias_findings(relative_path, tree))
        findings.extend(_static_span_findings(relative_path, tree, declared))
        if relative_path.startswith(_ADAPTERS_PREFIX) and relative_path != _REPORTING_SEAM_PATH:
            findings.extend(_direct_logger_findings(relative_path, tree))
        findings.extend(_report_error_flush_findings(relative_path, tree))
        findings.extend(_configured_handler_findings(relative_path, tree))
        if relative_path not in _OTEL_IMPORT_EXEMPTIONS:
            findings.extend(_otel_import_findings(relative_path, tree))
    return tuple(sorted(set(findings)))


def _validate_owner(
    declaration: SpanDeclaration, owner: _BoundaryOwner, findings: list[str]
) -> None:
    calls = [
        call
        for call in _span_calls_in_function(owner.function, owner.relative_path, findings)
        if call.constant == declaration.constant
    ]
    if len(calls) != 1:
        findings.append(
            f"{owner.relative_path}:{owner.line}: trace_boundary({declaration.constant}) "
            f"owner must contain exactly one matching span; found {len(calls)}."
        )
        if not calls:
            return
    allowed = frozenset(declaration.allowed_attributes)
    required = frozenset(declaration.required_attributes)
    for call in calls:
        missing = sorted(required - call.initial_attributes)
        if missing:
            findings.append(
                f"{owner.relative_path}:{call.line}: {declaration.constant} must provide required "
                f"metadata when the span opens: {', '.join(missing)}."
            )
        extra = sorted(call.attributes - allowed)
        if extra:
            findings.append(
                f"{owner.relative_path}:{call.line}: {declaration.constant} records undeclared "
                f"metadata: {', '.join(extra)}."
            )
        forbidden = sorted(name for name in call.attributes if forbidden_attribute(name))
        if forbidden:
            findings.append(
                f"{owner.relative_path}:{call.line}: {declaration.constant} records forbidden "
                f"metadata: {', '.join(forbidden)}."
            )


def _parse_sources(root: Path, findings: list[str]) -> dict[str, ast.Module]:
    parsed: dict[str, ast.Module] = {}
    source_root = root / "src"
    if not source_root.is_dir():
        findings.append("src: source directory is missing.")
        return parsed
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            parsed[relative] = ast.parse(path.read_text(), filename=str(path))
        except SyntaxError as error:
            findings.append(
                f"{relative}:{error.lineno or 0}: source cannot be parsed: {error.msg}."
            )
    return parsed


def _trace_boundary_owners(
    relative_path: str, tree: ast.Module, findings: list[str]
) -> tuple[tuple[_BoundaryOwner, ...], bool]:
    owners: list[_BoundaryOwner] = []
    has_boundary = False

    class Visitor(ast.NodeVisitor):
        def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            nonlocal has_boundary
            boundary_calls: list[ast.Call] = []
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and _call_name(decorator.func) == "trace_boundary"
                ):
                    has_boundary = True
                    boundary_calls.append(decorator)
                elif _call_name(decorator) == "trace_boundary":
                    has_boundary = True
                    findings.append(
                        f"{relative_path}:{decorator.lineno}: trace_boundary must be called "
                        "with one or more declared constants."
                    )
            if len(boundary_calls) > 1:
                findings.append(
                    f"{relative_path}:{node.lineno}: use one trace_boundary(...) decorator "
                    "for all spans owned by a function."
                )
            for decorator in boundary_calls:
                if decorator.keywords:
                    findings.append(
                        f"{relative_path}:{decorator.lineno}: trace_boundary accepts positional "
                        "declared constants only."
                    )
                if not decorator.args:
                    findings.append(
                        f"{relative_path}:{decorator.lineno}: trace_boundary requires one or "
                        "more declared constants."
                    )
                    continue
                constants: list[str] = []
                for argument in decorator.args:
                    if not isinstance(argument, ast.Name):
                        findings.append(
                            f"{relative_path}:{argument.lineno}: trace_boundary arguments must "
                            "be static span constant names."
                        )
                        continue
                    constants.append(argument.id)
                duplicates = sorted(
                    constant for constant in set(constants) if constants.count(constant) > 1
                )
                if duplicates:
                    findings.append(
                        f"{relative_path}:{decorator.lineno}: trace_boundary constants must be "
                        f"unique: {', '.join(duplicates)}."
                    )
                for constant in dict.fromkeys(constants):
                    owners.append(_BoundaryOwner(constant, node, relative_path, decorator.lineno))
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._visit_function(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._visit_function(node)

    Visitor().visit(tree)
    return tuple(owners), has_boundary


def _span_calls_in_function(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    relative_path: str,
    findings: list[str],
) -> tuple[_SpanCall, ...]:
    calls: list[_SpanCall] = []

    class Visitor(ast.NodeVisitor):
        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is function:
                self.generic_visit(node)

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            del node

        def visit_With(self, node: ast.With) -> None:
            calls.extend(_with_span_calls(node, relative_path, findings))
            self.generic_visit(node)

        def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
            calls.extend(_with_span_calls(node, relative_path, findings))
            self.generic_visit(node)

    Visitor().visit(function)
    return tuple(calls)


def _with_span_calls(
    node: ast.With | ast.AsyncWith,
    relative_path: str,
    findings: list[str],
) -> tuple[_SpanCall, ...]:
    observed: list[_SpanCall] = []
    context = _ScanContext(relative_path, findings)
    for item in node.items:
        expression = item.context_expr
        if not isinstance(expression, ast.Call) or _call_name(expression.func) != "span":
            continue
        constant = _static_constant(expression)
        if constant is None:
            continue
        initial_attributes = _keyword_names(expression, context)
        attributes = set(initial_attributes)
        bound_name = item.optional_vars.id if isinstance(item.optional_vars, ast.Name) else None
        if bound_name is None:
            findings.append(
                f"{relative_path}:{node.lineno}: span({constant}, ...) must bind its Recording."
            )
        else:
            attributes.update(_recorded_attributes(node.body, bound_name, context))
        observed.append(
            _SpanCall(
                constant,
                frozenset(initial_attributes),
                frozenset(attributes),
                node.lineno,
            )
        )
    return tuple(observed)


def _recorded_attributes(body: list[ast.stmt], bound_name: str, context: _ScanContext) -> set[str]:
    attributes: set[str] = set()
    for statement in body:
        for node in ast.walk(statement):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr != "record" or not isinstance(node.func.value, ast.Name):
                continue
            if node.func.value.id != bound_name:
                continue
            attributes.update(_keyword_names(node, context))
    return attributes


def _keyword_names(call: ast.Call, context: _ScanContext) -> set[str]:
    names: set[str] = set()
    for keyword in call.keywords:
        if keyword.arg is None:
            context.findings.append(
                f"{context.relative_path}:{call.lineno}: "
                "trace metadata cannot use dynamic ** expansion."
            )
        else:
            names.add(keyword.arg)
            forbidden_sources = sorted(
                {
                    candidate
                    for node in ast.walk(keyword.value)
                    if (candidate := _expression_name(node)) is not None
                    and forbidden_attribute(candidate)
                }
            )
            if forbidden_sources:
                context.findings.append(
                    f"{context.relative_path}:{call.lineno}: trace metadata "
                    f"{keyword.arg} reads forbidden source expression(s): "
                    f"{', '.join(forbidden_sources)}."
                )
    return names


def _expression_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _telemetry_alias_findings(relative_path: str, tree: ast.Module) -> list[str]:
    """Reject aliases that hide mechanical trace-boundary and span discovery."""
    findings: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == (
            "adapters.outbound.telemetry.tracing"
        ):
            for alias in node.names:
                if alias.name in {"span", "trace_boundary", "tool_span"} and alias.asname:
                    findings.append(
                        f"{relative_path}:{node.lineno}: telemetry symbol {alias.name} "
                        "cannot be imported with an alias."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "adapters.outbound.telemetry.tracing" and alias.asname:
                    findings.append(
                        f"{relative_path}:{node.lineno}: telemetry tracing module cannot be "
                        "imported with an alias."
                    )
    return findings


def _reexport_findings(tree: ast.Module, declared: frozenset[str]) -> list[str]:
    """Every generated constant must be imported and re-exported by the tracing seam."""
    imported: set[str] = set()
    exported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("generated_spans"):
            imported.update(alias.asname or alias.name for alias in node.names)
        elif (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, ast.List | ast.Tuple)
        ):
            exported.update(
                element.value
                for element in node.value.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return [
        f"{_TRACING_SEAM_PATH}: generated span constant {constant} is not "
        f"{'imported' if constant not in imported else 're-exported in __all__'}."
        for constant in sorted(declared)
        if constant not in imported or constant not in exported
    ]


def _static_span_findings(
    relative_path: str, tree: ast.Module, declared: frozenset[str]
) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node.func) != "span":
            continue
        if not node.args:
            findings.append(f"{relative_path}:{node.lineno}: span call has no declared name.")
            continue
        first = node.args[0]
        if isinstance(first, ast.Call) and _call_name(first.func) == "tool_span":
            continue
        if isinstance(first, ast.Name) and first.id in declared:
            continue
        rendered = first.id if isinstance(first, ast.Name) else ast.unparse(first)
        findings.append(
            f"{relative_path}:{node.lineno}: undeclared static span name {rendered!r}; "
            "declare a constant or use governed tool_span(...)."
        )
    return findings


def _direct_logger_findings(relative_path: str, tree: ast.Module) -> list[str]:
    logger_names = _logger_names(tree)
    findings: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _LOG_METHODS:
            continue
        if not _is_logger_reference(node.func.value, logger_names):
            continue
        findings.append(
            f"{relative_path}:{node.lineno}: direct Logger.{node.func.attr} lifecycle call; "
            "use the shared reporting seam."
        )
    return findings


def _logger_names(tree: ast.Module) -> frozenset[str]:
    constructor_names: set[str] = set()
    module_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "aws_lambda_powertools":
            constructor_names.update(
                alias.asname or alias.name for alias in node.names if alias.name == "Logger"
            )
        elif isinstance(node, ast.Import):
            module_names.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "aws_lambda_powertools"
            )

    names: set[str] = set()
    for node in ast.walk(tree):
        value: ast.expr | None = None
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            value = node.value
            targets = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            value = node.value
            targets = [node.target]
        if not isinstance(value, ast.Call) or not _is_logger_constructor(
            value.func, constructor_names, module_names
        ):
            continue
        names.update(target.id for target in targets if isinstance(target, ast.Name))
    return frozenset(names)


def _is_logger_constructor(
    node: ast.expr, constructor_names: set[str], module_names: set[str]
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in constructor_names
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "Logger"
        and isinstance(node.value, ast.Name)
        and node.value.id in module_names
    )


def _is_logger_reference(node: ast.expr, logger_names: frozenset[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in logger_names
    if isinstance(node, ast.Attribute):
        return node.attr in logger_names
    return False


def _configured_handler_findings(relative_path: str, tree: ast.Module) -> list[str]:
    """Require a typed business root on every handler that installs tracing."""
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "adapters.outbound.telemetry.bootstrap"
        and any(alias.name == "configure_tracing" for alias in node.names)
        for node in ast.walk(tree)
    )
    configured = imported and any(
        isinstance(node, ast.Call) and _call_name(node.func) == "configure_tracing"
        for node in ast.walk(tree)
    )
    if not configured:
        return []
    findings: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) or node.name != "handler":
            continue
        if not any(
            isinstance(decorator, ast.Call) and _call_name(decorator.func) == "trace_boundary"
            for decorator in node.decorator_list
        ):
            findings.append(
                f"{relative_path}:{node.lineno}: traced handler has no typed trace_boundary root."
            )
    return findings


def _report_error_flush_findings(relative_path: str, tree: ast.Module) -> list[str]:
    bindings = _reporting_bindings(tree)
    if not any(
        isinstance(node, ast.Call) and _is_reporting_symbol(node.func, "report_error", bindings)
        for node in ast.walk(tree)
    ):
        return []

    findings: list[str] = []
    handlers = (
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "handler"
    )
    for handler in handlers:
        if _handler_flushes_metrics(handler, bindings):
            continue
        findings.append(
            f"{relative_path}:{handler.lineno}: handler calls report_error but does not flush "
            "shared metrics; add @METRICS.log_metrics, @flushes_metrics, or flush_metrics() "
            "in a finally block."
        )
    return findings


def _reporting_bindings(tree: ast.Module) -> _ReportingBindings:
    symbols = {"METRICS", "flush_metrics", "flushes_metrics", "report_error"}
    symbol_bindings = {symbol: set[str]() for symbol in symbols}
    reporting_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _REPORTING_MODULE:
            for alias in node.names:
                if alias.name in symbols:
                    symbol_bindings[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "adapters.outbound.telemetry":
            reporting_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "reporting"
            )
        elif isinstance(node, ast.Import):
            reporting_modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == _REPORTING_MODULE
            )
    return _ReportingBindings(
        symbols={name: frozenset(names) for name, names in symbol_bindings.items()},
        modules=frozenset(reporting_modules),
    )


def _is_reporting_symbol(
    node: ast.expr,
    symbol: str,
    bindings: _ReportingBindings,
) -> bool:
    if isinstance(node, ast.Name):
        return node.id in bindings.symbols[symbol]
    return (
        isinstance(node, ast.Attribute)
        and node.attr == symbol
        and isinstance(node.value, ast.Name)
        and node.value.id in bindings.modules
    )


def _handler_flushes_metrics(
    handler: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: _ReportingBindings,
) -> bool:
    for decorator in handler.decorator_list:
        candidate = decorator.func if isinstance(decorator, ast.Call) else decorator
        if _is_reporting_symbol(candidate, "flushes_metrics", bindings):
            return True
        if (
            isinstance(candidate, ast.Attribute)
            and candidate.attr == "log_metrics"
            and _is_reporting_symbol(candidate.value, "METRICS", bindings)
        ):
            return True

    for node in _walk_function_body(handler):
        if not isinstance(node, ast.Try):
            continue
        if any(
            isinstance(final_node, ast.Call)
            and _is_reporting_symbol(final_node.func, "flush_metrics", bindings)
            for statement in node.finalbody
            for final_node in ast.walk(statement)
        ):
            return True
    return False


def _walk_function_body(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(function.body)
    nested_scopes = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(
            child for child in ast.iter_child_nodes(node) if not isinstance(child, nested_scopes)
        )
    return nodes


def _otel_import_findings(relative_path: str, tree: ast.Module) -> list[str]:
    findings: list[str] = []
    for node in ast.walk(tree):
        imported = ""
        line = 0
        if isinstance(node, ast.ImportFrom):
            imported = node.module or ""
            line = node.lineno
        elif isinstance(node, ast.Import):
            imported = next(
                (alias.name for alias in node.names if alias.name.startswith("opentelemetry")), ""
            )
            line = node.lineno
        if imported == "opentelemetry" or imported.startswith("opentelemetry."):
            findings.append(
                f"{relative_path}:{line}: direct OpenTelemetry import outside an approved seam."
            )
    return findings


def _static_constant(call: ast.Call) -> str | None:
    if not call.args or not isinstance(call.args[0], ast.Name):
        return None
    return call.args[0].id


def _call_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""
