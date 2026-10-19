# SPDX-License-Identifier: MIT-0
"""The one runner for the ingestion-traceability slice probes (P0..P7) and the import scan.

Every mode reaches AWS only through an explicit caller-supplied profile, environment, region
and expected account, and writes its evidence as JSON under an explicit `--evidence-dir`. The
agent reads that file before any claim; a zero exit status is not evidence on its own.

Modes:

- `--asset-scan`: for every `Adlytics-*-<env>` stack outside the named stack set, collect each
  deployed function's handler, compute its transitive import closure over the staged
  `build/src` (AST, absolute and relative imports), and intersect it with the files the range
  `<base>..<sha>` changes under `src`, `contracts` and `presentation`. A changed contract counts
  as touched when a closure module names its path or walks its directory. A handler that is not
  a Python module is resolved (`run.sh`) or counted as touched. Framework functions (no source
  asset) are listed and excluded. Each stack outside the set must show an empty intersection.
- `--record-assets <STACK>`: record every function's `Code.S3Key` from the synthesized
  `infra/cdk.out/<STACK>.template.json`, marking the shared `build/src` source asset.
- `--probe P0`: the P0 slice probe (impl-plan.md, "Probe catalog").
- `--probe P1`, `--probe P2`: the S1 and S2 slice probes; each re-runs the earlier probes'
  invariant assertions. P2 drives the deployed explanation through the `flow explain` client
  under a synthetic `operations-status-read` persona, and (S1 finding M-1) the deployed
  run-request route under a requester persona and a non-member. For BF-5 it also reads the
  explanation function's reserved concurrency and commits one run request while explanation
  pagers run at twice that bound.
- `--probe P3`, `--probe P4`: the S3 and S4 slice probes (`probe_ingestion_traceability_p3`,
  `probe_ingestion_traceability_p4`). P4 runs the release live tests against the probe table
  and the pointer attribution invariant, and re-runs the P0-P3 invariants.
- `--probe P5`: the S5 slice probe (`probe_ingestion_traceability_p5`): the staffed destination
  readback (`--staffed-endpoint-sha256`, the endpoint's digest only), the metric fault controls
  to a confirmation receipt and the stuck-flow `ALARM -> OK`, both in probe scope under the probe
  role, the live detector and measurement paths, and P4's re-run.
- `--probe P6 --p6-step a|b0-start|b0-finish|alarm-window`: S6 part 1 (`probe_ingestion_traceability_p6`):
  ingress by mode in SHADOW, then the abort rehearsal in two steps around the drain wait, and a
  read-only re-read of a drain window's alarm rule (`alarm-window`). Each
  step takes the P5 set-stack, assets, diff and lagging arguments for its step-7 readback.
- `--probe P6 --p6-step pre-cas|e`: S6 part 2 (plan B8), read-only, written and unit-tested but
  not yet run: the zero-unstamped CURRENT count before the deploy and before the CAS, and P6(e)
  for one run (`--run-id`, `--window-start`, `--window-end`, `--windows-per-cycle`): its spans by
  `run_id` through 5a, one measurement per terminal node and claim, the explanation's
  `materialized`, every DLQ count, `flipped_at` against `publish`'s `settled_at`, the
  informational serial estimate, the observed named-cohort <=7,200-second verdict, and the
  delivery queue's in-run NotVisible maximum. Repeat `--run-id` for every cohort member. The
  reader, legacy-pointer and footnote readbacks are verdict functions over operator captures.
- `--provision-persona <LABEL> --persona-group <GROUP>`: create or reuse one synthetic dev
  persona in exactly one group (dev only).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final, NoReturn, Protocol, cast
from xml.etree import ElementTree

_ROOT: Final = Path(__file__).resolve().parents[1]
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
_BASE: Final = "1aca2df020eec4b44dc9b4533d0405eab9acf17b"
"""The implementation base of this plan; every changed-file set is measured from it."""
FIXED_PROBE_DELIVERY: Final = (
    "generated/amc-dsp-impressions.csv#daily-run=2026-10-06#chunk=2026-10-04"
)
"""The one fixed phase-A delivery P0 copies; `test_probe_delivery_unrestated` pins it."""
FIXED_PROBE_CONTRACT: Final = "amc-dsp-impressions"
_MARKETPLACE_KEY: Final = "amazon_us"
_ACCOUNT_DIGITS: Final = 12
_PROBE_LINE: Final = "PROBE_RETRIEVAL "
_TERMINAL_QUERY_STATES: Final = frozenset({"Complete", "Failed", "Cancelled", "Timeout"})
_WALK_CALLS: Final = frozenset({"rglob", "glob", "iterdir", "walk", "listdir", "scandir"})
_ERROR_OPERATIONS: Final = (
    "execution_graph.stuck_flow_detector",
    "finalize_registration",
    "ingest.handler",
    "ingest_delivery",
    "projection_handoff.callback",
    "semantic_table_writer",
    "stage1_plan_intent.reconcile",
    "stage1_provider_attempt.reconcile",
    "stage1_provider_fence.recovery",
)
"""The fixed ingestion operations, one typed-error alarm each (infra ingestion-stage1.ts)."""
_REQUIRED_ALARMS: Final = (
    "ingestion-oldest",
    "semantic-write-oldest",
    *(
        "ingestion-alarming-errors-" + operation.replace(".", "-").replace("_", "-")
        for operation in _ERROR_OPERATIONS
    ),
)
_ERRORS_SCHEMA: Final = 'SCHEMA("Adlytics", ErrorClass, ErrorCode, Operation)'
_TERMINAL_UNIT_OUTCOMES: Final = frozenset(
    {
        # The acknowledged half of TableWriter's closed `_UnitResult` vocabulary.
        "attested",
        "attested_replay",
        "attempt_budget_successor_created",
        "plan_replaced",
        "intent_replaced",
        "stale_generation",
        "stale_plan_authority",
        "plan_completed",
        "plan_superseded",
        "plan_admission_failed",
    }
)


class ProbeError(RuntimeError):
    """A bounded probe failure, safe to print and record."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ProbeError(f"invalid arguments: {message}")


# --- Pure parts (unit-tested in tests/unit/test_probe_ingestion_traceability_gate.py) --------


def parse_probe_report(stdout: str) -> dict[str, object]:
    """Return the one `PROBE_RETRIEVAL` line, refusing anything but one non-historical delivery."""
    lines = [line for line in stdout.splitlines() if line.startswith(_PROBE_LINE)]
    if len(lines) != 1:
        raise ProbeError(f"the dry run printed {len(lines)} probe lines; exactly one is required")
    parsed: object = json.loads(lines[0].removeprefix(_PROBE_LINE))
    if not isinstance(parsed, dict):
        raise ProbeError("the probe line is not an object")
    report = cast("dict[str, object]", parsed)
    if report.get("deliveries") != 1:
        raise ProbeError("the dry run does not report exactly one delivery")
    if report.get("historical") is not False or report.get("original_historical") is not False:
        raise ProbeError("the probe delivery is not classified non-historical")
    if report.get("body_sha256") != report.get("original_body_sha256"):
        raise ProbeError("the probe copy's data bytes differ from the original")
    return report


def _module_file(staged: Path, module: str) -> Path | None:
    base = staged.joinpath(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _package_inits(staged: Path, module: str) -> list[Path]:
    parts = module.split(".")
    found: list[Path] = []
    for index in range(1, len(parts)):
        init = staged.joinpath(*parts[:index], "__init__.py")
        if init.is_file():
            found.append(init)
    return found


def _module_name(staged: Path, path: Path) -> str:
    relative = path.relative_to(staged).with_suffix("")
    parts = list(relative.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(staged: Path, path: Path) -> set[str]:
    """Return every module the file imports, resolved against the staged tree."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _module_name(staged, path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            if base:
                names.add(base)
            names.update(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return names


def import_closure(staged: Path, module: str) -> frozenset[Path]:
    """Return every staged source file the module transitively imports, itself included."""
    start = _module_file(staged, module)
    if start is None:
        raise ProbeError(f"handler module {module!r} is not in the staged tree")
    seen: set[Path] = set()
    pending = [start, *_package_inits(staged, module)]
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imports(staged, path):
            found = _module_file(staged, name)
            if found is not None:
                pending.append(found)
                pending.extend(_package_inits(staged, name))
    return frozenset(seen)


@dataclass(frozen=True, slots=True)
class HandlerTarget:
    """One deployed function's handler, classified for the scan."""

    logical_id: str
    handler: str
    runtime: str
    module: str | None
    """The Python module, or None for a framework function with no source asset."""
    touched_unresolved: bool = False
    """A non-Python handler that could not be resolved: counted as touched."""


_RUN_SH_MODULE: Final = re.compile(r"-m\s+([A-Za-z_][\w.]*)")


def classify_handler(
    logical_id: str, properties: Mapping[str, object], staged: Path
) -> HandlerTarget:
    """Resolve a function's handler to its Python module, or mark it framework or touched."""
    handler = str(properties.get("Handler", ""))
    runtime = str(properties.get("Runtime", ""))
    if runtime.startswith("python"):
        if handler == "run.sh":
            script = staged / "run.sh"
            match = _RUN_SH_MODULE.search(script.read_text()) if script.is_file() else None
            if match is None:
                return HandlerTarget(logical_id, handler, runtime, None, touched_unresolved=True)
            return HandlerTarget(logical_id, handler, runtime, match.group(1))
        module = handler.rpartition(".")[0]
        if module and _module_file(staged, module) is not None:
            return HandlerTarget(logical_id, handler, runtime, module)
        return HandlerTarget(logical_id, handler, runtime, None, touched_unresolved=True)
    return HandlerTarget(logical_id, handler, runtime, None)


def staged_path(repository_path: str) -> str:
    """Map a repository path to its location in the staged `build/src` tree."""
    if repository_path.startswith("src/"):
        return repository_path.removeprefix("src/")
    return repository_path


def _contract_touched(sources: Mapping[Path, str], contract: str) -> bool:
    """Whether a closure module names a changed contract's path or walks its directory."""
    relative = contract.removeprefix("contracts/")
    name = Path(relative).name
    parents = [part for part in Path(relative).parent.parts if part]
    for text in sources.values():
        if relative in text or f'"{name}"' in text or f"'{name}'" in text:
            return True
        walks = any(f".{call}(" in text for call in _WALK_CALLS)
        if walks and '"contracts"' in text and any(f'"{part}"' in text for part in parents):
            return True
    return False


def scan_intersection(
    staged: Path, modules: Iterable[str], changed: Iterable[str]
) -> tuple[str, ...]:
    """Return the changed repository paths that the modules' closures touch."""
    closure: set[Path] = set()
    for module in modules:
        closure |= import_closure(staged, module)
    names = {path.relative_to(staged).as_posix() for path in closure}
    sources = {path: path.read_text() for path in closure}
    touched: list[str] = []
    for item in sorted(set(changed)):
        in_closure = item.startswith("src/") and staged_path(item) in names
        declared = item.startswith(("contracts/", "presentation/"))
        if in_closure or (declared and _contract_touched(sources, item)):
            touched.append(item)
    return tuple(touched)


def junit_verdict(xml_text: str, expected_tests: int) -> dict[str, int]:
    """Fail closed on any skip, error, failure, or a test count other than the expected one."""
    root = ElementTree.fromstring(xml_text)  # noqa: S314 -- the runner's own pytest output
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    totals = {"tests": 0, "skipped": 0, "errors": 0, "failures": 0}
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.get(key, "0"))
    if totals["skipped"] or totals["errors"] or totals["failures"]:
        raise ProbeError(f"integration file did not pass cleanly: {totals}")
    if totals["tests"] != expected_tests:
        raise ProbeError(f"expected {expected_tests} tests, ran {totals['tests']}")
    return totals


def function_assets(template: Mapping[str, object]) -> dict[str, dict[str, str]]:
    """Return every `AWS::Lambda::Function`'s handler, runtime and `Code.S3Key`."""
    resources = cast("Mapping[str, Mapping[str, object]]", template.get("Resources", {}))
    found: dict[str, dict[str, str]] = {}
    for logical_id, resource in resources.items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        properties = cast("Mapping[str, object]", resource.get("Properties", {}))
        code = cast("Mapping[str, object]", properties.get("Code", {}))
        found[logical_id] = {
            "handler": str(properties.get("Handler", "")),
            "runtime": str(properties.get("Runtime", "")),
            "s3_key": str(code.get("S3Key", "")),
        }
    return found


def compare_assets(
    recorded: Mapping[str, Mapping[str, str]],
    deployed: Mapping[str, Mapping[str, str]],
    source_key: str | None,
) -> dict[str, object]:
    """Compare a stack's recorded synthesized keys with its deployed processed template."""
    mismatches = sorted(
        logical_id
        for logical_id, item in recorded.items()
        if deployed.get(logical_id, {}).get("s3_key") != item.get("s3_key")
    )
    missing = sorted(set(recorded) - set(deployed))
    source_functions = sorted(
        logical_id for logical_id, item in recorded.items() if item.get("s3_key") == source_key
    )
    return {
        "equal": not mismatches and not missing,
        "mismatches": mismatches,
        "missing": missing,
        "source_asset_key": source_key,
        "source_asset_functions": len(source_functions),
    }


_WRITE_ACTIONS: Final = frozenset(
    {
        "dynamodb:*",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:PartiQLInsert",
        "dynamodb:PartiQLUpdate",
        "dynamodb:PartiQLDelete",
    }
)


def table_writers(
    template: Mapping[str, object], needles: Sequence[str]
) -> list[dict[str, object]]:
    """List every IAM statement in a template that grants a write action on a matching table.

    A table matches when the statement's rendered resource names one of the needles, in its
    literal or its SSM-reference (hyphen-free) spelling, or is the wildcard. A role's default
    policy that passes IAM's inline size bound overflows into managed policies CDK attaches to
    it (TableWriter's does from S6 part 2), so those are read too.
    """
    resources = cast("Mapping[str, Mapping[str, object]]", template.get("Resources", {}))
    found: list[dict[str, object]] = []
    for logical_id, resource in resources.items():
        if resource.get("Type") not in {
            "AWS::IAM::Policy",
            "AWS::IAM::ManagedPolicy",
            "AWS::IAM::Role",
        }:
            continue
        properties = cast("Mapping[str, object]", resource.get("Properties", {}))
        documents = [properties.get("PolicyDocument")] + [
            cast("Mapping[str, object]", item).get("PolicyDocument")
            for item in cast("list[object]", properties.get("Policies", []))
        ]
        for document in documents:
            if not isinstance(document, dict):
                continue
            for statement in cast("list[Mapping[str, object]]", document.get("Statement", [])):
                if statement.get("Effect") != "Allow":
                    continue
                raw = statement.get("Action", [])
                actions = [raw] if isinstance(raw, str) else cast("list[str]", raw)
                rendered = json.dumps(statement.get("Resource", ""))
                named = rendered == '"*"' or any(
                    needle in rendered or needle.replace("-", "") in rendered for needle in needles
                )
                writes = sorted(set(actions) & _WRITE_ACTIONS)
                if named and writes:
                    found.append(
                        {
                            "policy": logical_id,
                            "roles": properties.get("Roles", []),
                            "actions": writes,
                        }
                    )
    return found


def rows_digest(rows: Sequence[Sequence[str | None]]) -> str:
    """Digest a row set independent of its order."""
    canonical = sorted(json.dumps(list(row)) for row in rows)
    return hashlib.sha256("\n".join(canonical).encode()).hexdigest()


# --- AWS orchestration ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """Every explicit target input; none has a default."""

    profile: str
    environment: str
    region: str
    expected_account: str
    evidence_dir: Path


@dataclass(slots=True)
class Evidence:
    """One probe's evidence document, written whatever the outcome."""

    probe: str
    started_at: str
    document: dict[str, object] = field(default_factory=dict[str, object])

    def write(self, target: Target) -> Path:
        """Write the evidence JSON and return its path."""
        target.evidence_dir.mkdir(parents=True, exist_ok=True)
        path = target.evidence_dir / f"{self.probe}-{self.started_at}.json"
        body = {"probe": self.probe, "started_at": self.started_at, **self.document}
        path.write_text(json.dumps(body, indent=2, sort_keys=True, default=str) + "\n")
        return path


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _session(target: Target) -> Any:
    import boto3

    session = boto3.Session(profile_name=target.profile, region_name=target.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != target.expected_account:
        raise ProbeError("the explicit profile's account differs from --expected-account")
    return session


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed git argv
        ["git", "-C", str(worktree), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def changed_files(worktree: Path, sha: str) -> tuple[str, ...]:
    """List the repository paths `<base>..<sha>` changes under the staged roots."""
    output = _git(
        worktree, "diff", "--name-only", f"{_BASE}..{sha}", "--", "src", "contracts", "presentation"
    )
    return tuple(line for line in output.splitlines() if line)


def _stacks(cloudformation: Any, environment: str) -> list[str]:
    names: list[str] = []
    for page in cloudformation.get_paginator("describe_stacks").paginate():
        for stack in page.get("Stacks", []):
            name = str(stack["StackName"])
            if name.startswith("Adlytics-") and name.endswith(f"-{environment}"):
                names.append(name)
    return sorted(names)


def _processed_template(cloudformation: Any, stack: str) -> dict[str, object]:
    body: object = cloudformation.get_template(StackName=stack, TemplateStage="Processed")[
        "TemplateBody"
    ]
    if isinstance(body, str):
        body = json.loads(body)
    return cast("dict[str, object]", body)


def asset_scan(
    session: Any,
    request: AssetScanRequest,
) -> dict[str, object]:
    """Run the import scan for every stack outside the set and return its result."""
    cloudformation = session.client("cloudformation")
    changed = changed_files(request.worktree, request.sha)
    stacks: dict[str, object] = {}
    failures: list[str] = []
    for stack in _stacks(cloudformation, request.environment):
        if stack in request.set_stacks:
            continue
        functions = function_assets(_processed_template(cloudformation, stack))
        if not functions:
            stacks[stack] = {"functions": 0, "intersection": []}
            continue
        targets = [
            classify_handler(
                logical_id,
                {"Handler": item["handler"], "Runtime": item["runtime"]},
                request.staged,
            )
            for logical_id, item in functions.items()
        ]
        unresolved = [item.logical_id for item in targets if item.touched_unresolved]
        modules = sorted({item.module for item in targets if item.module is not None})
        intersection = list(scan_intersection(request.staged, modules, changed))
        if unresolved:
            intersection.append(f"<unresolved handler: {', '.join(sorted(unresolved))}>")
        framework = sorted(
            item.logical_id
            for item in targets
            if item.module is None and not item.touched_unresolved
        )
        stacks[stack] = {
            "functions": len(functions),
            "modules": modules,
            "handlers": sorted(
                {
                    f"{item.module}:"
                    + ("*" if item.handler == "run.sh" else item.handler.rpartition(".")[2])
                    for item in targets
                    if item.module is not None
                }
            ),
            "framework_functions": framework,
            "intersection": intersection,
        }
        if intersection:
            failures.append(stack)
    pins = pinned_lagging_files(request.worktree, request.sha, request.lagging_files)
    extended = extended_closed_sets(request.worktree, request.sha, sorted(pins["current"]))
    decoders = decoder_files(request.staged, extended)
    accepted: list[str] = []
    for stack in failures:
        entry = cast("dict[str, Any]", stacks[stack])
        if stack not in request.lagging_stacks:
            continue
        if not set(entry["intersection"]) <= pins["current"]:
            continue
        # Cross-version data check (B1): a lagging stack whose handler reaches a function that
        # decodes a closed set the change extends would refuse the values the updated set emits.
        reach = decoding_entry_points(
            request.staged, entry["handlers"], decoders=set(decoders), enums=set(extended)
        )
        entry["data_decoders"] = reach
        if not reach:
            accepted.append(stack)
    unaccepted = [stack for stack in failures if stack not in accepted]
    return {
        "sha": request.sha,
        "base": _BASE,
        "changed": list(changed),
        "set_stacks": sorted(request.set_stacks),
        "stacks": stacks,
        "non_empty": failures,
        "accepted_lagging": accepted,
        "lagging_files": dict(sorted(request.lagging_files.items())),
        "lagging_files_current": sorted(pins["current"]),
        "lagging_files_moved": sorted(pins["moved"]),
        "extended_closed_sets": {name: sorted(values) for name, values in extended.items()},
        "decoders": decoders,
        "unaccepted": unaccepted,
        "passed": not unaccepted,
    }


def pinned_lagging_files(worktree: Path, sha: str, pins: Mapping[str, str]) -> dict[str, set[str]]:
    """Split the accepted lagging files into those still at their pinned blob and those moved.

    A lagging acceptance covers one exact content of each file (B7): a later edit to the file
    changes its blob, so the file no longer counts as accepted and the stack fails the scan.
    """
    current: set[str] = set()
    moved: set[str] = set()
    for path, blob in pins.items():
        found = _git(worktree, "rev-parse", f"{sha}:{path}").strip()
        (current if found == blob else moved).add(path)
    return {"current": current, "moved": moved}


def _enum_members(source: str) -> dict[str, set[str]]:
    """Return each Enum/StrEnum class's member values in one Python source."""
    found: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {base.id for base in node.bases if isinstance(base, ast.Name)}
        if not bases & {"Enum", "StrEnum", "IntEnum"}:
            continue
        values: set[str] = set()
        for statement in node.body:
            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Constant):
                values.add(str(statement.value.value))
        found[node.name] = values
    return found


def extended_closed_sets(worktree: Path, sha: str, paths: Sequence[str]) -> dict[str, set[str]]:
    """Return every closed enum in the files whose `<base>..<sha>` change adds a member."""
    extended: dict[str, set[str]] = {}
    for path in paths:
        if not path.endswith(".py"):
            continue
        try:
            before = _enum_members(_git(worktree, "show", f"{_BASE}:{path}"))
        except subprocess.CalledProcessError:
            before = {}
        after = _enum_members(_git(worktree, "show", f"{sha}:{path}"))
        for name, values in after.items():
            added = values - before.get(name, set())
            if added:
                extended[name] = added
    return extended


def decoder_files(staged: Path, extended: Mapping[str, set[str]]) -> list[str]:
    """Return the staged files that construct an extended enum from a value (its decoders)."""
    if not extended:
        return []
    pattern = re.compile(r"\b(" + "|".join(map(re.escape, sorted(extended))) + r")\(")
    found: list[str] = []
    for path in sorted(staged.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for line in path.read_text().splitlines():
            if pattern.search(line) and not line.lstrip().startswith("class "):
                found.append(path.relative_to(staged).as_posix())
                break
    return found


type _FunctionRef = tuple[str, str]
"""(staged-relative file, function name)."""


@dataclass(frozen=True, slots=True)
class _ModuleNames:
    """One module's functions and how its local names resolve to other modules."""

    functions: dict[str, list[ast.AST]]
    imported: dict[str, tuple[str, str]]
    """Local name -> (module, name) from `from module import name`."""
    aliases: dict[str, str]
    """Local name -> module, from `import module [as alias]` or `from package import module`."""


def _module_names(staged: Path, path: Path) -> _ModuleNames:
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _module_name(staged, path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    functions: dict[str, list[ast.AST]] = {}
    imported: dict[str, tuple[str, str]] = {}
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            functions.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                anchor = package.split(".") if package else []
                anchor = anchor[: len(anchor) - (node.level - 1)] if node.level > 1 else anchor
                base = ".".join([*anchor, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                if _module_file(staged, f"{base}.{alias.name}") is not None:
                    aliases[local] = f"{base}.{alias.name}"
                else:
                    imported[local] = (base, alias.name)
    return _ModuleNames(functions, imported, aliases)


def _relative(staged: Path, module: str) -> str | None:
    found = _module_file(staged, module)
    return None if found is None else found.relative_to(staged).as_posix()


def _references(
    staged: Path, file: str, *, names: _ModuleNames, body: ast.AST
) -> set[_FunctionRef]:
    """Resolve every function a body names, by local definition, from-import or module alias."""
    found: set[_FunctionRef] = set()
    for node in ast.walk(body):
        if isinstance(node, ast.Name):
            if node.id in names.functions:
                found.add((file, node.id))
            elif node.id in names.imported:
                module, name = names.imported[node.id]
                target = _relative(staged, module)
                if target is not None:
                    found.add((target, name))
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            module = names.aliases.get(node.value.id)
            if module is not None:
                target = _relative(staged, module)
                if target is not None:
                    found.add((target, node.attr))
    return found


def _decoder_seeds(
    names: Mapping[str, _ModuleNames], decoders: set[str], enums: set[str]
) -> set[_FunctionRef]:
    """Return the functions in decoder files whose bodies construct an extended enum."""
    seeds: set[_FunctionRef] = set()
    for file in set(names) & decoders:
        for function, nodes in names[file].functions.items():
            calls = (call for node in nodes for call in ast.walk(node))
            if any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id in enums
                for call in calls
            ):
                seeds.add((file, function))
    return seeds


def decoding_entry_points(
    staged: Path, handlers: Iterable[str], *, decoders: set[str], enums: set[str]
) -> list[str]:
    """Return the handlers whose call graph reaches a function that decodes an extended enum.

    Name-resolved and conservative within the closure: a function is a decoder if its body
    constructs one of `enums` from a value; any function that names a decoder is one too, to a
    fixed point. A handler is reached when its entry function is a decoder by that rule.
    """
    if not decoders or not enums:
        return []
    entries = [item.partition(":") for item in handlers]
    files: set[str] = set()
    for module, _, _ in entries:
        files |= closure_names(staged, [module])
    names = {file: _module_names(staged, staged / file) for file in files}
    seeds = _decoder_seeds(names, decoders, enums)
    changed = True
    while changed:
        changed = False
        for file, module_names in names.items():
            for function, nodes in module_names.functions.items():
                if (file, function) in seeds:
                    continue
                if any(
                    _references(staged, file, names=module_names, body=node) & seeds
                    for node in nodes
                ):
                    seeds.add((file, function))
                    changed = True
    reached: list[str] = []
    for module, _, function in entries:
        file = _relative(staged, module)
        if file is None:
            continue
        # A `run.sh` entry runs the whole module: any decoder function in it counts.
        candidates = names[file].functions if function == "*" else [function]
        if any((file, name) in seeds for name in candidates):
            reached.append(f"{module}:{function}")
    return sorted(reached)


def closure_names(staged: Path, modules: Iterable[str]) -> set[str]:
    """Return the staged-relative paths of the modules' combined import closure."""
    closure: set[Path] = set()
    for module in modules:
        closure |= import_closure(staged, module)
    return {path.relative_to(staged).as_posix() for path in closure}


def parse_lagging_files(values: Sequence[str]) -> dict[str, str]:
    """Parse `--lagging-file <path>=<blob sha>`; an unpinned file is refused (B7)."""
    pins: dict[str, str] = {}
    for value in values:
        path, separator, blob = value.partition("=")
        if not separator or re.fullmatch(r"[0-9a-f]{40}", blob) is None:
            raise ProbeError(f"--lagging-file {value!r} must be <path>=<40-hex blob sha>")
        pins[path] = blob
    return pins


@dataclass(frozen=True, slots=True)
class AssetScanRequest:
    """The scan's inputs: the commit, the staged tree and the stack set it excludes."""

    worktree: Path
    sha: str
    staged: Path
    environment: str
    set_stacks: frozenset[str]
    lagging_stacks: frozenset[str] = frozenset()
    """Stacks the orchestrator accepted as lagging (R5-2 option 2, 2026-10-07, under the owner's
    overnight authorization; recorded in lagging-stacks.md, owner to confirm)."""
    lagging_files: Mapping[str, str] = field(default_factory=dict[str, str])
    """The only changed files a lagging stack's closure may reach, each pinned to its blob."""


def record_assets(worktree: Path, stack: str) -> dict[str, object]:
    """Record a synthesized stack's function keys and the shared source-asset key."""
    out = worktree / "infra" / "cdk.out"
    template = json.loads((out / f"{stack}.template.json").read_text())
    assets = json.loads((out / f"{stack}.assets.json").read_text())
    source_keys: list[str] = []
    for asset_hash, asset in cast("dict[str, Any]", assets.get("files", {})).items():
        source = cast("dict[str, Any]", asset.get("source", {}))
        directory = out / str(source.get("path", ""))
        if directory.is_dir() and all(
            (directory / name).exists() for name in ("domain", "ports", "adapters", "run.sh")
        ):
            source_keys.append(f"{asset_hash}.zip")
    if len(source_keys) > 1:
        raise ProbeError("more than one staged source asset in one stack")
    return {
        "stack": stack,
        "source_asset_key": source_keys[0] if source_keys else None,
        "functions": function_assets(template),
    }


def deployed_sha(
    worktree: Path, runner_sha: str, assets_files: Sequence[Path]
) -> dict[str, object]:
    """Name the one commit every assets file was recorded at, and link it to the runner (B8).

    Every assets file must carry the same `sha`, and `<deployed>..<runner>` may change nothing
    under `src/` or `contracts/`, so the probe ran against exactly the deployed source.
    """
    recorded = sorted({str(json.loads(path.read_text()).get("sha", "")) for path in assets_files})
    single = len(recorded) == 1 and bool(recorded[0])
    moved: list[str] = []
    if single:
        moved = [
            line
            for line in _git(
                worktree,
                "diff",
                "--name-only",
                f"{recorded[0]}..{runner_sha}",
                "--",
                "src",
                "contracts",
            ).splitlines()
            if line
        ]
    return {
        "deployed_sha": recorded[0] if single else None,
        "assets_file_shas": recorded,
        "runner_sha": runner_sha,
        "source_changed_since_deploy": moved,
        "passed": single and not moved,
    }


def deployed_asset_readback(
    session: Any,
    assets_files: Sequence[Path],
    *,
    stack_clients: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """Compare every set stack's deployed keys with its recorded file; one source key overall.

    Without `stack_clients` every stack is read through `session`'s Region. With it, each stack is
    read through its own CloudFormation client (a stack in another Region, such as the edge
    stack), and a recorded stack it does not name is refused.
    """
    shared: Any = None
    stacks: dict[str, object] = {}
    keys: set[str] = set()
    equal = True
    for path in assets_files:
        recorded = json.loads(path.read_text())
        stack = str(recorded["stack"])
        if stack_clients is not None:
            if stack not in stack_clients:
                raise ProbeError(f"no CloudFormation client for {stack} in its own Region")
            cloudformation = stack_clients[stack]
        else:
            if shared is None:
                shared = session.client("cloudformation")
            cloudformation = shared
        deployed = function_assets(_processed_template(cloudformation, stack))
        source_key = cast("str | None", recorded.get("source_asset_key"))
        result = compare_assets(recorded["functions"], deployed, source_key)
        # aws-pagination: an exact StackName returns one stack.
        status = cloudformation.describe_stacks(StackName=stack)["Stacks"][0]
        result["stack_status"] = status["StackStatus"]
        result["last_updated"] = str(status.get("LastUpdatedTime", ""))
        result["assets_file_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        stacks[stack] = result
        equal = equal and bool(result["equal"]) and str(status["StackStatus"]).endswith("_COMPLETE")
        if source_key is not None:
            keys.add(source_key)
    return {
        "stacks": stacks,
        "one_source_key": len(keys) <= 1,
        "source_keys": sorted(keys),
        "passed": equal and len(keys) <= 1,
    }


def stack_resources(cloudformation: Any, stack: str) -> list[dict[str, Any]]:
    """Every resource of one stack, through the one paginated stack-resource reader."""
    from adapters.outbound.cloudformation_stack import stack_resource_summaries

    return [dict(item) for item in stack_resource_summaries(cloudformation, stack)]


def _alarm_inventory(session: Any, environment: str) -> dict[str, object]:
    """Check the P0 invariant: required alarms exist, every action exactly the staffed topic."""
    cloudformation = session.client("cloudformation")
    stack = f"Adlytics-Ingestion-{environment}"
    resources = stack_resources(cloudformation, stack)
    alarms = [
        str(item["PhysicalResourceId"])
        for item in resources
        if item["ResourceType"] == "AWS::CloudWatch::Alarm"
    ]
    topics = [
        str(item["PhysicalResourceId"])
        for item in resources
        if item["ResourceType"] == "AWS::SNS::Topic"
        and str(item["PhysicalResourceId"]).endswith(f":adlytics-{environment}-ingestion-alarms")
    ]
    if len(topics) != 1:
        raise ProbeError("the staffed ingestion alarm topic is not exactly one stack resource")
    pages = (
        session.client("cloudwatch").get_paginator("describe_alarms").paginate(AlarmNames=alarms)
    )
    found: dict[str, object] = {}
    failures: list[str] = []
    for alarm in (item for page in pages for item in page.get("MetricAlarms", [])):
        name = str(alarm["AlarmName"])
        actions = sorted(str(item) for item in alarm.get("AlarmActions", []))
        expressions = [str(metric.get("Expression", "")) for metric in alarm.get("Metrics", [])]
        found[name] = {"state": alarm.get("StateValue"), "actions": actions}
        if actions != topics:
            failures.append(f"{name}: actions differ from the staffed topic")
        if "-alarming-errors-" in name and not any(_ERRORS_SCHEMA in e for e in expressions):
            failures.append(
                f"{name}: errors schema is not exactly ErrorClass, ErrorCode, Operation"
            )
    for suffix in _REQUIRED_ALARMS:
        if f"adlytics-{environment}-{suffix}" not in found:
            failures.append(f"missing alarm {suffix}")
    return {"topic": topics[0], "alarms": found, "failures": failures, "passed": not failures}


def _insights(  # noqa: PLR0917
    logs: Any,
    group: str,
    query: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, str]]:
    started = logs.start_query(
        logGroupName=group,
        startTime=int(start.timestamp()),
        endTime=int(end.timestamp()) + 60,
        queryString=query,
        limit=10000,
    )
    while True:
        result = logs.get_query_results(queryId=started["queryId"])
        if result["status"] in _TERMINAL_QUERY_STATES:
            break
        time.sleep(1.0)
    if result["status"] != "Complete":
        raise ProbeError(f"log query ended {result['status']}")
    return [
        {str(cell["field"]): str(cell["value"]) for cell in row}
        for row in result.get("results", [])
    ]


def _log_group(session: Any, stack: str, fragment: str) -> str:
    resources = stack_resources(session.client("cloudformation"), stack)
    functions = [
        str(item["PhysicalResourceId"])
        for item in resources
        if item["ResourceType"] == "AWS::Lambda::Function"
        and fragment in str(item["LogicalResourceId"])
    ]
    if len(functions) != 1:
        raise ProbeError(f"{fragment}: expected one function in {stack}, found {len(functions)}")
    configuration = session.client("lambda").get_function_configuration(FunctionName=functions[0])
    group = cast("dict[str, str]", configuration.get("LoggingConfig", {})).get("LogGroup")
    return group or f"/aws/lambda/{functions[0]}"


def _exact_versions(s3: Any, bucket: str, key: str) -> list[str]:
    versions: list[str] = []
    for page in s3.get_paginator("list_object_versions").paginate(Bucket=bucket, Prefix=key):
        versions.extend(
            str(item["VersionId"]) for item in page.get("Versions", []) if item["Key"] == key
        )
    return versions


def _land(target: Target, request: LandRequest) -> str:
    argv = [
        "uv",
        "run",
        "python",
        "-m",
        "adapters.jobs.land_fixtures",
        "--profile",
        target.profile,
        "--region",
        target.region,
        "--expected-account",
        target.expected_account,
        "--env",
        target.environment,
        "--operation-id",
        request.operation_id,
        "--stack",
        request.raw_stack,
        "--bucket",
        request.bucket,
        "--probe-retrieval",
        FIXED_PROBE_DELIVERY,
    ]
    if request.dry_run:
        argv.append("--dry-run")
    if request.graph_store_stack is not None and not request.no_twin:
        # S6 (V-19): the twin is decided against the named store stack's live table.
        argv += ["--graph-store-stack", request.graph_store_stack]
    elif request.no_twin or not request.dry_run:
        # Earlier probes never sent a twin; S6 made the twin explicit, so they keep put-only.
        argv.append("--no-twin")
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        argv, cwd=request.worktree, capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        raise ProbeError(f"land_fixtures exited {completed.returncode}: {completed.stderr[-400:]}")
    return completed.stdout


@dataclass(frozen=True, slots=True)
class LandRequest:
    """One land_fixtures probe-retrieval invocation."""

    worktree: Path
    operation_id: str
    raw_stack: str
    bucket: str
    dry_run: bool
    no_twin: bool = False
    """S3 (P3): put only, so the probe input writes nothing to the live graph table."""
    graph_store_stack: str | None = None
    """S6 (P6): send the on_demand twin, decided against this store stack's live table."""


def _window_rows(session: Any, environment: str, days: Sequence[date]) -> dict[str, object]:
    """Read the fixed contract's CURRENT publications and fenced rows for the window's days."""
    from probe_amc_current_invariants import (  # pyright: ignore[reportMissingImports]
        QueryExecutionRequest,
        QueryTarget,
        publication_clause,
        publication_scope,
        query,
    )

    from adapters.jobs.reprocess_semantic_v2.resources import resolve_named_semantic_resources
    from adapters.outbound.dynamodb.semantic_state import DynamoCurrentPublications
    from domain.semantics.publication import PartitionIdentity
    from domain.semantics.time import MeasurementBucket
    from domain.shared_kernel.values import DateRange

    resources = resolve_named_semantic_resources(
        environment, session.client("cloudformation"), session.client("ssm")
    )
    current = DynamoCurrentPublications(resources["publications"], session.client("dynamodb"))
    scope, pack = publication_scope(FIXED_PROBE_CONTRACT, _MARKETPLACE_KEY)
    partitions = tuple(
        PartitionIdentity(
            scope,
            MeasurementBucket(
                DateRange(day, day),
                pack.time.grain,
                scope.business_calendar,
                scope.source_timezone,
                pack.time.observation_basis,
            ),
        )
        for day in days
    )
    publications = current.current_exact(partitions)
    if not publications:
        return {"publication_ids": [], "rows": 0, "rows_sha256": None}
    ids = tuple(item.publication_id.value for item in publications)
    sql = _WINDOW_SQL.format(fence=publication_clause(ids))
    target = QueryTarget(resources["bake_workgroup"], resources["current_database"])
    rows = query(
        QueryExecutionRequest(
            session.client("athena"),
            target,
            sql,
            (min(days).isoformat(), max(days).isoformat(), *ids),
        )
    )
    return {"publication_ids": list(ids), "rows": len(rows), "rows_sha256": rows_digest(rows)}


_WINDOW_SQL: Final = (
    # A-SQL-001: literal names, values bind as ?; see docs/security-dispositions.md
    "SELECT campaign_key, channel_key, CAST(date_key AS varchar), "
    "CAST(impressions AS varchar) FROM fct_ad_exposure "
    "WHERE date_key BETWEEN CAST(? AS date) AND CAST(? AS date) "
    "AND publication_id IN ({fence})"
)


def _plan_digest(key: str, version: str) -> str:
    from domain.operations.write_plan import (
        plan_digest_for_source_delivery,
        source_delivery_identity,
    )

    return plan_digest_for_source_delivery(source_delivery_identity(((key, version),)))


SIBLING_RESERVATION_RACE: Final = {
    "error_code": "ADLY-PLAN_COORD-0001",
    "operation": "BeginTableAttemptRequest",
    "reasons": "[None, None, None, None, ConditionalCheckFailed]",
}
"""The known raised-receive classes: sibling units of one legacy plan contending on the plan's
shared coordinator items. The unit stays unacknowledged and its redelivery succeeds. Both occur
on 2026-10-06, before any traceability deploy, and S3 composes no member into either legacy
transaction (`evidence/s3-tablewriter-refusals-classification.md`,
`evidence/live/S3i3-tablewriter-plan-coord-history-*.json`). The S6 cutover retires them.

- `BeginTableAttemptRequest`, exactly `[None, None, None, None, ConditionalCheckFailed]`: member
  4, the plan `META` `plan_reservation_count`, fails its condition (S3V-3, iteration 2).
- `ClaimUnitRequest`, exactly 7 members, only `None` and at least one `TransactionConflict`:
  two siblings' claims collided (S3 iteration 3, owner-approved fix-forward).

A record is known only when it also names the probe's own plan and its traceback, in the same
log stream, runs through `_process_legacy_unit` (never `_graph_claim`) and ends in the
coordinator call for that operation. Anything else is unclassified and fails the probe. A known
record is still only accepted with the outcomes in `contention_verdict`."""

_PLAN_COORD_REASONS: Final = re.compile(r"specific reasons \[([^\]]*)\]$")
_CLAIM_MEMBERS: Final = 7
_TRACEBACK_FRAME: Final = re.compile(r", in (\w+)\n")
_TRACEBACK_WINDOW: Final = timedelta(seconds=2)
_LEGACY_FRAME: Final = "_process_legacy_unit"
_GRAPH_FRAME: Final = "_graph_claim"
_COORDINATOR_CALL: Final = {
    "BeginTableAttemptRequest": "begin_table_attempt",
    "ClaimUnitRequest": "claim_unit",
}


def _reasons(message: str) -> list[str] | None:
    match = _PLAN_COORD_REASONS.search(message)
    return None if match is None else [part.strip() for part in match.group(1).split(",")]


def _known_shape(operation: str | None, reasons: list[str]) -> bool:
    if operation == SIBLING_RESERVATION_RACE["operation"]:
        return reasons == ["None"] * 4 + ["ConditionalCheckFailed"]
    if operation == "ClaimUnitRequest":
        return (
            len(reasons) == _CLAIM_MEMBERS
            and set(reasons) <= {"None", "TransactionConflict"}
            and "TransactionConflict" in reasons
        )
    return False


def _log_instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace(" ", "T")).replace(tzinfo=UTC)


def traceback_frames(
    record: Mapping[str, str], tracebacks: Sequence[Mapping[str, str]]
) -> list[str] | None:
    """The frames of the first traceback in the record's log stream within 2 s after it."""
    stream = record.get("@logStream")
    if not stream or "@timestamp" not in record:
        return None
    at = _log_instant(record["@timestamp"])
    following = sorted(
        (
            (_log_instant(item["@timestamp"]), item)
            for item in tracebacks
            if item.get("@logStream") == stream and "@timestamp" in item
        ),
        key=lambda pair: pair[0],
    )
    for instant, item in following:
        frames = _TRACEBACK_FRAME.findall(str(item.get("@message", "")))
        if frames and at <= instant <= at + _TRACEBACK_WINDOW:
            return frames
    return None


def _known_raised(
    record: Mapping[str, str], plan_digest: str, tracebacks: Sequence[Mapping[str, str]]
) -> bool:
    """One error record is the probe plan's own legacy sibling contention."""
    if record.get("error_code") != SIBLING_RESERVATION_RACE["error_code"]:
        return False
    if not str(record.get("correlation_id", "")).startswith(f"WRITE_PLAN#{plan_digest}#"):
        return False
    operation = record.get("error_context.operation")
    reasons = _reasons(str(record.get("error_context.provider_message", "")))
    if reasons is None or not _known_shape(operation, reasons):
        return False
    frames = traceback_frames(record, tracebacks)
    return (
        frames is not None
        and _LEGACY_FRAME in frames
        and _GRAPH_FRAME not in frames
        and frames[-1:] == [_COORDINATOR_CALL[str(operation)]]
    )


def classify_raised_receives(
    raised: int,
    errors: Sequence[Mapping[str, str]],
    plan_digest: str,
    tracebacks: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    """Treat every raised receive as a finding that must classify (S3V-3).

    `raised` counts the window's `SemanticUnitReceives` with outcome `raised`; `errors` are the
    TableWriter error records and `tracebacks` its traceback lines in the same window. It passes
    only when every error record is the probe plan's own known legacy contention and no raised
    receive lacks such a record. The outcomes are checked separately (`contention_verdict`).
    """
    known = [record for record in errors if _known_raised(record, plan_digest, tracebacks)]
    unknown = [
        dict(record) for record in errors if not _known_raised(record, plan_digest, tracebacks)
    ]
    return {
        "raised_receives": raised,
        "error_records": len(errors),
        "known_sibling_reservation_race": len(known),
        "known_operations": sorted({str(r.get("error_context.operation")) for r in known}),
        "unclassified": unknown,
        "passed": not unknown and raised <= len(known),
    }


@dataclass(frozen=True, slots=True)
class ContentionOutcomes:
    """What must hold after known contention for it to be accepted (S3 iteration 3)."""

    plan_state: Mapping[str, object]
    c4_activations: int
    row_equality: bool


def contention_verdict(
    classification: Mapping[str, object], outcomes: ContentionOutcomes
) -> dict[str, object]:
    """Accept known contention only if every unit attested, the plan completed, C4 is 0 and the
    window's rows are equal before and after; an unclassified record always fails.
    """
    plan = outcomes.plan_state
    units = cast("dict[str, str]", plan.get("units", {}))
    unit_count = int(cast("int", plan.get("unit_count", -1)))
    checks = {
        "classified": bool(classification.get("passed")),
        "every_unit_attested": unit_count > 0
        and len(units) == unit_count
        and all(status == "attested" for status in units.values())
        and plan.get("attested_count") == unit_count,
        "plan_completed": plan.get("status") == "completed" and plan.get("failed_count") == 0,
        "c4_zero": outcomes.c4_activations == 0,
        "rows_equal": outcomes.row_equality,
    }
    known = int(cast("int", classification.get("known_sibling_reservation_race", 0)))
    return {
        "known_contention": known,
        "checks": checks,
        "passed": checks["classified"] and (known == 0 or all(checks.values())),
    }


def _plan_state(session: Any, environment: str, plan_digest: str) -> dict[str, object]:
    """Strongly read the plan's coordinator `META` and `UNIT#` items (read-only)."""
    name = f"/adlytics/{environment}/data/v2/semantic-publications-table"
    table = str(session.client("ssm").get_parameter(Name=name)["Parameter"]["Value"])
    dynamodb = session.client("dynamodb")

    def query(partition: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for page in dynamodb.get_paginator("query").paginate(
            TableName=table,
            ConsistentRead=True,
            KeyConditionExpression="partition_key = :p",
            ExpressionAttributeValues={":p": {"S": partition}},
        ):
            items.extend(page["Items"])
        return items

    root = {str(i["record_key"]["S"]): i for i in query(f"WRITE_PLAN#{plan_digest}")}
    owner = root.get("SUCCESSOR_OWNER", {})
    plan_key = str(owner.get("last_plan_key", {}).get("S", ""))
    items = {str(i["record_key"]["S"]): i for i in query(plan_key)} if plan_key else {}
    meta = items.get("META", {})

    def number(key: str) -> int | None:
        value = meta.get(key, {}).get("N")
        return None if value is None else int(value)

    return {
        "table": table,
        "plan_key": plan_key,
        "status": meta.get("status", {}).get("S"),
        "unit_count": number("unit_count"),
        "attested_count": number("attested_count"),
        "failed_count": number("failed_count"),
        "units": {
            key.removeprefix("UNIT#"): str(item.get("status", {}).get("S"))
            for key, item in items.items()
            if key.startswith("UNIT#")
        },
    }


def _legacy_readback(  # noqa: PLR0917
    session: Any,
    environment: str,
    plan_digest: str,
    start: datetime,
    wait_seconds: int,
) -> dict[str, object]:
    """Wait for the legacy chain and read back C1, C2 and C4 for one plan."""
    stack = f"Adlytics-Ingestion-{environment}"
    logs = session.client("logs")
    writer = _log_group(session, stack, "TableWriter")
    reconciler = _log_group(session, stack, "PlanIntentReconciler")
    deadline = time.monotonic() + wait_seconds
    admission: list[dict[str, str]] = []
    units: list[dict[str, str]] = []
    while True:
        now = datetime.now(UTC)
        admission = _insights(
            logs,
            reconciler,
            "fields plan_digest, flow_id, units_sent, units_admitted, outcome "
            f"| filter plan_digest = '{plan_digest}' and phase = 'intent'",
            start,
            now,
        )
        units = _insights(
            logs,
            writer,
            "fields unit_key, flow_id, parent_flow_id, outcome, operation, receive_count "
            f"| filter internal_plan_key like 'WRITE_PLAN#{plan_digest}#'",
            start,
            now,
        )
        sent = max((int(item.get("units_sent", "0") or 0) for item in admission), default=0)
        terminal = {
            item["unit_key"]
            for item in units
            if item.get("outcome") in _TERMINAL_UNIT_OUTCOMES and "unit_key" in item
        }
        if sent and len(terminal) >= sent:
            break
        if time.monotonic() > deadline:
            break
        time.sleep(30.0)
    receives = _insights(
        logs,
        writer,
        "fields Outcome, FinalReceive | filter @message like '\"SemanticUnitReceives\"'",
        start,
        datetime.now(UTC),
    )
    activations = [item for item in units if item.get("outcome") == "admission_incomplete"]
    errors = _insights(
        logs,
        writer,
        "fields @timestamp, @logStream, correlation_id, error_code, error_context.operation, "
        "error_context.provider_message | filter ispresent(error_code)",
        start,
        datetime.now(UTC),
    )
    tracebacks = _insights(
        logs,
        writer,
        "fields @timestamp, @logStream, @message "
        "| filter @message like 'Traceback (most recent call last)'",
        start,
        datetime.now(UTC),
    )
    raised = classify_raised_receives(
        sum(1 for item in receives if item.get("Outcome") == "raised"),
        errors,
        plan_digest,
        tracebacks,
    )
    plan_state = _plan_state(session, environment, plan_digest)
    return {
        "plan_digest": plan_digest,
        "c2_admission_records": admission,
        "unit_records": len(units),
        "unit_keys": sorted({item.get("unit_key", "") for item in units}),
        "units_with_flow_ids": sum(
            1 for item in units if item.get("flow_id") and item.get("parent_flow_id")
        ),
        "terminal_units": sorted(terminal),
        "outcomes": sorted({f"{item.get('operation')}:{item.get('outcome')}" for item in units}),
        "semantic_unit_receives_in_window": len(receives),
        "receive_outcomes": sorted({str(item.get("Outcome")) for item in receives}),
        "c4_activations": len(activations),
        "raised_receive_findings": raised,
        "plan_state": plan_state,
        "passed": bool(admission)
        and bool(raised["passed"])
        and bool(units)
        and all(item.get("flow_id") and item.get("parent_flow_id") for item in units)
        and sent > 0
        and len(terminal) >= sent
        and bool(receives)
        and not activations,
    }


def _clean_head(worktree: Path, sha: str) -> str:
    """Require HEAD to be the recorded commit and the tree clean outside `.agents/`."""
    head = _git(worktree, "rev-parse", "HEAD").strip()
    if head != sha:
        raise ProbeError("worktree HEAD differs from --sha")
    status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
    if [line for line in status.splitlines() if not line.startswith("?? .agents/")]:
        raise ProbeError("the worktree is not clean")
    return head


def _preflight(
    target: Target, arguments: argparse.Namespace, evidence: Evidence
) -> tuple[Any, bool]:  # boto3 session is untyped here
    """Record the commit, set readback, import scan and alarm inventory; return the session."""
    worktree = Path(arguments.worktree).resolve()
    evidence.document["sha"] = _clean_head(worktree, str(arguments.sha))
    evidence.document["diff_files"] = {
        path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in arguments.diff_file
    }
    session = _session(target)
    evidence.document["identity"] = "verified"
    readback = deployed_asset_readback(session, [Path(p) for p in arguments.assets_file])
    evidence.document["asset_readback"] = readback
    deployed = deployed_sha(worktree, str(arguments.sha), [Path(p) for p in arguments.assets_file])
    evidence.document["deployed_sha"] = deployed["deployed_sha"]
    evidence.document["deployed_sha_link"] = deployed
    readback["passed"] = bool(readback["passed"] and deployed["passed"])
    scan = asset_scan(
        session,
        AssetScanRequest(
            worktree,
            str(arguments.sha),
            worktree / "build" / "src",
            target.environment,
            frozenset(arguments.set_stack),
            frozenset(arguments.lagging_stack),
            parse_lagging_files(arguments.lagging_file),
        ),
    )
    evidence.document["asset_scan"] = scan
    inventory = _alarm_inventory(session, target.environment)
    evidence.document["alarm_inventory"] = inventory
    return session, bool(readback["passed"] and scan["passed"] and inventory["passed"])


def _window_days(key: str) -> list[date]:
    window = re.search(r"/window=(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})/", key)
    if window is None:
        raise ProbeError("the probe key carries no window")
    first, last = (date.fromisoformat(item) for item in window.groups())
    return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]


def _probe_p0(target: Target, arguments: argparse.Namespace) -> int:
    evidence = Evidence("P0", _utc_stamp())
    try:
        session, ready = _preflight(target, arguments, evidence)
        worktree = Path(arguments.worktree).resolve()

        def land(*, dry_run: bool) -> dict[str, object]:
            request = LandRequest(
                worktree, arguments.operation_id, arguments.raw_stack, arguments.bucket, dry_run
            )
            return parse_probe_report(_land(target, request))

        report = land(dry_run=True)
        evidence.document["dry_run"] = report
        s3 = session.client("s3")
        original = _exact_versions(s3, arguments.bucket, str(report["original_key"]))
        evidence.document["original_versions"] = len(original)
        if len(original) != 1:
            raise ProbeError("the fixed delivery's original is not landed exactly once")
        days = _window_days(str(report["key"]))
        before = _window_rows(session, target.environment, days)
        evidence.document["rows_before"] = before
        if not ready:
            raise ProbeError("asset readback, import scan or alarm inventory failed; no put")
        start = datetime.now(UTC) - timedelta(seconds=60)
        real = land(dry_run=False)
        if real["key"] != report["key"]:
            raise ProbeError("the real put chose a different key than the dry run")
        evidence.document["created_keys"] = [real["data_key"], real["key"]]
        versions = _exact_versions(s3, arguments.bucket, str(real["key"]))
        if len(versions) != 1:
            raise ProbeError("the probe key does not have exactly one version")
        digest = _plan_digest(str(real["key"]), versions[0])
        legacy = _legacy_readback(
            session, target.environment, digest, start, int(arguments.wait_seconds)
        )
        evidence.document["legacy"] = legacy
        after = _window_rows(session, target.environment, days)
        evidence.document["rows_after"] = after
        equal = before["rows_sha256"] is not None and before["rows_sha256"] == after["rows_sha256"]
        evidence.document["row_equality"] = equal
        evidence.document["publications_changed"] = (
            before["publication_ids"] != after["publication_ids"]
        )
        contention = contention_verdict(
            cast("dict[str, object]", legacy["raised_receive_findings"]),
            ContentionOutcomes(
                cast("dict[str, object]", legacy["plan_state"]),
                int(cast("int", legacy["c4_activations"])),
                equal,
            ),
        )
        evidence.document["contention"] = contention
        evidence.document["passed"] = bool(
            ready and legacy["passed"] and equal and contention["passed"]
        )
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1


def _operator(
    target: Target, arguments: argparse.Namespace, *command: str
) -> tuple[int, dict[str, object]]:
    """Run one operator CLI command and return its exit status and `RESULT` document."""
    argv = [
        "uv",
        "run",
        "python",
        "-m",
        "adapters.jobs.execution_graph.operator",
        "--profile",
        target.profile,
        "--region",
        target.region,
        "--expected-account",
        target.expected_account,
        "--env",
        target.environment,
        "--store-stack",
        str(arguments.store_stack),
        *command,
    ]
    completed = subprocess.run(  # noqa: S603 -- fixed argv, explicit inputs only
        argv, cwd=Path(arguments.worktree).resolve(), capture_output=True, text=True, check=False
    )
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    if len(lines) != 1:
        raise ProbeError(f"operator printed {len(lines)} results: {completed.stderr[-400:]}")
    return completed.returncode, cast("dict[str, object]", json.loads(lines[0][7:]))


def _table_census(session: Any, table: str) -> dict[str, int]:
    """Count every live-table item by its pk family (a consistent full scan; the table is small)."""
    counts: dict[str, int] = {}
    pages = (
        session.client("dynamodb")
        .get_paginator("scan")
        .paginate(TableName=table, ConsistentRead=True, ProjectionExpression="pk, sk")
    )
    for page in pages:
        for item in page.get("Items", []):
            family = str(item["pk"]["S"]).split("#", 1)[0]
            sk = str(item["sk"]["S"]).split("#", 1)[0]
            name = family if family != "DUE" else f"DUE/{sk}"
            counts[name] = counts.get(name, 0) + 1
    return counts


def _store_table(session: Any, environment: str, store_stack: str) -> str:
    path = f"/adlytics/{environment}/operations/execution-graph-table-name"
    name = str(session.client("ssm").get_parameter(Name=path)["Parameter"]["Value"])
    owned = any(
        item["ResourceType"] == "AWS::DynamoDB::Table" and item["PhysicalResourceId"] == name
        for item in stack_resources(session.client("cloudformation"), store_stack)
    )
    if not owned:
        raise ProbeError("the named store stack does not own the execution-graph table")
    return name


def _p1_table_invariants(session: Any, table: str, store_stack: str) -> dict[str, object]:
    dynamodb = session.client("dynamodb")
    # aws-pagination: DynamoDB DescribeTable reads one named table.
    described = dynamodb.describe_table(TableName=table)["Table"]
    backups = dynamodb.describe_continuous_backups(TableName=table)["ContinuousBackupsDescription"]
    template = _processed_template(session.client("cloudformation"), store_stack)
    resources = cast("dict[str, dict[str, object]]", template.get("Resources", {}))
    policies = [
        str(item.get("DeletionPolicy"))
        for item in resources.values()
        if item.get("Type") == "AWS::DynamoDB::Table"
    ]
    pitr = backups["PointInTimeRecoveryDescription"]["PointInTimeRecoveryStatus"]
    sse = described.get("SSEDescription", {}).get("Status")
    result: dict[str, object] = {
        "deletion_policies": policies,
        "deletion_protection": described.get("DeletionProtectionEnabled"),
        "sse_status": sse,
        "pitr": pitr,
    }
    result["passed"] = (
        bool(policies)
        and all(policy == "Retain" for policy in policies)
        and described.get("DeletionProtectionEnabled") is True
        and sse == "ENABLED"
        and pitr == "ENABLED"
    )
    return result


GRAPH_WORKER_ROLE_PREFIXES: Final = tuple(
    f"Stage1ExecutionGraph{name}ExecutionRole"
    for name in ("Dispatcher", "FenceSettler", "NodeVerifier", "Promoter", "Release")
)
"""The graph-authority worker execution roles (S3's four, and S4's release issuer)."""
FINALIZER_ROLE_PREFIX: Final = "ProducerFinalizerExecutionRole"
ADMISSION_REQUESTER_ROLE_PREFIXES: Final = ("IngestionExecutionRole", FINALIZER_ROLE_PREFIX)
"""S6 (V-19, S1A-7): the two workload requesters, which admit exactly as the run-request API."""
INGESTION_LIVE_WRITER_PREFIXES: Final = (
    *GRAPH_WORKER_ROLE_PREFIXES,
    *ADMISSION_REQUESTER_ROLE_PREFIXES,
)
"""Every ingestion-stack live-table writer role from S6 on."""
GRAPH_CHAIN_WRITERS: Final = {
    "Ingestion": ("Stage1TableWriterExecutionRole",),
    "Publisher": ("BakerExecutionRole",),
    "InsightPrecompute": ("PrecomputeServiceRole",),
}
"""S6 part 2 (§2.11 rows 2, 5 and 7): the GRAPH chain's other live-table writers, by stack."""


def graph_consumer_writers(environment: str) -> dict[str, tuple[str, ...]]:
    """Return the two consumer stacks' physical names and the writer prefix each must carry."""
    return {
        f"Adlytics-{stack}-{environment}": prefixes
        for stack, prefixes in GRAPH_CHAIN_WRITERS.items()
        if stack != "Ingestion"
    }


def consumer_writers_verdict(
    writers: Mapping[str, Sequence[Mapping[str, object]]],
    consumers: Mapping[str, Sequence[str]],
) -> dict[str, object]:
    """Pass when each consumer stack's live-table writers are exactly its one declared role."""
    found: dict[str, list[str]] = {}
    for stack in consumers:
        names: set[str] = set()
        for item in writers.get(stack, ()):
            for role in cast("list[object]", item["roles"]):
                mapping = cast("Mapping[str, object]", role) if isinstance(role, dict) else None
                reference = role if mapping is None else mapping.get("Ref")
                names.add(str(reference))
        found[stack] = sorted(names)
    passed = all(
        len(found[stack]) == 1 and any(found[stack][0].startswith(prefix) for prefix in prefixes)
        for stack, prefixes in consumers.items()
    )
    return {"roles": found, "passed": passed}


def provisioned_writer_prefixes(template: Mapping[str, object]) -> tuple[str, ...]:
    """The ingestion live-table writer prefixes one processed ingestion template provisions.

    The registration finalizer exists only where producer onboarding is enabled, so its prefix
    is required only when its execution role is in the template. Every other writer is
    unconditional, and its absence still fails.
    """
    resources = cast("Mapping[str, Mapping[str, object]]", template.get("Resources", {}))
    finalizer = any(
        name.startswith(FINALIZER_ROLE_PREFIX) and resource.get("Type") == "AWS::IAM::Role"
        for name, resource in resources.items()
    )
    return tuple(
        prefix
        for prefix in INGESTION_LIVE_WRITER_PREFIXES
        if finalizer or prefix != FINALIZER_ROLE_PREFIX
    )


def sole_writer_verdict(
    writers: Mapping[str, Sequence[Mapping[str, object]]],
    expected: Mapping[str, int],
    prefixes: Sequence[str] = INGESTION_LIVE_WRITER_PREFIXES,
) -> dict[str, object]:
    """Compare the live-table writer roles per stack with the slice's declared writer set.

    P1 (S1) declared the run-request admission role alone. S3 adds the four graph-authority
    workers in the ingestion stack, S4 the release issuer, and S6 the two workload requesters
    (the delivery handler and the registration finalizer). Each ingestion writer must carry
    exactly one of the worker execution-role logical-ID prefixes, and together they must cover
    all of them, so a swap of one worker's grant to another role fails too (S3V-13).
    """
    roles: dict[str, list[str]] = {}
    for stack, found in writers.items():
        for item in found:
            for role in cast("list[object]", item["roles"]):
                reference = role.get("Ref") if isinstance(role, dict) else role
                roles.setdefault(stack, []).append(str(reference))
    counts = {stack: len(set(names)) for stack, names in roles.items()}
    ingestion = [stack for stack in roles if stack.startswith("Adlytics-Ingestion-")]
    # CDK prefixes each logical ID with its construct path and suffixes a hash.
    named = [
        [prefix for prefix in prefixes if name.startswith(prefix)]
        for stack in ingestion
        for name in set(roles[stack])
    ]
    workers_named = all(len(found) == 1 for found in named) and (
        not ingestion or {found[0] for found in named if found} == set(prefixes)
    )
    return {
        "roles": {stack: sorted(set(names)) for stack, names in roles.items()},
        "counts": counts,
        "expected": dict(expected),
        "prefixes": list(prefixes),
        "passed": counts == dict(expected) and workers_named,
    }


def _p1_sole_writer(
    session: Any,
    environment: str,
    expected: Mapping[str, int] | None = None,
    *,
    graph_chain: bool = False,
) -> dict[str, object]:
    """Read every deployed template's live-graph-table writers and compare the declared set.

    S6 part 2 (§2.11 row 3): the delivery handler's slice 3 writes (node, token, lock, `ARM#`,
    `RELEASE#`, fence, re-attestation and lease release) ride its admission-requester grant, so
    its existing prefix covers them. With `graph_chain`, the chain's other writers are declared
    too: TableWriter in the ingestion stack, and one consumer role each in Publisher and
    InsightPrecompute (`GRAPH_CHAIN_WRITERS`), each by its role prefix.
    """
    cloudformation = session.client("cloudformation")
    writers: dict[str, object] = {}
    ingestion_stack = f"Adlytics-Ingestion-{environment}"
    prefixes: tuple[str, ...] = INGESTION_LIVE_WRITER_PREFIXES
    for stack in _stacks(cloudformation, environment):
        template = _processed_template(cloudformation, stack)
        if stack == ingestion_stack:
            prefixes = provisioned_writer_prefixes(template)
            if graph_chain:
                prefixes = (*prefixes, *GRAPH_CHAIN_WRITERS["Ingestion"])
        found = table_writers(
            template, ["execution-graph-table-arn", f"adlytics-{environment}-execution-graph"]
        )
        if found:
            writers[stack] = found
    roles = [
        role
        for found in writers.values()
        for item in cast("list[dict[str, object]]", found)
        for role in cast("list[object]", item["roles"])
    ]
    if expected is not None:
        # S6: the ingestion stack's writers are the set its template provisions.
        typed = cast("Mapping[str, Sequence[Mapping[str, object]]]", writers)
        consumers = graph_consumer_writers(environment) if graph_chain else {}
        verdict = sole_writer_verdict(
            typed,
            {**expected, ingestion_stack: len(prefixes), **dict.fromkeys(consumers, 1)},
            prefixes,
        )
        if graph_chain:
            named = consumer_writers_verdict(typed, consumers)
            passed = bool(verdict["passed"]) and bool(named["passed"])
            verdict = {**verdict, "consumers": named, "passed": passed}
        return {"writers": writers, **verdict}
    passed = list(writers) == [f"Adlytics-Api-{environment}"] and len(roles) == 1
    return {"writers": writers, "passed": passed}


def _probe_p1(target: Target, arguments: argparse.Namespace) -> int:
    evidence = Evidence("P1", _utc_stamp())
    try:
        session, ready = _preflight(target, arguments, evidence)
        p0 = json.loads(Path(arguments.p0_evidence).read_text())
        if not p0.get("passed"):
            raise ProbeError("the P0 evidence did not pass")
        key = str(p0["dry_run"]["key"])
        versions = _exact_versions(session.client("s3"), arguments.bucket, key)
        if len(versions) != 1:
            raise ProbeError("the P0 probe key does not have exactly one version")
        table = _store_table(session, target.environment, str(arguments.store_stack))
        evidence.document["p0_probe_key"] = key
        evidence.document["table_invariants"] = _p1_table_invariants(
            session, table, str(arguments.store_stack)
        )
        evidence.document["sole_writer"] = _p1_sole_writer(session, target.environment)
        status, mode = _operator(target, arguments, "show-mode")
        evidence.document["mode"] = mode
        if mode.get("mode") != "SHADOW":
            raise ProbeError("the mode does not read SHADOW; run the operator CAS first")
        stamp = evidence.started_at.lower()
        admit = (
            "admit",
            "--manifest-key",
            key,
            "--version-id",
            versions[0],
            "--reason",
            "operator_rerun",
            "--raw-stack",
            str(arguments.raw_stack),
            "--bucket",
            str(arguments.bucket),
        )
        before = _table_census(session, table)
        refusals: dict[str, object] = {}
        for name, extra in (
            ("full_history", ("--label", "full_history")),
            ("non_v1", ("--protocol-version", "v2")),
        ):
            code, result = _operator(
                target,
                arguments,
                *admit,
                "--request-id",
                f"p1-{name.replace('_', '-')}-{stamp}",
                *extra,
            )
            refusals[name] = {"exit": code, "result": result}
        after_refusals = _table_census(session, table)
        evidence.document["refusals"] = refusals
        evidence.document["census_before"] = before
        evidence.document["census_after_refusals"] = after_refusals
        _, first = _operator(target, arguments, *admit, "--request-id", f"p1-first-{stamp}")
        _, second = _operator(target, arguments, *admit, "--request-id", f"p1-second-{stamp}")
        # S1A-1: reusing the first request ID with another body is receipted once (row 2) under
        # its own key, and an exact replay returns that receipt and writes nothing.
        conflict_args = (
            *admit,
            "--request-id",
            f"p1-first-{stamp}",
            "--reason",
            "bounded_validation",
        )
        _, conflict = _operator(target, arguments, *conflict_args)
        after_conflict = _table_census(session, table)
        _, conflict_replay = _operator(target, arguments, *conflict_args)
        census = _table_census(session, table)
        evidence.document["requests"] = {
            "first": first,
            "second": second,
            "conflict": conflict,
            "conflict_replay": conflict_replay,
        }
        evidence.document["census_after_conflict"] = after_conflict
        evidence.document["census_after"] = census
        first_receipt = cast("dict[str, object]", first["receipt"])
        second_receipt = cast("dict[str, object]", second["receipt"])
        conflict_receipt = cast("dict[str, object]", conflict.get("receipt") or {})
        graph = (
            session.client("dynamodb")
            .get_item(
                TableName=table,
                Key={"pk": {"S": f"GRAPH#{first_receipt['graph_id']}"}, "sk": {"S": "GRAPH"}},
                ConsistentRead=True,
            )
            .get("Item")
        )
        evidence.document["shadow_graph_sha256"] = (
            None
            if graph is None
            else hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
        )
        obligations = sum(
            count
            for family, count in census.items()
            if family in {"SLOT", "RUN", "FINGERPRINT"} or family.startswith("DUE/")
        )
        checks = {
            "refused_full_history": cast("dict[str, object]", refusals["full_history"])["result"]
            == {"command": "admit", "refused": "FULL_HISTORY_NOT_ENABLED"},
            "refused_non_v1": cast("dict[str, object]", refusals["non_v1"])["result"]
            == {"command": "admit", "refused": "PROTOCOL_MIGRATION_UNSUPPORTED"},
            "refusals_wrote_nothing": before == after_refusals,
            "first_admitted_epoch_0": (
                first_receipt.get("decision"),
                first_receipt.get("authority_epoch"),
            )
            == ("ADMITTED", 0),
            "second_joined_epoch_0": (
                second_receipt.get("decision"),
                second_receipt.get("authority_epoch"),
            )
            == ("JOINED", 0),
            "joined_the_same_graph": second_receipt.get("graph_id")
            == first_receipt.get("graph_id"),
            "row_2_conflict_receipted": (
                conflict_receipt.get("row"),
                conflict_receipt.get("decision"),
                conflict_receipt.get("reason"),
            )
            == (2, "REFUSED", "request_id_conflict"),
            "row_2_replay_returns_it_and_writes_nothing": conflict_replay.get("receipt")
            == conflict_receipt
            and census == after_conflict,
            "one_shadow_graph_recorded": graph is not None
            and graph.get("state") == {"S": "SHADOW_RECORDED"},
            "zero_slot_run_and_obligation_rows": obligations == 0,
            "table_invariants": bool(
                cast("dict[str, object]", evidence.document["table_invariants"])["passed"]
            ),
            "sole_writer": bool(
                cast("dict[str, object]", evidence.document["sole_writer"])["passed"]
            ),
            "preflight": ready,
        }
        del status
        evidence.document["checks"] = checks
        evidence.document["passed"] = all(checks.values())
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1


_EXPLANATIONS: Final = "/api/operations/execution-explanations"
_RUN_REQUESTS: Final = "/api/operations/run-requests"
_INVALID_EXPLANATION_CODE: Final = "ADLY-EXEC_EXPLAIN-0001"
_GRAPH_WRITE_ACTIONS: Final = (
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
    "dynamodb:BatchWriteItem",
    "dynamodb:ConditionCheckItem",
)


def _personas() -> Any:
    """Import the validation persona helpers the way the other validation scripts do."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import importlib

    return importlib.import_module("cognito_personas")


def diff_header_check(diff_files: Sequence[Path], deployed: str | None) -> dict[str, object]:
    """M-2: every diff file's `SHA=` header names the deployed commit."""
    headers = {
        str(path): path.read_text().splitlines()[0].removeprefix("SHA=") for path in diff_files
    }
    passed = bool(diff_files) and deployed is not None and set(headers.values()) == {deployed}
    return {"headers": headers, "deployed_sha": deployed, "passed": passed}


def graph_digest(item: Mapping[str, object]) -> str:
    """The P1 shadow-graph digest: SHA-256 of the raw item's canonical JSON."""
    return hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()


def parse_baseline_graphs(values: Sequence[str]) -> dict[str, str]:
    """Parse `--baseline-graph <graph_id>=<sha256>` (M-4: every recorded shadow graph)."""
    pairs: dict[str, str] = {}
    for value in values:
        name, separator, digest = value.partition("=")
        hexes = re.fullmatch(r"[0-9a-f]{64}", name) and re.fullmatch(r"[0-9a-f]{64}", digest)
        if not separator or not hexes:
            raise ProbeError(f"--baseline-graph {value!r} must be <64 hex>=<64 hex>")
        pairs[name] = digest
    return pairs


def page_union(pages: Sequence[Sequence[Mapping[str, object]]]) -> dict[str, object]:
    """Check a paged traversal: no duplicate across pages; return the ordered keys."""
    keys = [f"{row.get('request_id')}#{row.get('receipt_key')}" for page in pages for row in page]
    return {"keys": keys, "duplicates": len(keys) - len(set(keys))}


@dataclass(frozen=True, slots=True)
class ExplanationRead:
    """One full explanation of the probe manifest and the reads it is checked against."""

    document: Mapping[str, object]
    traversal: Mapping[str, object]
    receipt_census: int
    p1_requests: frozenset[str]
    manifest_last_modified: str


def explanation_invariants(read: ExplanationRead) -> dict[str, bool]:
    """P2's receipt, root-clock and two-page traversal invariants, shared by P2 and P3."""
    full = read.document
    receipts = cast("list[dict[str, object]]", full.get("receipts", []))
    stages = cast("list[dict[str, object]]", full.get("stages", []))
    received = stages[0] if stages else {}
    full_keys = [f"{row.get('request_id')}#{row.get('receipt_key')}" for row in receipts]
    traversal = read.traversal
    return {
        "receipts_cover_the_root": len(receipts) == read.receipt_census
        and read.p1_requests <= {str(r["request_id"]) for r in receipts}
        and all(r["label"] == "shadow" and r["attested"] is True for r in receipts),
        "root_received_elapsed_0": received.get("stage") == "received"
        and received.get("flow_root_elapsed_ms") == 0
        and received.get("occurred_at") == read.manifest_last_modified,
        "two_page_traversal_no_duplicate_or_omission": traversal["duplicates"] == 0
        and traversal["keys"] == full_keys
        and len(cast("list[int]", traversal["page_sizes"])) >= 2,
    }


def p1_request_ids(p1: Mapping[str, object]) -> frozenset[str]:
    """The request IDs P1 admitted, which every later explanation must still carry."""
    requests = cast("dict[str, dict[str, object]]", p1["requests"])
    return frozenset(
        str(cast("dict[str, object]", requests[name]["receipt"])["request_id"])
        for name in ("first", "second")
    )


def root_receipts(session: Any, table: str, request_ids: frozenset[str]) -> dict[str, object]:
    """The receipt count and graph IDs of the root flow P1's requests belong to.

    From S6 on, P6 admits other roots into the live table, so the P1 root's explanation is
    checked against its own receipts and graphs (a consistent scan of `RECEIPT#` items by
    `root_flow_id`) rather than the whole table's. Before S6 the two sets were the same.
    """
    dynamodb = session.client("dynamodb")
    roots: set[str] = set()
    for request_id in sorted(request_ids):
        item = dynamodb.get_item(
            TableName=table,
            Key={"pk": {"S": f"RECEIPT#{request_id}"}, "sk": {"S": "RECEIPT"}},
            ConsistentRead=True,
        ).get("Item")
        if item is None:
            raise ProbeError(f"P1's receipt {request_id} is missing")
        roots.add(str(item["root_flow_id"]["S"]))
    if len(roots) != 1:
        raise ProbeError("P1's requests do not share one root flow")
    root = roots.pop()
    count = 0
    graphs: set[str] = set()
    for page in dynamodb.get_paginator("scan").paginate(
        TableName=table,
        ConsistentRead=True,
        FilterExpression="begins_with(pk, :receipt) AND root_flow_id = :root",
        ExpressionAttributeValues={":receipt": {"S": "RECEIPT#"}, ":root": {"S": root}},
        ProjectionExpression="pk, graph_id",
    ):
        for found in page["Items"]:
            count += 1
            graph = cast("dict[str, object]", found.get("graph_id", {})).get("S")
            if graph:
                graphs.add(str(graph))
    return {"root_flow_id": root, "receipts": count, "graph_ids": sorted(graphs)}


def root_graphs_verdict(
    graphs: Sequence[Mapping[str, object]], root: Mapping[str, object], baseline: set[str]
) -> bool:
    """The explanation names exactly the root's graphs, each recorded, shadow and epoch 0."""
    own = {str(graph_id) for graph_id in cast("list[object]", root["graph_ids"])}
    return (
        bool(own)
        and {str(g["graph_id"]) for g in graphs} == own
        and own <= baseline
        and all(g["authority_epoch"] == 0 and g["label"] == "shadow" for g in graphs)
    )


def _baseline_graphs(session: Any, table: str, baseline: Mapping[str, str]) -> dict[str, object]:
    """M-4: the GRAPH set equals the recorded baseline, and each graph is byte-identical."""
    dynamodb = session.client("dynamodb")
    names: list[str] = []
    for page in dynamodb.get_paginator("scan").paginate(
        TableName=table,
        ConsistentRead=True,
        FilterExpression="begins_with(pk, :graph)",
        ExpressionAttributeValues={":graph": {"S": "GRAPH#"}},
        ProjectionExpression="pk",
    ):
        names.extend(str(item["pk"]["S"]).removeprefix("GRAPH#") for item in page["Items"])
    digests: dict[str, str | None] = {}
    for name in sorted(names):
        item = dynamodb.get_item(
            TableName=table,
            Key={"pk": {"S": f"GRAPH#{name}"}, "sk": {"S": "GRAPH"}},
            ConsistentRead=True,
        ).get("Item")
        digests[name] = None if item is None else graph_digest(item)
    return {
        "digests": digests,
        "baseline": dict(baseline),
        "passed": bool(baseline) and digests == dict(baseline),
    }


def _p1_refusal_invariants(
    target: Target, arguments: argparse.Namespace, admit: Sequence[str], table: str, session: Any
) -> dict[str, object]:
    """P1 (I): the two pre-domain refusals still refuse and write nothing."""
    before = _table_census(session, table)
    stamp = _utc_stamp().lower()
    results: dict[str, object] = {}
    for name, extra, signal in (
        ("full_history", ("--label", "full_history"), "FULL_HISTORY_NOT_ENABLED"),
        ("non_v1", ("--protocol-version", "v2"), "PROTOCOL_MIGRATION_UNSUPPORTED"),
    ):
        _, result = _operator(
            target,
            arguments,
            *admit,
            "--request-id",
            f"p2-{name.replace('_', '-')}-{stamp}",
            *extra,
        )
        results[name] = {"result": result, "passed": result.get("refused") == signal}
    after = _table_census(session, table)
    passed = before == after and all(
        cast("dict[str, object]", item)["passed"] for item in results.values()
    )
    return {"results": results, "census_before": before, "census_after": after, "passed": passed}


def _graph_projection_rebuild(target: Target, worktree: Path) -> dict[str, object]:
    argv = [
        "uv",
        "run",
        "python",
        "-m",
        "adapters.jobs.operational_status.graph_projection_rebuild",
        "--profile",
        target.profile,
        "--region",
        target.region,
        "--expected-account",
        target.expected_account,
        "--env",
        target.environment,
    ]
    completed = subprocess.run(argv, cwd=worktree, capture_output=True, text=True, check=False)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("RESULT ")]
    if completed.returncode != 0 or len(lines) != 1:
        raise ProbeError(f"projection rebuild failed: {completed.stderr[-400:]}")
    return cast("dict[str, object]", json.loads(lines[0][7:]))


@dataclass(frozen=True, slots=True)
class _Api:
    """The deployed origin and one persona's session, never printed."""

    origin: str
    session: str = field(repr=False)


def _persona_api(session: Any, environment: str, label: str) -> _Api:
    personas = _personas()
    ssm = session.client("ssm")
    origin = str(personas.published_parameter(ssm, environment, "publicBaseUrl")).rstrip("/")
    credential = personas.load_persona(ssm, environment, label)
    tokens = personas.sign_in(
        session.client("cognito-idp"), credential, personas.app_client(session, environment)
    )
    return _Api(origin, str(tokens.access_token))


def _explain(api: _Api, body: Mapping[str, object]) -> tuple[int, dict[str, object]]:
    from adapters.jobs.operational_status_cli.client import (
        SessionCredential,
        explain,
    )

    response = explain(api.origin, SessionCredential(api.session), body)
    return response.status, dict(response.body)


def _run_request(
    api: _Api, method: str, path: str, body: Mapping[str, object] | None
) -> tuple[int, dict[str, object]]:
    from adapters.outbound.https import HttpsSession, require_https

    csrf = hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:32]
    with HttpsSession().request(
        method,
        require_https(f"{api.origin}{path}"),
        data=None if body is None else json.dumps(dict(body)).encode(),
        headers={
            "Content-Type": "application/json",
            "Cookie": f"adl_session={api.session}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
            "Sec-Fetch-Site": "same-origin",
        },
        timeout=30,
        allow_redirects=False,
    ) as response:
        try:
            decoded: object = response.json() if response.content else {}
        except ValueError:
            decoded = {}
    return response.status_code, cast("dict[str, object]", decoded) if isinstance(
        decoded, dict
    ) else {}


def _traverse(api: _Api, reference: Mapping[str, object], limit: int) -> dict[str, object]:
    """Walk every page at `limit`, retrying the first page once for cursor byte identity."""
    pages: list[list[dict[str, object]]] = []
    statuses: list[int] = []
    body: dict[str, object] = {**reference, "limit": limit}
    first_cursor: object = None
    retry_cursor: object = None
    for index in range(64):
        status, document = _explain(api, body)
        statuses.append(status)
        if status != 200:
            break
        pages.append(cast("list[dict[str, object]]", document.get("receipts", [])))
        cursor = document.get("next_cursor")
        if index == 0:
            first_cursor = cursor
            _, again = _explain(api, body)
            retry_cursor = again.get("next_cursor")
        if not isinstance(cursor, str):
            break
        body = {**reference, "limit": limit, "cursor": cursor}
    union = page_union(pages)
    return {
        "statuses": statuses,
        "page_sizes": [len(page) for page in pages],
        "keys": union["keys"],
        "duplicates": union["duplicates"],
        "retry_cursor_identical": first_cursor == retry_cursor and isinstance(first_cursor, str),
    }


def _explanation_role_iam(session: Any, environment: str, table_arn: str) -> dict[str, object]:
    """The API read role: no graph write action, by template and by policy simulation."""
    stack = f"Adlytics-Api-{environment}"
    resources = stack_resources(session.client("cloudformation"), stack)
    functions = [
        str(item["PhysicalResourceId"])
        for item in resources
        if item["ResourceType"] == "AWS::Lambda::Function"
        and "OperationalStatusFunction" in str(item["LogicalResourceId"])
    ]
    if len(functions) != 1:
        raise ProbeError("expected one OperationalStatusFunction in the Api stack")
    role_arn = str(
        session.client("lambda").get_function_configuration(FunctionName=functions[0])["Role"]
    )
    simulated = session.client("iam").simulate_principal_policy(
        PolicySourceArn=role_arn,
        ActionNames=[*_GRAPH_WRITE_ACTIONS, "dynamodb:GetItem", "dynamodb:BatchGetItem"],
        ResourceArns=[table_arn],
    )["EvaluationResults"]
    decisions = {str(item["EvalActionName"]): str(item["EvalDecision"]) for item in simulated}
    writes_denied = all(decisions.get(action) != "allowed" for action in _GRAPH_WRITE_ACTIONS)
    reads_allowed = decisions.get("dynamodb:GetItem") == "allowed"
    return {
        "role": role_arn.rsplit("/", 1)[-1],
        "decisions": decisions,
        "passed": writes_denied and reads_allowed,
    }


def error_metric_query(code: str) -> str:
    """Count the `Errors` EMF records of one code.

    Only the EMF record carries the capitalized `ErrorCode` key (the log line says `error_code`),
    and EMF writes `Errors` as an array, so the filter keys on `ErrorCode` and the EMF envelope
    rather than on `ispresent(Errors)`, which never matches an array value.
    """
    return (
        f'fields @timestamp | filter ErrorCode = "{code}" '
        "and @message like /CloudWatchMetrics/ | stats count() as n"
    )


def _error_metric_count(
    session: Any, environment: str, start: datetime, wait_seconds: int
) -> dict[str, object]:
    """Count the invalid request's `Errors` EMF records in the status function's log group."""
    group = _log_group(session, f"Adlytics-Api-{environment}", "OperationalStatusFunction")
    query = error_metric_query(_INVALID_EXPLANATION_CODE)
    logs = session.client("logs")
    deadline = time.monotonic() + wait_seconds
    count = 0
    while time.monotonic() < deadline:
        rows = _insights(logs, group, query, start, datetime.now(UTC))
        count = int(rows[0]["n"]) if rows else 0
        if count:
            break
        time.sleep(20)
    time.sleep(30)
    rows = _insights(logs, group, query, start, datetime.now(UTC))
    settled = int(rows[0]["n"]) if rows else 0
    return {"log_group": group, "first_count": count, "settled_count": settled}


def _receipt_census(session: Any, table: str) -> int:
    return _table_census(session, table).get("RECEIPT", 0)


# BF-5 load check. A throttled explanation is load shedding, not a failure; any other non-200
# (a WAF block, an authorizer refusal, a transport error recorded as -1) fails the check.
_LOAD_SHED_STATUSES: Final = frozenset({429, 500, 502, 503})
_LOAD_WARMUP_SECONDS: Final = 3.0
_LOAD_TAIL_SECONDS: Final = 2.0
# Well under the edge WAF's per-IP five-minute limit, whatever the rest of the probe sends.
_LOAD_MAX_CALLS_PER_WORKER: Final = 24


@dataclass(frozen=True, slots=True)
class TimedCall:
    """One HTTP call's status and its monotonic start and end."""

    status: int
    started: float
    ended: float


def load_overlap(calls: Sequence[TimedCall], window: tuple[float, float]) -> dict[str, object]:
    """Summarize explanation calls around one lifecycle command's window."""
    start, end = window
    overlapping = [call for call in calls if call.started < end and call.ended > start]
    statuses: dict[str, int] = {}
    for call in calls:
        statuses[str(call.status)] = statuses.get(str(call.status), 0) + 1
    return {
        "calls": len(calls),
        "statuses": dict(sorted(statuses.items())),
        "overlapping": len(overlapping),
        "overlapping_ok": sum(1 for call in overlapping if call.status == 200),
        "unexpected": sorted(
            {call.status for call in calls if call.status != 200} - set(_LOAD_SHED_STATUSES)
        ),
    }


def _explanation_worker(
    api: _Api, reference: Mapping[str, object], stop: Any, calls: list[TimedCall]
) -> None:
    """Page the explanation at limit 1, restarting at the first page, until told to stop."""
    body: dict[str, object] = {**reference, "limit": 1}
    for _ in range(_LOAD_MAX_CALLS_PER_WORKER):
        if stop.is_set():
            return
        started = time.monotonic()
        try:
            status, document = _explain(api, body)
        except Exception:
            status, document = -1, {}
        calls.append(TimedCall(status, started, time.monotonic()))
        cursor = document.get("next_cursor") if status == 200 else None
        body = {**reference, "limit": 1, **({"cursor": cursor} if isinstance(cursor, str) else {})}


def _lifecycle_under_explanation_load(
    reader: _Api,
    requester: _Api,
    reference: Mapping[str, object],
    command: Mapping[str, object],
    workers: int,
) -> dict[str, object]:
    """BF-5: commit one lifecycle command while `workers` explanation pagers run against it."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    stop = threading.Event()
    lanes: list[list[TimedCall]] = [[] for _ in range(workers)]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_explanation_worker, reader, reference, stop, lane) for lane in lanes
        ]
        time.sleep(_LOAD_WARMUP_SECONDS)
        started = time.monotonic()
        try:
            status, posted = _run_request(requester, "POST", _RUN_REQUESTS, command)
        finally:
            ended = time.monotonic()
            time.sleep(_LOAD_TAIL_SECONDS)
            stop.set()
        for future in futures:
            future.result()
    calls = [call for lane in lanes for call in lane]
    return {
        "workers": workers,
        "lifecycle": {
            "status": status,
            "receipt": posted.get("receipt"),
            "latency_ms": round((ended - started) * 1000),
        },
        "explanations": load_overlap(calls, (started, ended)),
    }


def _status_concurrency(session: Any, environment: str) -> dict[str, object]:
    """The deployed reservation of the explanation's function, and none on run-request admission."""
    stack = f"Adlytics-Api-{environment}"
    template = _processed_template(session.client("cloudformation"), stack)
    resources = cast("Mapping[str, Mapping[str, Any]]", template.get("Resources", {}))
    declared: dict[str, object] = {}
    for logical_id, resource in resources.items():
        if resource.get("Type") != "AWS::Lambda::Function":
            continue
        for fragment in ("OperationalStatusFunction", "RunRequestsFunction"):
            if logical_id.startswith(fragment):
                declared[fragment] = resource.get("Properties", {}).get(
                    "ReservedConcurrentExecutions"
                )
    client = session.client("lambda")
    live: dict[str, object] = {}
    for fragment in ("OperationalStatusFunction", "RunRequestsFunction"):
        reply = client.get_function_concurrency(FunctionName=f"adlytics-{environment}-{fragment}")
        live[fragment] = reply.get("ReservedConcurrentExecutions")
    unreserved = client.get_account_settings()["AccountLimit"]["UnreservedConcurrentExecutions"]
    status_bound = live.get("OperationalStatusFunction")
    return {
        "declared": declared,
        "live": live,
        "account_unreserved": unreserved,
        "passed": isinstance(status_bound, int)
        and status_bound > 0
        and declared.get("OperationalStatusFunction") == status_bound
        and live.get("RunRequestsFunction") is None
        and declared.get("RunRequestsFunction") is None
        and isinstance(unreserved, int)
        and unreserved > status_bound,
    }


def _probe_p2(target: Target, arguments: argparse.Namespace) -> int:
    evidence = Evidence("P2", _utc_stamp())
    try:
        session, ready = _preflight(target, arguments, evidence)
        worktree = Path(arguments.worktree).resolve()
        headers = diff_header_check(
            [Path(path) for path in arguments.diff_file],
            cast("str | None", evidence.document.get("deployed_sha")),
        )
        evidence.document["diff_headers"] = headers
        p1 = json.loads(Path(arguments.p1_evidence).read_text())
        if not p1.get("passed"):
            raise ProbeError("the P1 evidence did not pass")
        key = str(p1["p0_probe_key"])
        versions = _exact_versions(session.client("s3"), arguments.bucket, key)
        if len(versions) != 1:
            raise ProbeError("the P1 manifest key does not have exactly one version")
        version = versions[0]
        table = _store_table(session, target.environment, str(arguments.store_stack))
        table_arn = str(
            # aws-pagination: DynamoDB DescribeTable reads one named table.
            session.client("dynamodb").describe_table(TableName=table)["Table"]["TableArn"]
        )
        admit = (
            "admit",
            "--manifest-key",
            key,
            "--version-id",
            version,
            "--reason",
            "operator_rerun",
            "--raw-stack",
            str(arguments.raw_stack),
            "--bucket",
            str(arguments.bucket),
        )
        # P0-P1 invariants (I), in their SHADOW form.
        _, mode = _operator(target, arguments, "show-mode")
        invariants: dict[str, object] = {
            "mode": mode,
            "table_invariants": _p1_table_invariants(session, table, str(arguments.store_stack)),
            "sole_writer": _p1_sole_writer(session, target.environment),
            "refusals": _p1_refusal_invariants(target, arguments, admit, table, session),
            "shadow_graphs": _baseline_graphs(
                session, table, parse_baseline_graphs(arguments.baseline_graph)
            ),
        }
        evidence.document["invariants"] = invariants
        # Items committed before the stream existed are projected by the operator rebuild.
        evidence.document["projection_rebuild"] = _graph_projection_rebuild(target, worktree)
        reader = _persona_api(session, target.environment, str(arguments.status_persona))
        reference = {"manifest": {"key": key, "version_id": version}}
        census_before = _table_census(session, table)
        status, full = _explain(reader, {**reference, "limit": 25})
        head = session.client("s3").head_object(Bucket=arguments.bucket, Key=key, VersionId=version)
        last_modified = cast("datetime", head["LastModified"]).astimezone(UTC).isoformat()
        traversal = _traverse(reader, reference, 1)
        oversize_status, oversize = _explain(reader, {**reference, "cursor": "A" * 2049})
        invalid_start = datetime.now(UTC) - timedelta(seconds=5)
        invalid_status, invalid = _explain(reader, {"flow_id": "not-a-uuid"})
        census_after = _table_census(session, table)
        metric = _error_metric_count(
            session, target.environment, invalid_start, int(arguments.wait_seconds)
        )
        graphs = cast("list[dict[str, object]]", full.get("graphs", []))
        explained = explanation_invariants(
            ExplanationRead(
                full,
                traversal,
                census_before.get("RECEIPT", 0),
                p1_request_ids(p1),
                last_modified,
            )
        )
        evidence.document["explanation"] = {
            "status": status,
            "document": full,
            "manifest_last_modified": last_modified,
        }
        evidence.document["traversal"] = traversal
        evidence.document["oversize"] = {"status": oversize_status, "body": oversize}
        evidence.document["invalid"] = {"status": invalid_status, "body": invalid}
        evidence.document["error_metric"] = metric
        evidence.document["census_before"] = census_before
        evidence.document["census_after"] = census_after
        evidence.document["read_role_iam"] = _explanation_role_iam(
            session, target.environment, table_arn
        )
        baseline = set(parse_baseline_graphs(arguments.baseline_graph))
        checks: dict[str, bool] = {
            "preflight": ready,
            "diff_headers_name_the_deployed_sha": bool(headers["passed"]),
            "mode_shadow": mode.get("mode") == "SHADOW",
            "table_invariants": bool(
                cast("dict[str, object]", invariants["table_invariants"])["passed"]
            ),
            "sole_writer": bool(cast("dict[str, object]", invariants["sole_writer"])["passed"]),
            "p1_refusals_still_refuse_and_write_nothing": bool(
                cast("dict[str, object]", invariants["refusals"])["passed"]
            ),
            "shadow_graphs_byte_identical": bool(
                cast("dict[str, object]", invariants["shadow_graphs"])["passed"]
            ),
            "explanation_200": status == 200,
            "every_shadow_graph_labelled_epoch_0": {str(g["graph_id"]) for g in graphs} == baseline
            and all(g["authority_epoch"] == 0 and g["label"] == "shadow" for g in graphs),
            "receipts_cover_the_root": explained["receipts_cover_the_root"],
            "root_received_elapsed_0": explained["root_received_elapsed_0"],
            "incomplete_not_success": full.get("complete") is False,
            "two_page_traversal_no_duplicate_or_omission": explained[
                "two_page_traversal_no_duplicate_or_omission"
            ],
            "retry_cursor_byte_identical": bool(traversal["retry_cursor_identical"]),
            "oversize_cursor_capacity_limited": (oversize_status, oversize)
            == (413, {"error": "CURSOR_CAPACITY_LIMITED"}),
            "invalid_reference_400": (invalid_status, invalid)
            == (400, {"error": "invalid_reference"}),
            "invalid_request_one_error_metric": metric["settled_count"] == 1,
            "zero_graph_writes": census_before == census_after,
            "read_role_has_no_graph_write": bool(
                cast("dict[str, object]", evidence.document["read_role_iam"])["passed"]
            ),
        }
        # S1 carry-forward M-1: the deployed run-request route, live, then the stream projector.
        if arguments.requester_persona:
            requester = _persona_api(session, target.environment, str(arguments.requester_persona))
            outsider = _persona_api(session, target.environment, str(arguments.non_member_persona))
            request_id = f"p2-route-{evidence.started_at.lower()}"
            body = {
                "request_id": request_id,
                "protocol_version": "v1",
                "reason": "operator_rerun",
                "manifest_key": key,
                "version_id": version,
            }
            receipts_before = _receipt_census(session, table)
            denied_status, denied = _run_request(outsider, "POST", _RUN_REQUESTS, body)
            receipts_after_denied = _receipt_census(session, table)
            posted_status, posted = _run_request(requester, "POST", _RUN_REQUESTS, body)
            shown_status, shown = _run_request(
                requester, "GET", f"{_RUN_REQUESTS}/{request_id}", None
            )
            receipts_after = _receipt_census(session, table)
            receipt = cast("dict[str, object]", posted.get("receipt") or {})
            streamed: list[str] = []
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline:
                _, again = _explain(reader, {**reference, "limit": 25})
                streamed = [
                    str(row["request_id"])
                    for row in cast("list[dict[str, object]]", again.get("receipts", []))
                ]
                if request_id in streamed:
                    break
                time.sleep(15)
            # BF-5's third effect: lifecycle stays available while explanations page at twice
            # the explanation function's reserved concurrency.
            concurrency = _status_concurrency(session, target.environment)
            bound = concurrency["live"]
            workers = 2 * int(cast("dict[str, int]", bound).get("OperationalStatusFunction") or 1)
            load_request = f"p2-load-{evidence.started_at.lower()}"
            load_before = _table_census(session, table)
            load = _lifecycle_under_explanation_load(
                reader, requester, reference, {**body, "request_id": load_request}, workers
            )
            load_after = _table_census(session, table)
            load_receipt = cast(
                "dict[str, object]",
                cast("dict[str, object]", load["lifecycle"]).get("receipt") or {},
            )
            explanations = cast("dict[str, object]", load["explanations"])
            evidence.document["lifecycle_under_load"] = {
                "concurrency": concurrency,
                "request_id": load_request,
                "census_before": load_before,
                "census_after": load_after,
                **load,
            }
            evidence.document["route"] = {
                "request_id": request_id,
                "non_member": {"status": denied_status, "body": denied},
                "post": {"status": posted_status, "body": posted},
                "get": {"status": shown_status, "body": shown},
                "receipts_before": receipts_before,
                "receipts_after_denied": receipts_after_denied,
                "receipts_after": receipts_after,
                "projected_request_ids": streamed,
            }
            checks.update(
                {
                    "route_non_member_403_writes_nothing": denied_status == 403
                    and receipts_after_denied == receipts_before,
                    "route_post_joins_the_shadow_graph": posted_status == 200
                    and (
                        receipt.get("decision"),
                        receipt.get("row"),
                        receipt.get("authority_epoch"),
                    )
                    == ("JOINED", 9, 0)
                    and str(receipt.get("graph_id")) in baseline,
                    "route_get_returns_it": shown_status == 200
                    and shown.get("receipt") == posted.get("receipt"),
                    "route_wrote_one_receipt": receipts_after == receipts_before + 1,
                    "stream_projected_the_new_receipt": request_id in streamed,
                    "explanation_concurrency_bounded": bool(concurrency["passed"]),
                    "lifecycle_commits_under_explanation_load": cast(
                        "dict[str, object]", load["lifecycle"]
                    )["status"]
                    == 200
                    and (
                        load_receipt.get("decision"),
                        load_receipt.get("row"),
                        load_receipt.get("authority_epoch"),
                    )
                    == ("JOINED", 9, 0)
                    and int(cast("int", explanations["overlapping_ok"])) >= 1,
                    "explanation_load_sheds_only_by_throttle": explanations["unexpected"] == [],
                    "explanation_load_writes_no_graph_item": load_after
                    == {**load_before, "RECEIPT": load_before.get("RECEIPT", 0) + 1},
                }
            )
        evidence.document["checks"] = checks
        evidence.document["passed"] = all(checks.values())
    except Exception as error:
        evidence.document["passed"] = False
        evidence.document["error"] = f"{type(error).__name__}: {error}"[:2000]
    path = evidence.write(target)
    print(f"evidence: {path}")
    return 0 if evidence.document.get("passed") else 1


def provision_persona(session: Any, environment: str, label: str, group: str) -> dict[str, object]:
    """Create or reuse one synthetic dev persona and add it to exactly one named group.

    The same reserved non-deliverable username and SecureString credential path as the quality
    review personas (`provision_quality_review_test_users.py`); no other persona or group changes.
    """
    import secrets
    import string

    from botocore.exceptions import ClientError

    if environment != "dev":
        raise ProbeError("synthetic personas are provisioned in dev only")
    personas = _personas()
    ssm = session.client("ssm")
    cognito = session.client("cognito-idp")
    pool = str(
        ssm.get_parameter(Name=f"/adlytics/{environment}/auth/user-pool-id")["Parameter"]["Value"]
    )
    username = str(personas.persona_username(label))
    name = str(personas.persona_parameter_name(environment, label))
    created_credential = False
    try:
        stored = json.loads(ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"])
        password = str(stored["password"])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ParameterNotFound":
            raise
        alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
        # pragma: allowlist nextline secret (A-SECRET-PLACEHOLDER-001)
        password = "Aa1!" + "".join(secrets.choice(alphabet) for _ in range(28))
        ssm.put_parameter(
            Name=name,
            Type="SecureString",
            Value=json.dumps(
                {"username": username, "password": password, "state": "permanent"},
                separators=(",", ":"),
                sort_keys=True,
            ),
            KeyId="alias/aws/ssm",
            Overwrite=False,
        )
        created_credential = True
    created_user = False
    try:
        cognito.admin_get_user(UserPoolId=pool, Username=username)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "UserNotFoundException":
            raise
        cognito.admin_create_user(
            UserPoolId=pool,
            Username=username,
            UserAttributes=[
                {"Name": "email", "Value": username},
                {"Name": "email_verified", "Value": "true"},
            ],
            TemporaryPassword=password,
            MessageAction="SUPPRESS",
        )
        created_user = True
    cognito.admin_set_user_password(
        UserPoolId=pool, Username=username, Password=password, Permanent=True
    )
    cognito.admin_add_user_to_group(UserPoolId=pool, Username=username, GroupName=group)
    groups = sorted(
        str(item["GroupName"])
        for item in cognito.admin_list_groups_for_user(UserPoolId=pool, Username=username)["Groups"]
    )
    return {
        "label": label,
        "username": username,
        "group": group,
        "groups": groups,
        "created_user": created_user,
        "created_credential": created_credential,
        "passed": groups == [group],
    }


# --- S6 part 2 (plan B8): the pre-CAS readbacks and P6(e), written and unit-tested, not run ---
#
# Each step is a pure verdict over what it reads, plus a thin read helper. None runs before the
# owner's approvals and the dark deploy; their live results are PENDING (design §4, "Before the
# CAS" and "Live acceptance").

UNSTAMPED_CURRENT_FILTER: Final = (
    "#record = :current AND attribute_not_exists(projected_publication_id)"
)
"""AGG4-3/D25: a CURRENT item without `projected_*`, which 4a's readers refuse."""
HEAD_VECTOR_CHANGED: Final = "head vector changed"
"""The reader's refusal text after its three attempts (`semantic_state.py`, slice 4a)."""
NEW_POINTER_FIELDS: Final = ("grant_id", "serving_generation", "written_at")
"""D28(a): the grant-bearing pointer fields; a legacy bake writes none of them."""
STALE_FOOTNOTE: Final = "latest published coverage"
# A shown build time is "built" followed by an instant: the GRAPH forms "built {built_at}" and
# "built before {built_before}" (`TemporalControl.tsx`, `formatIsoInstant`). The static sentence
# "...the published coverage this page was built from, never to today." is not one.
BUILD_TIME_SHOWN: Final = re.compile(r"\bbuilt\s+(?:before\s+)?\d", re.IGNORECASE)
READER_SURFACES: Final = ("page", "chat", "probe")
"""AGG4-6/AGG4-18: the three reads whose pre-deploy result the readback compares."""
EXPECTED_DLQ_VISIBLE: Final = {"delivery": 1, "semantic-write": 0, "bake": 0, "precompute": 0}
"""§2.12: the delivery DLQ holds one pre-existing message (S7 owns it); the rest are empty."""
DAILY_BURST_CEILING_SECONDS: Final = 7200
"""`protocol_ceiling_seconds` (`execution-graphs.yaml`); the probe unit test pins the two equal."""
PUBLICATIONS_RECORD_KEY: Final = "record_key"
PUBLICATIONS_CURRENT: Final = "CURRENT"
"""`semantic_state.PUBLICATION_RECORD_KEY` and `CURRENT_RECORD`, pinned by the unit test."""
SWEEP_SECONDS: Final = 60
"""One release sweep: the completion after the binding (`ingestion-stage1.ts`, row 9)."""


def unstamped_current_count(dynamodb: Any, table: str) -> dict[str, object]:
    """Count unstamped CURRENT items with a read-only `Scan`, `Select=COUNT`, every page."""
    request: dict[str, Any] = {
        "TableName": table,
        "Select": "COUNT",
        "FilterExpression": UNSTAMPED_CURRENT_FILTER,
        "ExpressionAttributeNames": {"#record": PUBLICATIONS_RECORD_KEY},
        "ExpressionAttributeValues": {":current": {"S": PUBLICATIONS_CURRENT}},
        "ConsistentRead": True,
    }
    count = scanned = pages = 0
    while True:
        page = dynamodb.scan(**request)
        count += int(page.get("Count", 0))
        scanned += int(page.get("ScannedCount", 0))
        pages += 1
        start = page.get("LastEvaluatedKey")
        if start is None:
            return {"table": table, "count": count, "scanned": scanned, "pages": pages}
        request["ExclusiveStartKey"] = start


def zero_unstamped_verdict(read: Mapping[str, object]) -> dict[str, object]:
    """Pass only at zero: a non-zero count is a stop, and stamping is a reviewed write (AGG4-3)."""
    return {**read, "passed": read.get("count") == 0}


@dataclass(frozen=True, slots=True)
class ReaderRead:
    """One reader's result for one window: the canonical digest and the IDs it resolved."""

    surface: str
    window: str
    read_at: str
    digest: str
    publication_ids: tuple[str, ...]


def reader_read(
    surface: str, window: str, body: object, *, publication_ids: Sequence[str], read_at: str
) -> ReaderRead:
    """Record one read: the canonical JSON digest of its body, and its resolved publications."""
    if surface not in READER_SURFACES:
        raise ProbeError(f"unknown reader surface {surface!r}")
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return ReaderRead(surface, window, read_at, digest, tuple(sorted(publication_ids)))


def probe_reader_read(
    reader: Any, partitions: Sequence[Any], *, window: str, read_at: str
) -> ReaderRead:
    """Read the probe surface: each partition through `DynamoCurrentPublications`."""
    resolved: dict[str, str | None] = {}
    for partition in partitions:
        found = reader.current(partition)
        resolved[str(partition.identity)] = None if found is None else found.publication_id.value
    ids = [value for value in resolved.values() if value is not None]
    return reader_read("probe", window, resolved, publication_ids=ids, read_at=read_at)


def head_vector_error_query() -> str:
    """Return the Logs Insights query counting the reader's refusals (expected 0)."""
    return f"filter @message like /{HEAD_VECTOR_CHANGED}/ | stats count(*) as refusals"


def reader_readback_verdict(
    baseline: Sequence[ReaderRead], readback: Sequence[ReaderRead], head_vector_errors: int
) -> dict[str, object]:
    """Each surface returns its pre-deploy result for the same unchanged window (AGG4-18)."""
    before = {read.surface: read for read in baseline}
    after = {read.surface: read for read in readback}
    surfaces: dict[str, dict[str, object]] = {}
    for surface in READER_SURFACES:
        old, new = before.get(surface), after.get(surface)
        same = (
            old is not None
            and new is not None
            and old.window == new.window
            and old.digest == new.digest
            and old.publication_ids == new.publication_ids
        )
        surfaces[surface] = {
            "window": None if new is None else new.window,
            "baseline_digest": None if old is None else old.digest,
            "readback_digest": None if new is None else new.digest,
            "equal": same,
        }
    passed = all(entry["equal"] for entry in surfaces.values()) and head_vector_errors == 0
    return {"surfaces": surfaces, "head_vector_errors": head_vector_errors, "passed": passed}


def legacy_pointer_verdict(
    pointer_items: Sequence[Mapping[str, object]],
    cutover_drops: int,
    reports: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Judge the next legacy bake after the Publisher deploy (slice 5, AGG4-6, D26, D28(a)).

    Its pointer items read back as today's envelope (no grant-bearing field),
    `LEGACY_BAKE_AFTER_CUTOVER` stays 0, and an unchanged page reports `unchanged`.
    """
    extra = sorted({name for item in pointer_items for name in NEW_POINTER_FIELDS if name in item})
    results = [str(report.get("result")) for report in reports]
    passed = (
        bool(pointer_items)
        and not extra
        and cutover_drops == 0
        and "unavailable" not in results
        and "unchanged" in results
    )
    return {
        "items": len(pointer_items),
        "new_fields": extra,
        "legacy_bake_after_cutover": cutover_drops,
        "results": results,
        "passed": passed,
    }


def footnote_verdict(
    page_text: str, asset_texts: Sequence[str], window_end: str
) -> dict[str, object]:
    """SHADOW footnote: "Data as of {window end}." with no build time, and no stale copy."""
    expected = f"Data as of {window_end}."
    texts = [page_text, *asset_texts]
    stale = [index for index, text in enumerate(texts) if STALE_FOOTNOTE in text]
    built = BUILD_TIME_SHOWN.search(page_text) is not None
    passed = expected in page_text and not built and not stale
    return {
        "expected": expected,
        "build_time_shown": built,
        "stale_copy_in": stale,
        "passed": passed,
    }


_MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_WHOLE_SECOND_INSTANT: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})Z$")


def _format_iso_date(value: str) -> str:
    """Mirror of `formatIsoDate` (`frontend/lib/exact-format.ts`): `Mon D, YYYY`."""
    year, month, day = value.split("-")
    return f"{_MONTHS[int(month) - 1]} {int(day)}, {year}"


_GOVERNED_REFUSALS: Final = {"domain": 409, "validation": 400}
"""The presentation handler's refusal classes and statuses (`_error_status`, `handler.py`)."""
_UNAUTHORIZED: Final = 401


def presentation_error_reason(error: Mapping[str, object]) -> str:
    """Name one presentation response >= 400 the way the handler's own taxonomy does (G2).

    `presentation_refused` is a governed refusal: a DOMAIN (409) or VALIDATION (400) error, which
    the handler records as outcome `refused`; the grant-serving check (ADLY-PRESENTATION-0002,
    counted by `PresentationServingRefused`) is one, but the body alone can't tell it from the
    other domain refusals. `presentation_unauthorized` is a 401 (the sign-in, not the page).
    Anything else is `presentation_failed`: an INVARIANT 500 (e.g. ADLY-PAGE_CONTEXT-0004,
    malformed artifact bytes) or a DEPENDENCY 503, both recorded `failed` by the handler and
    counted by the errors-by-operation alarm, or a body with no handler class (an edge or gateway
    error) or a class that contradicts its status.
    """
    status = error.get("status")
    if status == _UNAUTHORIZED:
        return "presentation_unauthorized"
    error_class = error.get("error_class")
    if isinstance(error_class, str) and _GOVERNED_REFUSALS.get(error_class) == status:
        return "presentation_refused"
    return "presentation_failed"


def graph_footnote_verdict(
    readback: Mapping[str, object], asset_texts: Sequence[str], page_text: str, *, not_before: str
) -> dict[str, object]:
    """HIST-8, GRAPH form: the persona sees the served window and the granted build time.

    Protects a user-visible behaviour: a reader is told how fresh the page is. The served V2
    page must carry a grant-bearing `generation.built_at` (a grant-less `built_before` or no
    generation is not this bake's), and the rendered footnote must be exactly
    "Data as of {window end}, built {built_at}." (`TemporalControl.tsx`) with no stale copy,
    read no earlier than the bake's final `bake.finalize`. A missing, stale or mismatched
    footnote is NOT_PROVEN, and so is a presentation response >= 400 (named by
    `presentation_error_reason`) or a render that never produced a readback (`render_failed`).
    """
    reasons: list[str] = []
    if readback.get("render_failed") is not None:
        reasons.append("render_failed")
    error = readback.get("presentation_error")
    if error is not None:
        # A malformed error record is unclassified, so it reads as a failure, never a refusal.
        recorded: Mapping[str, object] = (
            cast("Mapping[str, object]", error) if isinstance(error, Mapping) else {}
        )
        reasons.append(presentation_error_reason(recorded))
    raw = readback.get("generation")
    generation: Mapping[str, object] = (
        cast("Mapping[str, object]", raw) if isinstance(raw, Mapping) else {}
    )
    built_at = generation.get("built_at")
    instant = _WHOLE_SECOND_INSTANT.match(built_at) if isinstance(built_at, str) else None
    if instant is None or len(generation) != 1:
        reasons.append("generation_not_granted")
    window_end = readback.get("window_end_iso")
    expected: str | None = None
    if not isinstance(window_end, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", window_end) is None:
        reasons.append("window_end_absent")
    elif instant is not None:
        expected = (
            f"Data as of {_format_iso_date(window_end)}, "
            f"built {instant.group(1)} {instant.group(2)} UTC."
        )
    if expected is None or expected not in page_text:
        reasons.append("footnote_missing_or_stale")
    if any(STALE_FOOTNOTE in text for text in (page_text, *asset_texts)):
        reasons.append("stale_copy")
    try:
        read_at = utc_instant(str(readback.get("read_at")), "read_at")
        if read_at < utc_instant(not_before, "not_before"):
            reasons.append("read_before_bake_finalize")
    except ProbeError:
        reasons.append("read_at_invalid")
    return {
        "expected": expected,
        "reasons": reasons,
        "passed": not reasons,
        "disposition": "PASS" if not reasons else "NOT_PROVEN",
    }


def run_span_query(
    environment: str, run_id: str, *, window_start: datetime, window_end: datetime
) -> Any:
    """P6(e) through 5a: the chain's four graph spans of one run, by its declared `run_id`."""
    from adapters.jobs.span_retrieval import build_span_query, declared_span_attributes

    return build_span_query(
        environment=environment,
        window_start=window_start,
        window_end=window_end,
        declared_attributes=declared_span_attributes(),
        keys=(("run_id", run_id),),
        span_names=GRAPH_CHAIN_SPANS,
        limit=1000,
        max_pages=8,
    )


GRAPH_UNIT_BATCH_SPAN: Final = "ingest.table_writer.graph_unit_batch"
GRAPH_UNIT_SPAN: Final = "ingest.table_writer.graph_unit"
GRAPH_CHAIN_SPANS: Final = (
    "ingest.graph_materialize",
    GRAPH_UNIT_SPAN,
    "ingest.graph_publish",
    "graph.post_grant_consume",
    GRAPH_UNIT_BATCH_SPAN,
)
"""The graph-native spans (§2.10), as `spans.yaml` names them; slice 7 adds the batch span."""


def span_verdict(
    result: Mapping[str, object],
    units: int,
    *,
    batch_ids: Sequence[str] = (),
    batch_spans: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """One span per node, and one batch span per distinct `batch_id` (slice 7, conversion 9).

    The batch span carries no `run_id`, so a run's retrieval holds none of it; its count is the
    spans each target batch's own retrieval by `batch_id` returned (FEAT-009 H1), never the
    number of target batches. `pending` is never a pass, and a truncated read is not a
    complete count.
    """
    spans = cast("Sequence[Mapping[str, object]]", result.get("spans", ()))
    retrievals = [(batch_spans or {}).get(batch_id) or {} for batch_id in batch_ids]
    counts = dict.fromkeys(GRAPH_CHAIN_SPANS, 0)
    for span in [
        *spans,
        *(
            span
            for retrieved in retrievals
            for span in cast("Sequence[Mapping[str, object]]", retrieved.get("spans", ()))
        ),
    ]:
        name = str(span.get("name"))
        if name in counts:
            counts[name] += 1
    expected = dict(zip(GRAPH_CHAIN_SPANS, (1, units, 1, 2, len(batch_ids)), strict=True))
    passed = (
        all(
            read.get("status") == "complete" and not read.get("truncated")
            for read in (result, *retrievals)
        )
        and counts == expected
    )
    return {
        "status": result.get("status"),
        "counts": counts,
        "expected": expected,
        "passed": passed,
    }


def measurement_coverage(rows: Sequence[Any], present: Callable[[str], bool]) -> dict[str, object]:
    """One `FlowTransitionLatency` per armed node, one `ActivityAdmissionElapsed` per claim.

    The expected identities are derived from the projected NODE and CLAIM rows exactly as the
    projector derives them (`row_measurement`); `present` reads `MEASUREMENT#<id>` strongly.

    P6E-7 (design §2.10): latency is `settled_at - armed_at`, so it is owed by every armed node.
    `grant_apply` is the one node never armed (release settles it inline and never dispatches
    it), so a `grant_apply` row without `armed_at` is `unarmed_by_design` and leaves the
    denominator. Every other node without a measurement, armed or not, is `unmeasured` and fails.
    """
    from adapters.outbound.dynamodb.operational_status import row_measurement
    from domain.operations.execution_graph_release import GRANT_APPLY_NODE
    from domain.operations.graph_projection import ProjectionKind

    expected: dict[str, list[str]] = {"FlowTransitionLatency": [], "ActivityAdmissionElapsed": []}
    missing: list[str] = []
    unmeasured: list[str] = []
    unarmed_by_design: list[str] = []
    for row in rows:
        if row.kind not in {ProjectionKind.NODE, ProjectionKind.CLAIM}:
            continue
        if (
            row.kind is ProjectionKind.NODE
            and row.document.get("node") == GRANT_APPLY_NODE
            and row.document.get("armed_at") is None
        ):
            unarmed_by_design.append(f"{row.authority_pk}/{row.authority_sk}")
            continue
        contribution = row_measurement(row)
        if contribution is None:
            unmeasured.append(f"{row.authority_pk}/{row.authority_sk}")
            continue
        expected[contribution.family].append(contribution.measurement_id)
        if not present(contribution.measurement_id):
            missing.append(contribution.measurement_id)
    nodes = sum(1 for row in rows if row.kind is ProjectionKind.NODE)
    armed = nodes - len(unarmed_by_design)
    claims = sum(1 for row in rows if row.kind is ProjectionKind.CLAIM)
    passed = (
        not missing
        and not unmeasured
        and len(expected["FlowTransitionLatency"]) == armed > 0
        and len(expected["ActivityAdmissionElapsed"]) == claims > 0
    )
    return {
        "terminal_nodes": nodes,
        "armed_nodes": armed,
        "unarmed_by_design": unarmed_by_design,
        "claims": claims,
        "expected": {family: len(ids) for family, ids in expected.items()},
        "missing": missing,
        "unmeasured": unmeasured,
        "passed": passed,
    }


def explanation_materialized_verdict(document: Mapping[str, object]) -> dict[str, object]:
    """§2.10: the explanation shows `materialized` APPLIED and no longer lists it missing."""
    stages = cast("Sequence[Mapping[str, object]]", document.get("stages", ()))
    statuses = [str(row.get("status")) for row in stages if row.get("stage") == "materialized"]
    missing = cast("Sequence[object]", document.get("missing_required_stages", ()))
    passed = "APPLIED" in statuses and "materialized" not in missing
    return {"statuses": statuses, "missing": list(missing), "passed": passed}


def dlq_verdict(status: Mapping[str, object]) -> dict[str, object]:
    """§2.12: delivery 1 and the others 0 visible, none in flight, no active move; else a stop.

    The visible count alone would pass a queue still holding an in-flight dead letter or one
    mid-redrive, so every route must also read zero in flight and a terminal (or no) move task.
    """
    from adapters.jobs.execution_graph.dead_letters import ACTIVE_MOVE_STATUSES

    queues = cast("Mapping[str, Mapping[str, object]]", status.get("queues", {}))
    visible = {name: queues.get(name, {}).get("visible") for name in EXPECTED_DLQ_VISIBLE}
    in_flight = {name: queues.get(name, {}).get("in_flight") for name in EXPECTED_DLQ_VISIBLE}
    active_moves = sorted(
        name
        for name in EXPECTED_DLQ_VISIBLE
        if (task := queues.get(name, {}).get("latest_move_task")) is not None
        and cast("Mapping[str, object]", task).get("status") in ACTIVE_MOVE_STATUSES
    )
    passed = (
        visible == EXPECTED_DLQ_VISIBLE
        and all(count == 0 for count in in_flight.values())
        and not active_moves
    )
    return {
        "visible": visible,
        "expected": EXPECTED_DLQ_VISIBLE,
        "in_flight": in_flight,
        "active_moves": active_moves,
        "passed": passed,
    }


def flip_order_verdict(flipped_at: str, settled_at: str) -> dict[str, object]:
    """§2.6: the pointer flips inside `publish`, before the node settles."""
    flipped, settled = datetime.fromisoformat(flipped_at), datetime.fromisoformat(settled_at)
    return {
        "flipped_at": flipped_at,
        "settled_at": settled_at,
        "lead_seconds": (settled - flipped).total_seconds(),
        "passed": flipped <= settled,
    }


def serving_duration_seconds(publish_settled_at: str, bound_at: str) -> dict[str, object]:
    """AGG4-5: `publish`'s settlement to the binding, and to completion within one sweep.

    The run completes on the release sweep after its binding, so the completion bound is an
    inference (binding plus one sweep), labelled as one.
    """
    measured = (
        datetime.fromisoformat(bound_at) - datetime.fromisoformat(publish_settled_at)
    ).total_seconds()
    return {
        "to_binding_seconds": measured,
        "to_completion_upper_bound_seconds": measured + SWEEP_SECONDS,
        "inferred": "completion = binding + at most one release sweep",
    }


def serving_chronology_verdict(publish_settled_at: str, bound_at: str) -> dict[str, object]:
    """AGG4-5: the binding cannot precede `publish`'s settlement.

    A negative interval is malformed evidence, and fed to the burst bound it would lower the
    inferred burst, so it fails P6(e) here and is never clamped.
    """
    settled, bound = datetime.fromisoformat(publish_settled_at), datetime.fromisoformat(bound_at)
    return {
        "publish_settled_at": publish_settled_at,
        "bound_at": bound_at,
        "passed": settled <= bound,
    }


def inferred_serial_bound(
    windows_per_cycle: int, serving_seconds: float, pre_grant_seconds: float
) -> dict[str, object]:
    """Return the pre-coalescing serial model as informational evidence only.

    This retained estimate can neither pass nor fail P6E-3; the observed named cohort decides.
    """
    burst = windows_per_cycle * serving_seconds + pre_grant_seconds
    return {
        "windows_per_cycle": windows_per_cycle,
        "burst_seconds": burst,
        "ceiling_seconds": DAILY_BURST_CEILING_SECONDS,
        "inferred": True,
        "informational_only": True,
    }


@dataclass(frozen=True, slots=True)
class ObservedCohortRun:
    """One named run's strongly read terminal, publication, and serving-binding evidence (D7)."""

    run_id: str
    root_flow_id: str
    cohort_revision: str
    binding_id: str
    winner_run_id: str
    publication_effect_digest: str
    pointer_generation: str
    root_clock_started_at: str
    publish_flipped_at: str
    publish_settled_at: str
    terminal_at: str
    state: str
    serving_outcome: str | None
    binding_kind: str
    execution_attempts: int
    contributor_roots: tuple[str, ...] = ()
    direct_binding_id: str | None = None
    """Required only for an alias: the direct binding it names."""


@dataclass(frozen=True, slots=True)
class ObservedBakeChain:
    """The terminal `bake.run` and `bake.finalize` outcomes of one cohort's winner."""

    cohort_revision: str
    run_id: str
    bake_run_outcomes: tuple[str, ...]
    bake_finalize_outcomes: tuple[str, ...]
    finalize_ended_at: str | None = None
    """The latest successful `bake.finalize` span's end, from the retrieval's own instants."""


@dataclass(frozen=True, slots=True)
class ObservedPromoterTransition:
    """The immutable promoter transition that drained one cohort."""

    drained_cohort_revision: str
    elected_cohort_revision: str | None
    aws_request_id: str
    drained_at: str
    outcome: str | None = None
    """`ELECTED` (naming the elected cohort) or `NO_ELIGIBLE` (naming none)."""


@dataclass(frozen=True, slots=True)
class ObservedHeadRead:
    """One strong read of a serving domain's head, taken after every observed finalize."""

    domain: str
    state: str | None
    cohort_revision: str | None
    read_at: str


@dataclass(frozen=True, slots=True)
class ObservedCohortEvidence:
    """The named runs plus each cohort's bake chain and promoter transition."""

    runs: tuple[ObservedCohortRun, ...]
    bakes: tuple[ObservedBakeChain, ...] = ()
    transitions: tuple[ObservedPromoterTransition, ...] = ()
    heads: tuple[ObservedHeadRead, ...] = ()
    cohort_domains: tuple[tuple[str, str], ...] = ()
    """Each observed cohort's serving domain, from its binding."""


BAKE_RUN_SUCCEEDED: Final = "fanned_out"
"""The GRAPH `bake.run` span's success outcome (`graph_bake.py`, `_fan_out`); `completed` is the
retired legacy bake's, which x5's live retrieval showed GRAPH never emits."""
BAKE_FINALIZE_SUCCEEDED: Final = "graph_bake_finalized"
BAKE_FINALIZE_ARMED: Final = "graph_bake_precompute_armed"
"""The finalize node's first span: it arms precompute, then finalizes after the contribution."""
_OBSERVED_RUN_TEXT: Final = (
    "run_id",
    "root_flow_id",
    "cohort_revision",
    "binding_id",
    "winner_run_id",
    "publication_effect_digest",
    "pointer_generation",
    "root_clock_started_at",
    "publish_flipped_at",
    "publish_settled_at",
    "terminal_at",
)


def utc_instant(value: object, what: str) -> datetime:
    """Parse one timezone-aware UTC ISO instant; anything else is a `ProbeError`."""
    if not isinstance(value, str) or not value:
        raise ProbeError(f"{what} is not an ISO instant")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ProbeError(f"{what} is not an ISO instant") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ProbeError(f"{what} is not a timezone-aware UTC instant")
    return parsed


def _validated_observed_runs(evidence: ObservedCohortEvidence) -> None:
    if not evidence.runs:
        raise ProbeError("the observed cohort names no runs")
    ids = [run.run_id for run in evidence.runs]
    if len(set(ids)) != len(ids):
        raise ProbeError("the observed cohort names a run twice")
    for run in evidence.runs:
        for name in _OBSERVED_RUN_TEXT:
            value = getattr(run, name)
            if not isinstance(value, str) or not value.strip():
                raise ProbeError(f"run {run.run_id or '?'} carries no {name}")
        for name in ("root_clock_started_at", "publish_flipped_at", "publish_settled_at"):
            utc_instant(getattr(run, name), f"run {run.run_id} {name}")
        utc_instant(run.terminal_at, f"run {run.run_id} terminal_at")
        if run.binding_kind not in {"alias", "direct"}:
            raise ProbeError(f"run {run.run_id} has neither an alias nor direct binding")
        if run.binding_kind == "alias" and not run.direct_binding_id:
            raise ProbeError(f"alias run {run.run_id} names no direct binding")
        attempts = run.execution_attempts
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ProbeError(f"run {run.run_id} carries invalid execution_attempts")


def _cohort_failures(
    cohort: str,
    group: Sequence[ObservedCohortRun],
    bakes: Sequence[ObservedBakeChain],
    transitions: Sequence[ObservedPromoterTransition],
    head: ObservedHeadRead | None = None,
) -> list[str]:
    """One cohort: one direct, exact aliases and contributors, one bake chain, its drain.

    Acceptance criterion 4 places the promoter transition in the one fixed-member
    `drain_final_alias_and_activate_next` transaction that settles a cohort's last alias, and
    criterion 5 requires exact loser aliases, which are zero for a single root. The runtime writes
    `SERVING_TRANSITION#` only in that drain (`DynamoServingReleaseStore`,
    `drain_final_alias_and_activate_next`). So a cohort with at least one alias needs exactly one
    transition whose outcome matches what it names (`ELECTED` naming another observed cohort, or
    `NO_ELIGIBLE` naming none); a direct-only cohort needs exactly zero, its one successful
    finalize, and a strong head read after that finalize showing it is no longer ACTIVE.
    """
    failures: list[str] = []
    directs = [run for run in group if run.binding_kind == "direct"]
    if len(directs) != 1:
        return [f"{cohort}: {len(directs)} direct bindings, expected exactly 1"]
    direct = directs[0]
    if direct.execution_attempts != 1:
        failures.append(f"{cohort}: the direct ran {direct.execution_attempts} executions")
    if direct.winner_run_id != direct.run_id:
        failures.append(f"{cohort}: the direct names another winner")
    for run in group:
        if run.binding_kind != "alias":
            continue
        if run.direct_binding_id != direct.binding_id or run.winner_run_id != direct.run_id:
            failures.append(f"{cohort}: alias {run.run_id} does not alias the direct")
        if run.execution_attempts != 0:
            failures.append(f"{cohort}: alias {run.run_id} executed")
    if set(direct.contributor_roots) != {run.root_flow_id for run in group} or len(
        direct.contributor_roots
    ) != len(set(direct.contributor_roots)):
        failures.append(f"{cohort}: contributor roots differ from the grouped roots")
    chains = [chain for chain in bakes if chain.cohort_revision == cohort]
    chain = chains[0] if len(chains) == 1 and chains[0].run_id == direct.run_id else None
    if chain is None:
        failures.append(f"{cohort}: {len(chains)} bake chains for the direct, expected exactly 1")
    else:
        if chain.bake_run_outcomes.count(BAKE_RUN_SUCCEEDED) != 1:
            failures.append(f"{cohort}: not exactly one successful bake.run")
        if chain.bake_finalize_outcomes.count(BAKE_FINALIZE_SUCCEEDED) != 1:
            failures.append(f"{cohort}: not exactly one successful bake.finalize")
    drained = [item for item in transitions if item.drained_cohort_revision == cohort]
    if any(run.binding_kind == "alias" for run in group):
        if len(drained) != 1:
            failures.append(f"{cohort}: {len(drained)} promoter transitions, expected exactly 1")
        else:
            failures.extend(_transition_outcome_failures(cohort, drained[0]))
        return failures
    if drained:
        failures.append(f"{cohort}: direct-only cohort has {len(drained)} promoter transitions")
    failures.extend(_released_failures(cohort, chain, head))
    return failures


def _transition_outcome_failures(cohort: str, item: ObservedPromoterTransition) -> list[str]:
    elected = item.elected_cohort_revision
    if item.outcome == "ELECTED" and elected and elected != cohort:
        return []
    if item.outcome == "NO_ELIGIBLE" and elected is None:
        return []
    return [f"{cohort}: transition outcome {item.outcome} does not match its election"]


def _released_failures(
    cohort: str, chain: ObservedBakeChain | None, head: ObservedHeadRead | None
) -> list[str]:
    """Require a strong head read after a direct-only cohort's finalize showing it released."""
    if chain is None or chain.finalize_ended_at is None:
        return [f"{cohort}: direct-only cohort has no successful finalize instant"]
    if head is None:
        return [f"{cohort}: direct-only cohort has no head read"]
    if utc_instant(head.read_at, "head read_at") < utc_instant(chain.finalize_ended_at, cohort):
        return [f"{cohort}: the head read precedes the cohort's finalize"]
    if head.state == "ACTIVE" and head.cohort_revision == cohort:
        return [f"{cohort}: direct-only cohort is still ACTIVE after its finalize"]
    return []


def observed_cohort_verdict(evidence: ObservedCohortEvidence) -> dict[str, object]:
    """Judge criteria 3-5 from observed named runs only; malformed evidence is refused.

    Root duration is `terminal_at - root_clock_started_at`; the cycle is the latest terminal
    minus the earliest root clock. Neither is ever taken from `bound_at`.
    """
    _validated_observed_runs(evidence)
    cohorts = sorted({run.cohort_revision for run in evidence.runs})
    for chain in evidence.bakes:
        if chain.cohort_revision not in cohorts:
            raise ProbeError(f"bake chain for unexpected cohort {chain.cohort_revision}")
    for item in evidence.transitions:
        if item.drained_cohort_revision not in cohorts:
            raise ProbeError(f"transition for unexpected cohort {item.drained_cohort_revision}")
        utc_instant(item.drained_at, f"transition {item.drained_cohort_revision} drained_at")
    failures: list[str] = []
    root_seconds: list[float] = []
    for run in evidence.runs:
        root = utc_instant(run.root_clock_started_at, run.run_id)
        terminal = utc_instant(run.terminal_at, run.run_id)
        seconds = (terminal - root).total_seconds()
        if seconds < 0:
            raise ProbeError(f"run {run.run_id} completes before its root clock")
        root_seconds.append(seconds)
        flipped = utc_instant(run.publish_flipped_at, run.run_id)
        if flipped > utc_instant(run.publish_settled_at, run.run_id):
            failures.append(f"{run.run_id}: the flip follows publish settlement")
        if run.state != "SUCCEEDED" or run.serving_outcome is None:
            failures.append(f"{run.run_id}: not terminal with a serving outcome")
    domains = dict(evidence.cohort_domains)
    heads = {head.domain: head for head in evidence.heads}
    for cohort in cohorts:
        group = [run for run in evidence.runs if run.cohort_revision == cohort]
        head = heads.get(domains.get(cohort, ""))
        failures.extend(_cohort_failures(cohort, group, evidence.bakes, evidence.transitions, head))
    cycle_seconds = (
        max(utc_instant(run.terminal_at, run.run_id) for run in evidence.runs)
        - min(utc_instant(run.root_clock_started_at, run.run_id) for run in evidence.runs)
    ).total_seconds()
    if max(root_seconds) > DAILY_BURST_CEILING_SECONDS:
        failures.append("a root exceeds the 7,200-second ceiling")
    if cycle_seconds > DAILY_BURST_CEILING_SECONDS:
        failures.append("the cycle exceeds the 7,200-second ceiling")
    return {
        "runs": len(evidence.runs),
        "cohorts": cohorts,
        "cycle_seconds": cycle_seconds,
        "max_root_seconds": max(root_seconds),
        "ceiling_seconds": DAILY_BURST_CEILING_SECONDS,
        "direct": sum(run.binding_kind == "direct" for run in evidence.runs),
        "aliases": sum(run.binding_kind == "alias" for run in evidence.runs),
        "failures": failures,
        "passed": not failures,
    }


def delivery_not_visible_verdict(maximum: float | None) -> dict[str, object]:
    """Record the in-run delivery-queue NotVisible maximum; no datapoint fails closed."""
    return {
        "namespace": "AWS/SQS",
        "metric": "ApproximateNumberOfMessagesNotVisible",
        "statistic": "Maximum",
        "period_seconds": 60,
        "maximum": maximum,
        "passed": maximum is not None and maximum >= 0,
    }


def run_batch_span_query(
    environment: str, batch_id: str, *, window_start: datetime, window_end: datetime
) -> Any:
    """Slice 7 (AR5-1): one batch's span, retrieved by its declared `batch_id`."""
    from adapters.jobs.span_retrieval import build_span_query, declared_span_attributes

    return build_span_query(
        environment=environment,
        window_start=window_start,
        window_end=window_end,
        declared_attributes=declared_span_attributes(),
        keys=(("batch_id", batch_id),),
        span_names=(GRAPH_UNIT_BATCH_SPAN,),
        limit=100,
        max_pages=2,
    )


ALLOWANCE_TRIGGER_MS: Final = 22_193
"""Probe 2's largest in-bound client wall (design §2.5 step 4, D42): a finalized set whose mean
time per charged allowance passes it is a re-derivation trigger (M1)."""
BATCH_METRICS: Final = ("GraphUnitBatchSize", "GraphUnitBatchCommitMs", "GraphUnitBatchSplits")
SET_RECORD_PHASE: Final = "graph_unit_set_executed"
SET_RECORD_FIELDS: Final = (
    "batch_id",
    "execution_set_id",
    "table",
    "statements",
    "rows",
    "execution_ms",
    "deadline_cancelled",
    "max_partitions_per_merge",
    "members",
)
"""The fields a `graph_unit_set_executed` line carries (`table_writer.py`), as the verdict reads."""
_SET_RECORD_TEXT_FIELDS: Final = ("batch_id", "execution_set_id", "table", "outcome")
_SET_RECORD_COUNT_FIELDS: Final = (
    "statements",
    "rows",
    "execution_ms",
    "max_partitions_per_merge",
    "members",
)
LOCK_TABLE_PREFIX: Final = "LOCK#TABLE#"


def _set_record_complete(record: Mapping[str, object]) -> bool:
    """Whether a set record carries every field `_record_set` writes, each of its written type.

    A record missing one, or carrying it as another type, proves no timing, outcome,
    cancellation or merge width (fix-review F1), so it can never stand in for its set.
    """
    for name in _SET_RECORD_TEXT_FIELDS:
        value = record.get(name)
        if not isinstance(value, str) or not value:
            return False
    for name in _SET_RECORD_COUNT_FIELDS:
        value = record.get(name)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
            or (name == "statements" and value == 0)
        ):
            return False
    return isinstance(record.get("deadline_cancelled"), bool)


@dataclass(frozen=True, slots=True)
class BatchingEvidence:
    """Slice 7's P6(e) batch evidence for one run (AR5-1), each part from its own read."""

    batch_ids: tuple[str, ...]
    batch_spans: Mapping[str, Mapping[str, object]]
    """The batch span retrieval of each `batch_id`."""
    expected_units: tuple[tuple[str, str], ...]
    """Each `write_unit` NODE row's `(batch_id, node_id)`: the members owed one unit span each
    (D37, FEAT-009 J1)."""
    unit_spans: Sequence[Mapping[str, object]]
    """The run's `TABLE_WRITER_GRAPH_UNIT` spans, from its retrieval by `run_id` (FEAT-009 H1);
    each must sit beneath its own target batch's one span."""
    metrics: Mapping[str, Mapping[str, Sequence[float]]]
    """Each target batch's EMF values per batch metric, attributed by the samples' `batch_id`
    property (FEAT-009 review F6); a sample from another batch can never stand in."""
    set_records: Sequence[Mapping[str, object]]
    """`graph_unit_set_executed` records' details in the window."""
    budget_records: Mapping[str, int]
    """Counts of `semantic_statement_budget` `insufficient` and `semantic_attempt_parked`."""
    table_locks: Mapping[str, Mapping[str, object] | None]
    """Each touched `LOCK#TABLE#<k>` item, strongly read."""
    explanation_stages: Sequence[Mapping[str, object]]


def _attribute(span: Mapping[str, object], key: str) -> object:
    attributes = span.get("attributes")
    if not isinstance(attributes, Mapping):
        return None
    return cast("Mapping[str, object]", attributes).get(key)


def _batch_span_failures(
    evidence: BatchingEvidence,
) -> tuple[list[str], dict[str, Mapping[str, object]]]:
    """Exactly one complete batch span per target `batch_id` (D37, FEAT-009 H1).

    A span counts for a batch only when its own `batch_id` attribute names it; none is
    `batch_span_missing`, more than one is `batch_span_repeated`. Returns each batch's one span.
    """
    failures: list[str] = []
    single: dict[str, Mapping[str, object]] = {}
    for batch_id in evidence.batch_ids:
        retrieved = evidence.batch_spans.get(batch_id) or {}
        spans = cast("Sequence[Mapping[str, object]]", retrieved.get("spans", ()))
        found = [
            span
            for span in spans
            if span.get("name") == GRAPH_UNIT_BATCH_SPAN
            and _attribute(span, "batch_id") == batch_id
        ]
        if retrieved.get("status") != "complete" or retrieved.get("truncated") or not found:
            failures.append(f"batch_span_missing:{batch_id}")
        elif len(found) > 1:
            failures.append(f"batch_span_repeated:{batch_id}")
        else:
            single[batch_id] = found[0]
    return failures, single


def _unit_span_failures(
    evidence: BatchingEvidence, single: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """Exactly one unit span per expected member, each a child of its batch's one span (D37).

    A span is bound to a member by its own `(batch_id, node_id)` attributes (FEAT-009 J1), so a
    second span for one member can never stand in for another member's absent span: a member
    with none is `unit_span_member_missing`, with more than one `unit_span_member_repeated`,
    and a target-batch span naming no expected member `unit_span_member_extra`. A batch whose
    span is missing or repeated has already failed; its units are not judged against an
    ambiguous parent.
    """
    if not evidence.unit_spans:
        return ["unit_span_missing"]
    failures: list[str] = []
    expected = dict.fromkeys(evidence.expected_units, 0)
    for unit in evidence.unit_spans:
        batch_id, span_id = _attribute(unit, "batch_id"), unit.get("spanId")
        if batch_id not in evidence.batch_ids:
            failures.append(f"unit_span_batch_foreign:{span_id}")
            continue
        parent = single.get(cast("str", batch_id))
        if parent is not None and (
            unit.get("traceId") != parent.get("traceId")
            or unit.get("parentSpanId") != parent.get("spanId")
        ):
            failures.append(f"unit_span_not_nested:{span_id}")
        identity = (cast("str", batch_id), _attribute(unit, "node_id"))
        if identity in expected:
            expected[cast("tuple[str, str]", identity)] += 1
        else:
            failures.append(f"unit_span_member_extra:{span_id}")
    for (batch_id, node_id), count in expected.items():
        if count == 0:
            failures.append(f"unit_span_member_missing:{batch_id}:{node_id}")
        elif count > 1:
            failures.append(f"unit_span_member_repeated:{batch_id}:{node_id}")
    return failures


def _explanation_batch_failures(evidence: BatchingEvidence) -> list[str]:
    """Every unit-stage explanation row names one of the run's target batches (FEAT-009 H1)."""
    unit_rows = [
        row
        for row in evidence.explanation_stages
        if row.get("stage") in {"unit_claimed", "unit_retrying", "unit_attested"}
    ]
    if not unit_rows or any(not row.get("batch_id") for row in unit_rows):
        return ["explanation_batch_missing"]
    foreign = {
        str(row.get("batch_id"))
        for row in unit_rows
        if row.get("batch_id") not in evidence.batch_ids
    }
    return [f"explanation_batch_foreign:{batch_id}" for batch_id in sorted(foreign)]


def batching_verdict(evidence: BatchingEvidence) -> dict[str, object]:  # noqa: PLR0912
    """Judge the run's batching, fail-closed (AR5-1): every check needs its own read.

    A re-derivation trigger (a budget refusal, a park, a deadline cancellation, or a set slower
    per charged allowance than 22,193 ms) fails as `allowance_rederivation_required`, never a
    pass. The largest partitions-per-merge is recorded with no expected value. A batch with any
    incomplete set record fails as `set_record_invalid`, and only complete records are judged.
    """
    failures: list[str] = []
    if not evidence.batch_ids:
        failures.append("no_batch")
    span_failures, single = _batch_span_failures(evidence)
    failures.extend(span_failures)
    for batch_id in evidence.batch_ids:
        own = [record for record in evidence.set_records if record.get("batch_id") == batch_id]
        if not own:
            failures.append(f"set_record_missing:{batch_id}")
        elif not all(_set_record_complete(record) for record in own):
            failures.append(f"set_record_invalid:{batch_id}")
    failures.extend(_unit_span_failures(evidence, single))
    for batch_id in evidence.batch_ids:
        samples = evidence.metrics.get(batch_id, {})
        for name in BATCH_METRICS:
            count = len(samples.get(name, ()))
            if count == 0:
                failures.append(f"metric_missing:{name}:{batch_id}")
            elif count > 1:
                failures.append(f"metric_repeated:{name}:{batch_id}")
        if any(value != 0 for value in samples.get("GraphUnitBatchSplits", ())):
            failures.append(f"splits_nonzero:{batch_id}")
    for lock_key, item in evidence.table_locks.items():
        if item is None or item.get("open_members") != 0:
            failures.append(f"lock_open:{lock_key}")
    failures.extend(_explanation_batch_failures(evidence))
    triggers: list[str] = []
    for name, count in evidence.budget_records.items():
        if count:
            triggers.append(f"{name}:{count}")
    widest: Mapping[str, object] | None = None
    for record in filter(_set_record_complete, evidence.set_records):
        if record.get("deadline_cancelled"):
            triggers.append(f"deadline_cancelled:{record.get('execution_set_id')}")
        statements = int(cast("int", record.get("statements", 0)))
        execution_ms = int(cast("int", record.get("execution_ms", 0)))
        if record.get("outcome") == "finalized" and statements >= 1:
            mean = execution_ms / (statements + 1)
            if mean > ALLOWANCE_TRIGGER_MS:
                triggers.append(f"slow_set:{record.get('execution_set_id')}")
        partitions = int(cast("int", record.get("max_partitions_per_merge", 0)))
        if widest is None or partitions > int(cast("int", widest["max_partitions_per_merge"])):
            widest = {
                "max_partitions_per_merge": partitions,
                "execution_set_id": record.get("execution_set_id"),
            }
    if triggers:
        failures.append("allowance_rederivation_required")
    return {
        "batches": len(evidence.batch_ids),
        "failures": failures,
        "rederivation_triggers": triggers,
        "widest_merge": widest,
        "passed": not failures,
    }


@dataclass(frozen=True, slots=True)
class P6eEvidence:
    """Everything P6(e) reads for one run, gathered by `P6eReads` (live) or a test fake."""

    spans: Mapping[str, object]
    units: int
    rows: Sequence[Any]
    measured: Callable[[str], bool]
    explanation: Mapping[str, object]
    dlq_status: Mapping[str, object]
    flipped_at: str
    publish_settled_at: str
    bound_at: str
    pre_grant_seconds: float
    windows_per_cycle: int
    run_state: str
    serving_outcome: str | None
    observed_cohort: ObservedCohortEvidence
    delivery_not_visible_maximum: float | None
    batching: BatchingEvidence | None = None
    """Slice 7 (AR5-1): None reads as a failed check, never a pass."""


def p6e_verdict(evidence: P6eEvidence) -> dict[str, object]:
    """Judge P6(e): one probe retrieval end to end, every check from its own read.

    Nothing passes without its read: a pending span lookup or a missing measurement fails.
    """
    duration = serving_duration_seconds(evidence.publish_settled_at, evidence.bound_at)
    checks = {
        "serving_chronology": serving_chronology_verdict(
            evidence.publish_settled_at, evidence.bound_at
        ),
        "spans": span_verdict(evidence.spans, evidence.units)
        if evidence.batching is None
        else span_verdict(
            evidence.spans,
            evidence.units,
            batch_ids=evidence.batching.batch_ids,
            batch_spans=evidence.batching.batch_spans,
        ),
        "batching": {"passed": False, "failures": ["batching_not_read"]}
        if evidence.batching is None
        else batching_verdict(evidence.batching),
        "measurements": measurement_coverage(evidence.rows, evidence.measured),
        "explanation": explanation_materialized_verdict(evidence.explanation),
        "dlq": dlq_verdict(evidence.dlq_status),
        "flip_order": flip_order_verdict(evidence.flipped_at, evidence.publish_settled_at),
        "observed_cohort": observed_cohort_verdict(evidence.observed_cohort),
        "delivery_not_visible": delivery_not_visible_verdict(evidence.delivery_not_visible_maximum),
        "completed": {
            "state": evidence.run_state,
            "serving_outcome": evidence.serving_outcome,
            "passed": evidence.run_state == "SUCCEEDED" and evidence.serving_outcome is not None,
        },
    }
    passed = all(bool(check["passed"]) for check in checks.values())
    serial_bound = inferred_serial_bound(
        evidence.windows_per_cycle,
        float(cast("float", duration["to_completion_upper_bound_seconds"])),
        evidence.pre_grant_seconds,
    )
    return {
        "serving_duration": duration,
        "inferred_serial_bound": serial_bound,
        "checks": checks,
        "passed": passed,
    }


class P6eReads(Protocol):
    """The strong reads P6(e) makes; `BotoP6eReads` is the live one, a fake in the unit test."""

    def run(self, run_id: str) -> Mapping[str, object]:
        """Return the decoded `RUN#<run_id>/RUN` item."""
        ...

    def nodes(self, run_id: str) -> Sequence[Mapping[str, object]]:
        """Return the run's decoded node items."""
        ...

    def binding(self, grant_id: str) -> Mapping[str, object] | None:
        """Return the decoded `SERVING#<grant>/BINDING` item."""
        ...

    def pointer(self, conflict_unit: str) -> Mapping[str, object] | None:
        """Return the decoded window generation pointer."""
        ...

    def projection_rows(self, root_flow_id: str) -> Sequence[Any]:
        """Return the root's NODE, CLAIM and SERVING projection rows."""
        ...

    def measured(self, measurement_id: str) -> bool:
        """Return whether `MEASUREMENT#<id>` exists, strongly read."""
        ...

    def spans(
        self, run_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Return the 5a retrieval of the run's graph spans."""
        ...

    def explanation(self, root_flow_id: str) -> Mapping[str, object]:
        """Return the deployed explanation of the root flow."""
        ...

    def dlq_status(self) -> Mapping[str, object]:
        """Return the operator CLI's `dlq-status` document."""
        ...

    def bake_spans(
        self, run_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Return the 5a retrieval of the winner's `bake.run` and `bake.finalize` spans."""
        ...

    def serving_transition(self, domain: str, cohort_revision: str) -> Mapping[str, object] | None:
        """Return the drained cohort's decoded `SERVING_TRANSITION#` record, strongly read."""
        ...

    def serving_head(self, domain: str) -> ObservedHeadRead:
        """Strongly read the serving domain's head, stamped with the read instant."""
        ...

    def delivery_not_visible(self, window_start: datetime, window_end: datetime) -> float | None:
        """Return the delivery queue's 60-second Maximum NotVisible over the run window."""
        ...

    def batch_spans(
        self, batch_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Slice 7: return the 5a retrieval of one batch's span, by `batch_id`."""
        ...

    def table_lock(self, lock_key: str) -> Mapping[str, object] | None:
        """Slice 7: return `LOCK#<lock_key>/LOCK`, strongly read."""
        ...

    def batch_records(self, window_start: datetime, window_end: datetime) -> Mapping[str, object]:
        """Slice 7: the TableWriter log group's set records, batch metrics and budget counts."""
        ...


def gather_batching(
    reads: P6eReads,
    nodes: Sequence[Mapping[str, object]],
    explanation: Mapping[str, object],
    *,
    spans: Mapping[str, object],
    window_start: datetime,
    window_end: datetime,
) -> BatchingEvidence:
    """Collect every `batch_id` the run's units ran in; a unit with none is a `ProbeError`.

    `spans` is the run's retrieval by `run_id`; its unit spans are judged against the batch
    spans retrieved here by `batch_id`, and against each unit's `(batch_id, node_id)` (J1).
    """
    units = [node for node in nodes if str(node.get("node", "")).startswith("write_unit:")]
    batch_ids: list[str] = []
    expected_units: list[tuple[str, str]] = []
    locks: set[str] = set()
    for node in units:
        batch = node.get("batch_id")
        if not isinstance(batch, str) or not batch:
            raise ProbeError(f"the unit {node.get('node')} carries no batch_id")
        node_id = node.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise ProbeError(f"the unit {node.get('node')} carries no node_id")
        if batch not in batch_ids:
            batch_ids.append(batch)
        expected_units.append((batch, node_id))
        locks.add(str(node.get("lock_key")))
    records = reads.batch_records(window_start, window_end)
    sets = [
        record
        for record in cast("Sequence[Mapping[str, object]]", records.get("sets", ()))
        if record.get("batch_id") in batch_ids
    ]
    return BatchingEvidence(
        batch_ids=tuple(batch_ids),
        batch_spans={
            batch: reads.batch_spans(batch, window_start, window_end) for batch in batch_ids
        },
        expected_units=tuple(expected_units),
        unit_spans=[
            span
            for span in cast("Sequence[Mapping[str, object]]", spans.get("spans", ()))
            if span.get("name") == GRAPH_UNIT_SPAN
        ],
        metrics={
            batch: samples
            for batch, samples in cast(
                "Mapping[str, Mapping[str, Sequence[float]]]", records.get("metrics", {})
            ).items()
            if batch in batch_ids
        },
        set_records=sets,
        budget_records=cast("Mapping[str, int]", records.get("budget", {})),
        table_locks={lock: reads.table_lock(lock) for lock in sorted(locks)},
        explanation_stages=cast("Sequence[Mapping[str, object]]", explanation.get("stages", ())),
    )


def _seconds_between(start: object, end: object) -> float:
    return (datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))).total_seconds()


def _required_text(item: Mapping[str, object], name: str, what: str) -> str:
    value = item.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"{what} carries no {name}")
    return value


def _span_outcomes(retrieved: Mapping[str, object], name: str, cohort: str) -> tuple[str, ...]:
    if retrieved.get("status") != "complete" or retrieved.get("truncated"):
        raise ProbeError("the bake span retrieval is incomplete")
    return tuple(
        str(_attribute(span, "outcome"))
        for span in cast("Sequence[Mapping[str, object]]", retrieved.get("spans", ()))
        if span.get("name") == name and _attribute(span, "cohort_revision") == cohort
    )


def gather_observed_cohort(
    reads: P6eReads,
    run_ids: Sequence[str],
    *,
    window_start: datetime,
    window_end: datetime,
) -> ObservedCohortEvidence:
    """Strongly gather the named runs, each cohort's bake chain, and its promoter transition."""
    from domain.operations.execution_graph_release import serving_generation

    named = tuple(run_ids)
    if not named or len(set(named)) != len(named) or any(not run_id for run_id in named):
        raise ProbeError("the observed cohort run ids must be non-empty and unique")
    runs: list[ObservedCohortRun] = []
    domains: dict[str, str] = {}
    for run_id in named:
        run = reads.run(run_id)
        grant_id = _required_text(run, "grant_id", f"run {run_id}")
        unit = _required_text(run, "conflict_unit", f"run {run_id}")
        binding = reads.binding(grant_id)
        if binding is None:
            raise ProbeError(f"run {run_id} has no serving binding")
        nodes = {str(node.get("node")): node for node in reads.nodes(run_id)}
        publish = nodes.get("publish")
        pointer = reads.pointer(unit)
        if publish is None or pointer is None:
            raise ProbeError(f"run {run_id} has no publish node or generation pointer")
        kind = binding.get("binding_kind")
        if kind not in {"alias", "direct"}:
            raise ProbeError(f"run {run_id} has neither an alias nor direct binding")
        attempts = binding.get("execution_attempts")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 0:
            raise ProbeError(f"run {run_id} binding carries invalid execution_attempts")
        contributors = binding.get("contributor_roots", ())
        if not isinstance(contributors, list | tuple) or any(
            not isinstance(root, str) or not root for root in contributors
        ):
            raise ProbeError(f"run {run_id} binding carries invalid contributor_roots")
        cohort = _required_text(binding, "cohort_revision", f"run {run_id} binding")
        domains[cohort] = _required_text(binding, "domain", f"run {run_id} binding")
        direct = binding.get("direct_binding_id")
        runs.append(
            ObservedCohortRun(
                run_id=run_id,
                root_flow_id=_required_text(run, "root_flow_id", f"run {run_id}"),
                cohort_revision=cohort,
                binding_id=_required_text(binding, "binding_id", f"run {run_id} binding"),
                winner_run_id=_required_text(binding, "winner_run_id", f"run {run_id} binding"),
                publication_effect_digest=_required_text(
                    publish, "effect_digest", f"run {run_id} publish"
                ),
                # The semantic pointer carries the grant, not a generation (x5): derive it as
                # the runtime stamps it, `serving_generation(grant_id)`.
                pointer_generation=serving_generation(
                    _required_text(pointer, "grant_id", f"run {run_id} pointer")
                ),
                root_clock_started_at=_required_text(run, "root_clock_started_at", run_id),
                publish_flipped_at=_required_text(pointer, "flipped_at", f"run {run_id} pointer"),
                publish_settled_at=_required_text(publish, "settled_at", f"run {run_id} publish"),
                terminal_at=_required_text(run, "terminal_at", f"run {run_id}"),
                state=str(run.get("state")),
                serving_outcome=None
                if run.get("serving_outcome") is None
                else str(run.get("serving_outcome")),
                binding_kind=str(kind),
                execution_attempts=attempts,
                contributor_roots=tuple(cast("Sequence[str]", contributors)),
                direct_binding_id=direct if isinstance(direct, str) and direct else None,
            )
        )
    bakes: list[ObservedBakeChain] = []
    transitions: list[ObservedPromoterTransition] = []
    for cohort in sorted(domains):
        winners = {run.winner_run_id for run in runs if run.cohort_revision == cohort}
        if len(winners) != 1:
            raise ProbeError(f"cohort {cohort} names {len(winners)} winners")
        (winner,) = winners
        retrieved = reads.bake_spans(winner, window_start, window_end)
        bakes.append(
            ObservedBakeChain(
                cohort,
                winner,
                _span_outcomes(retrieved, "bake.run", cohort),
                _span_outcomes(retrieved, "bake.finalize", cohort),
                _finalize_ended_at(retrieved, cohort),
            )
        )
        transition = reads.serving_transition(domains[cohort], cohort)
        if transition is not None:
            elected = transition.get("elected_cohort_revision")
            outcome = transition.get("outcome")
            transitions.append(
                ObservedPromoterTransition(
                    cohort,
                    elected if isinstance(elected, str) and elected else None,
                    _required_text(transition, "aws_request_id", f"transition {cohort}"),
                    _required_text(transition, "drained_at", f"transition {cohort}"),
                    outcome if isinstance(outcome, str) else None,
                )
            )
    # Read after every finalize retrieval above, so each head read follows each finalize.
    heads = tuple(reads.serving_head(domain) for domain in sorted(set(domains.values())))
    return ObservedCohortEvidence(
        tuple(runs),
        tuple(bakes),
        tuple(transitions),
        heads,
        tuple(sorted(domains.items())),
    )


def _finalize_ended_at(retrieved: Mapping[str, object], cohort: str) -> str | None:
    ends = [
        int(cast("int | str", span.get("endTimeUnixNano")))
        for span in cast("Sequence[Mapping[str, object]]", retrieved.get("spans", ()))
        if span.get("name") == "bake.finalize"
        and _attribute(span, "cohort_revision") == cohort
        and _attribute(span, "outcome") == BAKE_FINALIZE_SUCCEEDED
        and str(span.get("endTimeUnixNano") or "").isdigit()
    ]
    return datetime.fromtimestamp(max(ends) / 1e9, tz=UTC).isoformat() if ends else None


def gather_p6e(
    reads: P6eReads,
    run_id: str,
    *,
    window_start: datetime,
    window_end: datetime,
    windows_per_cycle: int,
    cohort_run_ids: Sequence[str] | None = None,
) -> P6eEvidence:
    """Read one run end to end for P6(e); a missing item is a `ProbeError`, never a pass."""
    run = reads.run(run_id)
    listed = reads.nodes(run_id)
    nodes = {str(node["node"]): node for node in listed}
    grant_id, unit = run.get("grant_id"), run.get("conflict_unit")
    publish, grant_apply = nodes.get("publish"), nodes.get("grant_apply")
    if not isinstance(grant_id, str) or not isinstance(unit, str):
        raise ProbeError("the run carries no grant or conflict unit")
    if publish is None or grant_apply is None:
        raise ProbeError("the run has no publish or grant_apply node")
    binding, pointer = reads.binding(grant_id), reads.pointer(unit)
    if binding is None or pointer is None:
        raise ProbeError("the run has no serving binding or generation pointer")
    root = str(run["root_flow_id"])
    explanation = reads.explanation(root)
    spans = reads.spans(run_id, window_start, window_end)
    return P6eEvidence(
        spans=spans,
        units=sum(1 for name in nodes if name.startswith("write_unit:")),
        rows=reads.projection_rows(root),
        measured=reads.measured,
        explanation=explanation,
        dlq_status=reads.dlq_status(),
        flipped_at=str(pointer["flipped_at"]),
        publish_settled_at=str(publish["settled_at"]),
        bound_at=str(binding["bound_at"]),
        pre_grant_seconds=_seconds_between(run["root_clock_started_at"], grant_apply["settled_at"]),
        windows_per_cycle=windows_per_cycle,
        run_state=str(run.get("state")),
        serving_outcome=None
        if run.get("serving_outcome") is None
        else str(run.get("serving_outcome")),
        observed_cohort=gather_observed_cohort(
            reads,
            (run_id,) if cohort_run_ids is None else cohort_run_ids,
            window_start=window_start,
            window_end=window_end,
        ),
        delivery_not_visible_maximum=reads.delivery_not_visible(window_start, window_end),
        batching=gather_batching(
            reads,
            listed,
            explanation,
            spans=spans,
            window_start=window_start,
            window_end=window_end,
        ),
    )


BATCHING_SCANS: Final = (
    {
        "FilterExpression": "begins_with(#pk, :lock_table)",
        "ExpressionAttributeNames": {"#pk": "pk"},
        "ExpressionAttributeValues": {":lock_table": {"S": LOCK_TABLE_PREFIX}},
    },
    {
        "FilterExpression": (
            "begins_with(#pk, :run) AND #item_kind = :node AND begins_with(#node, :write_unit) "
            "AND #state <> :pending"
        ),
        "ExpressionAttributeNames": {
            "#pk": "pk",
            "#item_kind": "item_kind",
            "#node": "node",
            "#state": "state",
        },
        "ExpressionAttributeValues": {
            ":run": {"S": "RUN#"},
            ":node": {"S": "node"},
            ":write_unit": {"S": "write_unit:"},
            ":pending": {"S": "PENDING"},
        },
    },
)
"""Design §4 "Before the CAS", the batching readback: no `LOCK#TABLE#` item and no non-PENDING
`write_unit` node may exist before the `GRAPH` CAS (in `SHADOW` no worker arms a unit)."""


def batching_readback_counts(dynamodb: Any, graph_table: str) -> dict[str, object]:
    """Run the two read-only `Scan`s, `Select=COUNT`, strongly, summing every page."""
    counts: list[int] = []
    for scan in BATCHING_SCANS:
        request: dict[str, Any] = {
            "TableName": graph_table,
            "Select": "COUNT",
            "ConsistentRead": True,
            **scan,
        }
        count = 0
        while True:
            page = dynamodb.scan(**request)
            count += int(page.get("Count", 0))
            start = page.get("LastEvaluatedKey")
            if start is None:
                break
            request["ExclusiveStartKey"] = start
        counts.append(count)
    table_locks, units = counts
    return {"table": graph_table, "table_locks": table_locks, "armed_units": units}


def batching_readback_verdict(read: Mapping[str, object]) -> dict[str, object]:
    """Pass only when both counts are zero; anything else is a stop back to design review."""
    return {**read, "passed": read.get("table_locks") == 0 and read.get("armed_units") == 0}


@dataclass(frozen=True, slots=True)
class BotoP6eReads:
    """P6(e)'s live reads under the operator session: strong, read-only, every table named."""

    session: Any
    environment: str
    graph_table: str
    status_table: str
    publications_table: str
    api: Any
    table_writer_log_group: str = ""
    """Slice 7: the TableWriter log group the batch records are read from (an argument)."""

    def _get(self, table: str, key: Mapping[str, object]) -> Mapping[str, object] | None:
        from adapters.outbound.dynamodb.operational_status import decode_item

        found = self.session.client("dynamodb").get_item(
            TableName=table, Key=dict(key), ConsistentRead=True
        )
        item = found.get("Item")
        return None if item is None else decode_item(item)

    def run(self, run_id: str) -> Mapping[str, object]:
        """Read `RUN#<run_id>/RUN` strongly."""
        found = self._get(self.graph_table, {"pk": {"S": f"RUN#{run_id}"}, "sk": {"S": "RUN"}})
        if found is None:
            raise ProbeError("no such run")
        return found

    def nodes(self, run_id: str) -> Sequence[Mapping[str, object]]:
        """Query the run's `NODE#` items strongly, every page."""
        from adapters.outbound.dynamodb.operational_status import decode_item

        paginator = self.session.client("dynamodb").get_paginator("query")
        pages = paginator.paginate(
            TableName=self.graph_table,
            KeyConditionExpression="pk = :pk AND begins_with(sk, :node)",
            ExpressionAttributeValues={":pk": {"S": f"RUN#{run_id}"}, ":node": {"S": "NODE#"}},
            ConsistentRead=True,
        )
        return [decode_item(item) for page in pages for item in page.get("Items", [])]

    def binding(self, grant_id: str) -> Mapping[str, object] | None:
        """Read the grant's serving binding strongly."""
        return self._get(
            self.graph_table, {"pk": {"S": f"SERVING#{grant_id}"}, "sk": {"S": "BINDING"}}
        )

    def pointer(self, conflict_unit: str) -> Mapping[str, object] | None:
        """Read `GENERATION#<U>/POINTER` strongly."""
        return self._get(
            self.publications_table,
            {
                "partition_key": {"S": f"GENERATION#{conflict_unit}"},
                PUBLICATIONS_RECORD_KEY: {"S": "POINTER"},
            },
        )

    def projection_rows(self, root_flow_id: str) -> Sequence[Any]:
        """Read the root's execution rows; a cut read is refused."""
        from adapters.outbound.dynamodb.operational_status import DynamoGraphProjectionStore

        store = DynamoGraphProjectionStore(self.status_table, self.session.client("dynamodb"))
        rows, truncated = store.executions(root_flow_id)
        if truncated:
            raise ProbeError("the root's execution rows exceed one bounded read")
        return rows

    def measured(self, measurement_id: str) -> bool:
        """Read `MEASUREMENT#<id>` strongly."""
        key = {
            "partition_key": {"S": f"MEASUREMENT#{measurement_id}"},
            "sort_key": {"S": "MEASUREMENT"},
        }
        return self._get(self.status_table, key) is not None

    def spans(
        self, run_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Retrieve the run's graph spans through 5a."""
        from adapters.jobs.span_retrieval import LogsInsightsSpanClient

        query = run_span_query(
            self.environment, run_id, window_start=window_start, window_end=window_end
        )
        return LogsInsightsSpanClient(self.session.client("logs"), timeout_seconds=180.0).retrieve(
            query
        )

    def explanation(self, root_flow_id: str) -> Mapping[str, object]:
        """Explain the root flow through the deployed route, as the status persona."""
        status, document = _explain(self.api, {"flow_id": root_flow_id})
        if status != 200:  # noqa: PLR2004 -- the one success status
            raise ProbeError(f"the explanation answered {status}")
        return document

    def dlq_status(self) -> Mapping[str, object]:
        """Read the four dead-letter queues' counts (the operator CLI's `dlq-status`)."""
        from adapters.jobs.execution_graph.dead_letters import dlq_status

        return dlq_status(self.session.client("sqs"), self.environment)

    def bake_spans(
        self, run_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Retrieve the winner's `bake.run` and `bake.finalize` spans through 5a."""
        from adapters.jobs.span_retrieval import (
            LogsInsightsSpanClient,
            build_span_query,
            declared_span_attributes,
        )

        query = build_span_query(
            environment=self.environment,
            window_start=window_start,
            window_end=window_end,
            declared_attributes=declared_span_attributes(),
            keys=(("run_id", run_id),),
            span_names=("bake.run", "bake.finalize"),
            limit=1000,
            max_pages=8,
        )
        return LogsInsightsSpanClient(self.session.client("logs"), timeout_seconds=180.0).retrieve(
            query
        )

    def serving_head(self, domain: str) -> ObservedHeadRead:
        """Read `SERVING_DOMAIN#<domain>/HEAD` strongly."""
        item = self._get(
            self.graph_table, {"pk": {"S": f"SERVING_DOMAIN#{domain}"}, "sk": {"S": "HEAD"}}
        )
        state, cohort = (item or {}).get("state"), (item or {}).get("cohort_revision")
        return ObservedHeadRead(
            domain,
            state if isinstance(state, str) else None,
            cohort if isinstance(cohort, str) else None,
            datetime.now(UTC).isoformat(),
        )

    def serving_transition(self, domain: str, cohort_revision: str) -> Mapping[str, object] | None:
        """Read `SERVING_TRANSITION#<domain>/COHORT#<cohort>` strongly."""
        return self._get(
            self.graph_table,
            {
                "pk": {"S": f"SERVING_TRANSITION#{domain}"},
                "sk": {"S": f"COHORT#{cohort_revision}"},
            },
        )

    def delivery_not_visible(self, window_start: datetime, window_end: datetime) -> float | None:
        """Read the delivery queue's in-run NotVisible maximum from CloudWatch."""
        data = self.session.client("cloudwatch").get_metric_statistics(
            Namespace="AWS/SQS",
            MetricName="ApproximateNumberOfMessagesNotVisible",
            Dimensions=[{"Name": "QueueName", "Value": f"adlytics-{self.environment}-ingestion"}],
            StartTime=window_start,
            EndTime=window_end,
            Period=60,
            Statistics=["Maximum"],
        )
        values = [
            float(point["Maximum"])
            for point in data.get("Datapoints", ())
            if isinstance(point.get("Maximum"), int | float)
            and not isinstance(point.get("Maximum"), bool)
        ]
        return max(values) if values else None

    def batch_spans(
        self, batch_id: str, window_start: datetime, window_end: datetime
    ) -> Mapping[str, object]:
        """Retrieve one batch's span through 5a, by `batch_id`."""
        from adapters.jobs.span_retrieval import LogsInsightsSpanClient

        query = run_batch_span_query(
            self.environment, batch_id, window_start=window_start, window_end=window_end
        )
        return LogsInsightsSpanClient(self.session.client("logs"), timeout_seconds=180.0).retrieve(
            query
        )

    def table_lock(self, lock_key: str) -> Mapping[str, object] | None:
        """Read `LOCK#<lock_key>/LOCK` strongly."""
        return self._get(self.graph_table, {"pk": {"S": f"LOCK#{lock_key}"}, "sk": {"S": "LOCK"}})

    def batch_records(self, window_start: datetime, window_end: datetime) -> Mapping[str, object]:
        """One Logs Insights query over the TableWriter log group for the batch evidence."""
        if not self.table_writer_log_group:
            raise ProbeError("--table-writer-log-group is required for P6(e)")
        return batch_records_from_logs(
            self.session.client("logs"),
            self.table_writer_log_group,
            window_start=window_start,
            window_end=window_end,
        )


BATCH_RECORD_LIMIT: Final = 10_000
"""The rows one `GetQueryResults` page returns at most; a read that reaches it may be cut."""


def batch_records_from_logs(
    logs: Any, log_group: str, *, window_start: datetime, window_end: datetime
) -> dict[str, object]:
    """Read the set records, batch metric EMF lines and budget reports, read-only.

    A query that does not complete is a `ProbeError`, never an empty (passing) read. So is a
    complete one that may be cut (FEAT-009 G2): a missing record could be the budget, park or
    slow-set evidence the verdict needs, so the read must prove it returned every match.
    """
    import time

    query = (
        "fields @message | filter @message like /graph_unit_set_executed/ or "
        "@message like /GraphUnitBatch/ or @message like /semantic_statement_budget/ or "
        f"@message like /semantic_attempt_parked/ | limit {BATCH_RECORD_LIMIT}"
    )
    started = logs.start_query(
        logGroupName=log_group,
        startTime=int(window_start.timestamp()),
        endTime=int(window_end.timestamp()),
        queryString=query,
        limit=BATCH_RECORD_LIMIT,
    )
    for _ in range(90):
        result = logs.get_query_results(queryId=started["queryId"])
        if result.get("status") == "Complete":
            break
        if result.get("status") in {"Failed", "Cancelled", "Timeout"}:
            raise ProbeError(f"the batch record query ended {result.get('status')}")
        time.sleep(2)
    else:
        raise ProbeError("the batch record query did not complete")
    rows = result.get("results", [])
    batch_records_complete(rows, result.get("statistics"), result.get("nextToken"))
    messages = [
        str(field.get("value")) for row in rows for field in row if field.get("field") == "@message"
    ]
    return parse_batch_records(messages)


def batch_records_complete(rows: Sequence[object], statistics: object, next_token: object) -> None:
    """Refuse a complete Logs Insights result that may omit matching records (FEAT-009 G2).

    Fail closed: the result reached `BATCH_RECORD_LIMIT`, carries a further page, reports more
    matched records than it returned, or carries no `recordsMatched` to compare against. The
    operator narrows the window; the read is never judged on a part of its evidence.
    """
    returned = len(rows)
    if returned >= BATCH_RECORD_LIMIT or next_token:
        raise ProbeError(
            f"the batch record query reached its {BATCH_RECORD_LIMIT}-row limit; narrow the window"
        )
    matched = (
        cast("dict[str, object]", statistics).get("recordsMatched")
        if isinstance(statistics, dict)
        else None
    )
    if not isinstance(matched, int | float) or isinstance(matched, bool):
        raise ProbeError("the batch record query reported no recordsMatched; completeness unproven")
    if matched > returned:
        raise ProbeError(
            f"the batch record query returned {returned} of {int(matched)} matched records"
        )


def parse_batch_records(messages: Sequence[str]) -> dict[str, object]:
    """Parse TableWriter JSON log lines into set records, batch metrics and budget counts.

    A batch metric sample is credited to the batch its `batch_id` property names (F6); one
    without the property is attributable to no batch and is not credited.
    """
    sets: list[Mapping[str, object]] = []
    metrics: dict[str, dict[str, list[float]]] = {}
    budget = {"semantic_statement_budget": 0, "semantic_attempt_parked": 0}
    for message in messages:
        try:
            line = json.loads(message)
        except ValueError:
            continue
        if not isinstance(line, dict):
            continue
        record = cast("dict[str, object]", line)
        batch = record.get("batch_id")
        for name in BATCH_METRICS:
            if name in record and isinstance(batch, str) and batch:
                # Powertools' EMF writes a metric's values as a list (one per `add_metric`).
                raw = record[name]
                values = cast("list[object]", raw) if isinstance(raw, list) else [raw]
                per_batch = metrics.setdefault(batch, {})
                per_batch.setdefault(name, []).extend(float(cast("float", v)) for v in values)
        # `report_operation` writes an event's details at the top level of the line, beside
        # `operation`, `phase` and `outcome`; no live line nests them (P6E-2).
        phase, outcome = record.get("phase"), record.get("outcome")
        if phase == SET_RECORD_PHASE:
            sets.append(
                {
                    **{name: record[name] for name in SET_RECORD_FIELDS if name in record},
                    "outcome": outcome,
                }
            )
            continue
        if record.get("writer_kind") != "graph_unit":
            continue  # only graph-unit sets carry the allowance (D43)
        if record.get("operation") == "semantic_statement_budget" and outcome == "insufficient":
            budget["semantic_statement_budget"] += 1
        elif record.get("operation") == "semantic_attempt_parked":
            budget["semantic_attempt_parked"] += 1
    return {"sets": sets, "metrics": metrics, "budget": budget}


def _graph_acceptance(target: Target, arguments: argparse.Namespace) -> int:
    """`--p6-step pre-cas` (zero-unstamped count) or `--p6-step e` (P6(e)), read-only.

    Written for S6 part 2 (plan B8) and unit-tested over fakes; not run before the approvals
    and the dark deploy. The page and chat reader readbacks are captured by the operator under a
    persona and compared with `reader_readback_verdict`; this step reads the probe surface only.
    """
    session = _session(target)
    environment = target.environment
    ssm = session.client("ssm")
    publications = str(
        ssm.get_parameter(Name=f"/adlytics/{environment}/data/v2/semantic-publications-table")[
            "Parameter"
        ]["Value"]
    )
    step = str(arguments.p6_step)
    evidence = Evidence(f"P6-{step}", _utc_stamp())
    if step == "pre-cas":
        verdict = zero_unstamped_verdict(
            unstamped_current_count(session.client("dynamodb"), publications)
        )
        # Slice 7 (P5): the batching readback beside the zero-unstamped count, both read-only.
        batching = batching_readback_verdict(
            batching_readback_counts(
                session.client("dynamodb"),
                _store_table(session, environment, str(arguments.store_stack)),
            )
        )
        verdict = {
            "unstamped_current": verdict,
            "batching_readback": batching,
            "passed": bool(verdict["passed"]) and bool(batching["passed"]),
        }
    else:
        for name in ("run_id", "window_start", "window_end", "windows_per_cycle"):
            value = getattr(arguments, name)
            if value is None or value == "" or value == []:
                raise ProbeError(f"--{name.replace('_', '-')} is required for P6(e)")
        status = str(
            ssm.get_parameter(
                Name=f"/adlytics/{environment}/operations/status-projection-table-name"
            )["Parameter"]["Value"]
        )
        reads = BotoP6eReads(
            session,
            environment,
            _store_table(session, environment, str(arguments.store_stack)),
            status,
            publications,
            _persona_api(session, environment, str(arguments.status_persona)),
            str(getattr(arguments, "table_writer_log_group", "") or ""),
        )
        run_ids = tuple(cast("Sequence[str]", arguments.run_id))
        verdict = p6e_verdict(
            gather_p6e(
                reads,
                run_ids[0],
                window_start=datetime.fromisoformat(str(arguments.window_start)),
                window_end=datetime.fromisoformat(str(arguments.window_end)),
                windows_per_cycle=int(arguments.windows_per_cycle),
                cohort_run_ids=run_ids,
            )
        )
    evidence.document.update(verdict)
    print(f"evidence: {evidence.write(target)}")
    return 0 if verdict["passed"] else 1


# --- D7 rolling-freshness hold-clear gates (acceptance criteria 1-9) ------------------------------

RETIREMENT_ROSTER_PATH: Final = (
    _ROOT / "validation" / "fixtures" / "runtime-admission-retirement-v1.json"
)
RETIREMENT_SCHEMA: Final = "runtime-admission-retirement-v1"
RETIREMENT_AUXILIARY_ROOTS: Final = (
    "validation/probe_ingestion_traceability_gate.py",
    "tests/unit/test_probe_ingestion_traceability_gate.py",
)
RETIREMENT_CALL_INVENTORY: Final = "infra/test/support/graph-call-inventory.ts"
RETIREMENT_MANDATORY_FILES: Final = (
    "src/domain/operations/generated_execution_graphs.py",
    "infra/lib/generated/execution-graph-protocol.ts",
    RETIREMENT_CALL_INVENTORY,
    *RETIREMENT_AUXILIARY_ROOTS,
)
_RETIREMENT_LISTED_SUFFIXES: Final = frozenset({".md", ".sh"})
"""Tracked non-code files under a scanned root: listed in the receipt, never parsed."""
_TS_TOKEN: Final = re.compile(
    r"//[^\n]*|/\*.*?\*/|'(?:\\.|[^'\\\n])*'|\"(?:\\.|[^\"\\\n])*\"|`(?:\\.|[^`\\])*`"
    r"|[A-Za-z_$][\w$]*",
    re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class RetirementRoster:
    """The closed retired runtime-admission vocabulary, read from its committed fixture."""

    key_prefixes: tuple[str, ...]
    kinds: Mapping[str, str]
    """Every forbidden identifier and its class: type, field, method, function or state."""
    identity_type: str
    identity_day_members: frozenset[str]
    allowlist: frozenset[str]


def load_retirement_roster(path: Path = RETIREMENT_ROSTER_PATH) -> RetirementRoster:
    """Load and validate the retired vocabulary; a malformed roster is a `ProbeError`."""
    try:
        document = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError(f"the retirement roster is unreadable: {path}") from error
    if not isinstance(document, dict) or document.get("schema_version") != RETIREMENT_SCHEMA:
        raise ProbeError("the retirement roster has the wrong schema")
    raw = cast("dict[str, object]", document)

    def names(key: str) -> tuple[str, ...]:
        value = raw.get(key)
        if not isinstance(value, list) or not value:
            raise ProbeError(f"the retirement roster has no {key}")
        items = cast("list[object]", value)
        if any(not isinstance(item, str) or not item for item in items) or len(set(items)) != len(
            items
        ):
            raise ProbeError(f"the retirement roster {key} are not unique nonblank names")
        return tuple(cast("list[str]", items))

    kinds: dict[str, str] = {}
    for key, kind in (
        ("types", "type"),
        ("fields", "field"),
        ("methods", "method"),
        ("functions", "function"),
        ("states", "state"),
    ):
        for name in names(key):
            if name in kinds:
                raise ProbeError(f"the retirement roster names {name} twice")
            kinds[name] = kind
    identity = raw.get("identity_type")
    if not isinstance(identity, str) or not identity:
        raise ProbeError("the retirement roster has no identity_type")
    return RetirementRoster(
        key_prefixes=names("key_prefixes"),
        kinds=kinds,
        identity_type=identity,
        identity_day_members=frozenset(names("identity_day_members")),
        allowlist=frozenset(names("allowlist")),
    )


@dataclass(frozen=True, slots=True)
class RetirementHit:
    """One retired-vocabulary occurrence, qualified to its file and enclosing definition."""

    path: str
    qualname: str
    line: int
    kind: str
    name: str
    allowlisted: bool


@dataclass(frozen=True, slots=True)
class RuntimeAdmissionRetirementV1:
    """The closed retirement inventory receipt (acceptance criterion 1)."""

    roster_sha256: str
    scanned_files: tuple[str, ...]
    listed_files: tuple[str, ...]
    auxiliary_roots: tuple[str, ...]
    hits: tuple[RetirementHit, ...]
    unknown_files: tuple[str, ...]
    parse_failures: tuple[str, ...]
    inventory_drift: tuple[str, ...]

    def document(self) -> dict[str, object]:
        """Return the JSON-ready receipt with its verdict."""
        forbidden = [hit for hit in self.hits if not hit.allowlisted]
        return {
            "schema_version": "runtime-admission-retirement-receipt-v1",
            "roster_sha256": self.roster_sha256,
            "scanned_files": list(self.scanned_files),
            "listed_files": list(self.listed_files),
            "auxiliary_roots": list(self.auxiliary_roots),
            "allowlisted_hits": [_hit_document(hit) for hit in self.hits if hit.allowlisted],
            "forbidden_hits": [_hit_document(hit) for hit in forbidden],
            "unknown_files": list(self.unknown_files),
            "parse_failures": list(self.parse_failures),
            "inventory_drift": list(self.inventory_drift),
            "passed": not (
                forbidden or self.unknown_files or self.parse_failures or self.inventory_drift
            ),
        }


def _hit_document(hit: RetirementHit) -> dict[str, object]:
    return {
        "path": hit.path,
        "qualname": hit.qualname,
        "line": hit.line,
        "kind": hit.kind,
        "name": hit.name,
    }


class _RetirementVisitor(ast.NodeVisitor):
    """Collect every retired name, aliased import/call, dataclass field, and string constant."""

    def __init__(self, path: str, roster: RetirementRoster) -> None:
        self.path = path
        self.roster = roster
        self.stack: list[tuple[str, bool]] = []
        self.aliases: dict[str, str] = {}
        self.hits: list[RetirementHit] = []

    def _hit(self, node: ast.AST, kind: str, name: str, *, literal: bool = False) -> None:
        qualname = ".".join(item for item, _ in self.stack) or "<module>"
        owner = self.stack[0] if self.stack else None
        allowlisted = (
            literal
            and owner is not None
            and owner[1]
            and f"{self.path}::{owner[0]}" in self.roster.allowlist
        )
        self.hits.append(
            RetirementHit(self.path, qualname, getattr(node, "lineno", 0), kind, name, allowlisted)
        )

    def _identifier(self, node: ast.AST, name: str | None) -> None:
        if name is None:
            return
        kind = self.roster.kinds.get(name)
        if kind is not None:
            self._hit(node, kind, name)
        elif name in self.aliases:
            self._hit(node, "aliased_call", f"{name}->{self.aliases[name]}")

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._identifier(node, node.name)
        arguments = node.args
        for argument in (
            *arguments.posonlyargs,
            *arguments.args,
            *arguments.kwonlyargs,
            *(item for item in (arguments.vararg, arguments.kwarg) if item is not None),
        ):
            self._identifier(argument, argument.arg)
        self.stack.append((node.name, True))
        self.generic_visit(node)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._identifier(node, node.name)
        if node.name == self.roster.identity_type:
            for statement in node.body:
                target = statement.target if isinstance(statement, ast.AnnAssign) else None
                if isinstance(target, ast.Name) and target.id in self.roster.identity_day_members:
                    self._hit(statement, "type", f"{node.name}.{target.id}")
        self.stack.append((node.name, False))
        self.generic_visit(node)
        self.stack.pop()

    def _imported(self, node: ast.Import | ast.ImportFrom) -> None:
        for alias in node.names:
            original = alias.name.rsplit(".", 1)[-1]
            if original in self.roster.kinds:
                self._hit(node, self.roster.kinds[original], original)
                if alias.asname:
                    self.aliases[alias.asname] = original
                    self._hit(node, "aliased_import", f"{alias.asname}->{original}")

    def visit_Import(self, node: ast.Import) -> None:
        self._imported(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._imported(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._identifier(node, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._identifier(node, node.attr)
        self.generic_visit(node)

    def visit_keyword(self, node: ast.keyword) -> None:
        self._identifier(node, node.arg)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str):
            return
        for prefix in self.roster.key_prefixes:
            if prefix in node.value:
                self._hit(node, "literal", prefix, literal=True)
        kind = self.roster.kinds.get(node.value)
        if kind is not None:
            self._hit(node, kind, node.value, literal=True)


def _typescript_hits(path: str, text: str, roster: RetirementRoster) -> list[RetirementHit]:
    """Lexically scan one TypeScript file: string literals and identifiers, comments skipped."""
    hits: list[RetirementHit] = []
    for match in _TS_TOKEN.finditer(text):
        token = match.group(0)
        if token.startswith(("//", "/*")):
            continue
        line = text.count("\n", 0, match.start()) + 1
        if token[0] in "'\"`":
            body = token[1:-1]
            for prefix in roster.key_prefixes:
                if prefix in body:
                    hits.append(
                        RetirementHit(path, "<typescript>", line, "iam_prefix", prefix, False)
                    )
            kind = roster.kinds.get(body)
            if kind is not None:
                hits.append(RetirementHit(path, "<typescript>", line, kind, body, False))
            continue
        kind = roster.kinds.get(token)
        if kind is not None:
            hits.append(RetirementHit(path, "<typescript>", line, kind, token, False))
    return hits


def _retirement_files(root: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (scanned, listed, unknown) relative paths under every broad runtime root."""
    scanned: list[str] = []
    listed: list[str] = []
    unknown: list[str] = []
    for base, suffix in (("src", ".py"), ("infra/lib", ".ts")):
        directory = root / base
        if not directory.is_dir():
            unknown.append(f"{base}/ (missing root)")
            continue
        for path in sorted(directory.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            relative = path.relative_to(root).as_posix()
            if path.suffix == suffix and not path.is_symlink():
                scanned.append(relative)
            elif path.name == ".gitkeep" or path.suffix in _RETIREMENT_LISTED_SUFFIXES:
                listed.append(relative)
            else:
                unknown.append(relative)
    for relative in (RETIREMENT_CALL_INVENTORY, *RETIREMENT_AUXILIARY_ROOTS):
        if (root / relative).is_file() and relative not in scanned:
            scanned.append(relative)
    return scanned, listed, unknown


def runtime_admission_retirement(
    root: Path = _ROOT, roster_path: Path = RETIREMENT_ROSTER_PATH
) -> RuntimeAdmissionRetirementV1:
    """Inventory every runtime, generated, call-inventory and auxiliary root (criterion 1)."""
    roster = load_retirement_roster(roster_path)
    scanned, listed, unknown = _retirement_files(root)
    drift = [relative for relative in RETIREMENT_MANDATORY_FILES if relative not in scanned]
    hits: list[RetirementHit] = []
    failures: list[str] = []
    for relative in scanned:
        text = (root / relative).read_text()
        if relative.endswith(".ts"):
            hits.extend(_typescript_hits(relative, text, roster))
            continue
        try:
            tree = ast.parse(text, filename=relative)
        except SyntaxError as error:
            failures.append(f"{relative}:{error.lineno or 0}")
            continue
        visitor = _RetirementVisitor(relative, roster)
        visitor.visit(tree)
        hits.extend(visitor.hits)
    return RuntimeAdmissionRetirementV1(
        roster_sha256=hashlib.sha256(roster_path.read_bytes()).hexdigest(),
        scanned_files=tuple(scanned),
        listed_files=tuple(listed),
        auxiliary_roots=tuple(item for item in RETIREMENT_AUXILIARY_ROOTS if item in scanned),
        hits=tuple(hits),
        unknown_files=tuple(unknown),
        parse_failures=tuple(failures),
        inventory_drift=tuple(drift),
    )


# Census and inertness (criterion 2). The census owns the only legacy key literals in this file.

LEGACY_CENSUS_SCHEMA: Final = "legacy-serving-census-v1"
LEGACY_INERTNESS_SCHEMA: Final = "legacy-serving-inertness-v1"
SERVING_HEAD_STATES: Final = frozenset({"IDLE", "ACTIVE", "BOUND_DRAINING"})
_AUTHORITY_DUE_PREFIXES: Final = ("ARM#", "COMPLETE#", "COALESCE#", "PUBLISH_WAIT#")
_SERVING_HEAD_PK: Final = "SERVING_DOMAIN#"


def canonical_json_bytes(value: object) -> bytes:
    """Recursively key-ordered, compact, UTF-8 JSON: the census's one canonical encoding."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def _raw_key(item: Mapping[str, object]) -> tuple[str, str]:
    """Return the raw `(pk.S, sk.S)` pair; anything else is malformed."""
    pk, sk = item.get("pk"), item.get("sk")
    if not (isinstance(pk, Mapping) and isinstance(sk, Mapping)):
        raise ProbeError("a scanned item has a malformed key")
    pk_text = cast("Mapping[str, object]", pk).get("S")
    sk_text = cast("Mapping[str, object]", sk).get("S")
    if not isinstance(pk_text, str) or not isinstance(sk_text, str):
        raise ProbeError("a scanned item has a malformed key")
    return pk_text, sk_text


class LegacyCensusError(ProbeError):
    """The census could not be proven; `partial` is retained as invalid provenance only."""

    def __init__(self, message: str, partial: Mapping[str, object]) -> None:
        super().__init__(message)
        self.partial = dict(partial)


@dataclass(frozen=True, slots=True)
class LegacyServingCensusV1:
    """One strongly consistent full-table census of the retained historical legacy rows."""

    caller_arn: str
    region: str
    table_arn: str
    table_name: str
    scan_started_at: str
    scan_ended_at: str
    page_count: int
    scanned_count: int
    rows: tuple[Mapping[str, object], ...]
    day_state_counts: Mapping[str, int]
    day_missing_field_counts: Mapping[str, int]
    wait_state_counts: Mapping[str, int]
    wait_rollover_counts: Mapping[str, int]
    wait_missing_field_counts: Mapping[str, int]
    wait_row_count: int
    wait_keys: tuple[tuple[str, str], ...]
    authority_rows: tuple[Mapping[str, object], ...]
    canonical_length: int
    sha256: str

    def document(self) -> dict[str, object]:
        """Return the JSON-ready artifact."""
        return {
            "schema_version": LEGACY_CENSUS_SCHEMA,
            "caller_arn": self.caller_arn,
            "region": self.region,
            "table_arn": self.table_arn,
            "table_name": self.table_name,
            "scan_started_at": self.scan_started_at,
            "scan_ended_at": self.scan_ended_at,
            "page_count": self.page_count,
            "scanned_count": self.scanned_count,
            "rows": list(self.rows),
            "day_state_counts": dict(self.day_state_counts),
            "day_missing_field_counts": dict(self.day_missing_field_counts),
            "wait_state_counts": dict(self.wait_state_counts),
            "wait_rollover_counts": dict(self.wait_rollover_counts),
            "wait_missing_field_counts": dict(self.wait_missing_field_counts),
            "wait_row_count": self.wait_row_count,
            "wait_keys": [list(key) for key in self.wait_keys],
            "authority_rows": list(self.authority_rows),
            "canonical_length": self.canonical_length,
            "sha256": self.sha256,
        }


def _caller_and_table(session: Any, table_name: str) -> tuple[str, str]:
    caller = str(session.client("sts").get_caller_identity()["Arn"])
    # aws-pagination: DescribeTable names one exact table and returns only it.
    described = session.client("dynamodb").describe_table(TableName=table_name)
    return caller, str(described["Table"]["TableArn"])


def _counted(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _attribute_text(item: Mapping[str, object], name: str) -> str | None:
    value = item.get(name)
    if not isinstance(value, Mapping):
        return None
    raw = cast("Mapping[str, object]", value)
    found = raw.get("S", raw.get("N"))
    return found if isinstance(found, str) else None


def gather_legacy_serving_census(
    session: Any, table_name: str, *, region: str
) -> LegacyServingCensusV1:
    """Strongly scan the whole table, every page, and canonicalize the selected legacy rows.

    Selection is exact: a `SERVING_DAY#` partition with the `RESERVATION` sort key, or any
    `CAPACITY_WAIT#` sort key. The second branch is on the sort key because waits live under
    `DUE#<shard>` partitions. Every `SERVING_DOMAIN#*/HEAD` and every `ARM#`, `COMPLETE#`,
    `COALESCE#` or `PUBLISH_WAIT#` due row is retained for the inertness verdict.
    """
    day_fields = ("state", "revision")
    wait_fields = ("state", "rollover_count", "run_ids")
    partial: dict[str, object] = {"table_name": table_name, "pages": 0, "invalid": True}
    try:
        caller, table_arn = _caller_and_table(session, table_name)
        partial.update(caller_arn=caller, table_arn=table_arn)
        started = datetime.now(UTC).isoformat()
        client = session.client("dynamodb")
        request: dict[str, object] = {"TableName": table_name, "ConsistentRead": True}
        seen: set[bytes] = set()
        days: list[Mapping[str, object]] = []
        waits: list[Mapping[str, object]] = []
        authority: list[Mapping[str, object]] = []
        pages = scanned = 0
        while True:
            page = client.scan(**request)
            pages += 1
            items = cast("list[Mapping[str, object]]", page.get("Items", []))
            scanned += len(items)
            partial.update(pages=pages, scanned=scanned)
            for item in items:
                pk, sk = _raw_key(item)
                if pk.startswith("SERVING_DAY#") and sk == "RESERVATION":
                    days.append(item)
                elif sk.startswith("CAPACITY_WAIT#"):
                    waits.append(item)
                elif (pk.startswith(_SERVING_HEAD_PK) and sk == "HEAD") or (
                    pk.startswith("DUE#") and sk.startswith(_AUTHORITY_DUE_PREFIXES)
                ):
                    authority.append(item)
            token = page.get("LastEvaluatedKey")
            if not token:
                break
            marker = canonical_json_bytes(token)
            if marker in seen:
                raise ProbeError("the census pagination token repeated")
            seen.add(marker)
            request["ExclusiveStartKey"] = token
        ended = datetime.now(UTC).isoformat()
        if _caller_and_table(session, table_name) != (caller, table_arn):
            raise ProbeError("the census caller or table identity changed during the scan")
        rows = tuple(sorted((*days, *waits), key=_raw_key))
        encoded = canonical_json_bytes(list(rows))
    except LegacyCensusError:
        raise
    except Exception as error:
        raise LegacyCensusError(f"the census is not proven: {error}", partial) from error
    return LegacyServingCensusV1(
        caller_arn=caller,
        region=region,
        table_arn=table_arn,
        table_name=table_name,
        scan_started_at=started,
        scan_ended_at=ended,
        page_count=pages,
        scanned_count=scanned,
        rows=rows,
        day_state_counts=_counted(_attribute_text(item, "state") or "missing" for item in days),
        day_missing_field_counts=_counted(
            name for item in days for name in day_fields if name not in item
        ),
        wait_state_counts=_counted(_attribute_text(item, "state") or "missing" for item in waits),
        wait_rollover_counts=_counted(
            _attribute_text(item, "rollover_count") or "missing" for item in waits
        ),
        wait_missing_field_counts=_counted(
            name for item in waits for name in wait_fields if name not in item
        ),
        wait_row_count=len(waits),
        wait_keys=tuple(sorted(_raw_key(item) for item in waits)),
        authority_rows=tuple(sorted(authority, key=_raw_key)),
        canonical_length=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def read_legacy_serving_authority(
    session: Any, census: LegacyServingCensusV1
) -> tuple[tuple[tuple[str, str], Mapping[str, object] | None], ...]:
    """Strongly `GetItem` every head and obligation key the census enumerated."""
    client = session.client("dynamodb")
    reads: list[tuple[tuple[str, str], Mapping[str, object] | None]] = []
    for row in census.authority_rows:
        pk, sk = _raw_key(row)
        found = client.get_item(
            TableName=census.table_name,
            Key={"pk": {"S": pk}, "sk": {"S": sk}},
            ConsistentRead=True,
        ).get("Item")
        reads.append(((pk, sk), cast("Mapping[str, object] | None", found)))
    return tuple(reads)


@dataclass(frozen=True, slots=True)
class LegacyServingInertnessV1:
    """The strong head/obligation reads that prove retained legacy rows are inert."""

    census_sha256: str
    reads: tuple[Mapping[str, object], ...]
    reads_sha256: str
    reasons: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """Inert only with no reason at all."""
        return not self.reasons

    def document(self) -> dict[str, object]:
        """Return the JSON-ready artifact."""
        return {
            "schema_version": LEGACY_INERTNESS_SCHEMA,
            "census_sha256": self.census_sha256,
            "reads": list(self.reads),
            "reads_sha256": self.reads_sha256,
            "reasons": list(self.reasons),
            "passed": self.passed,
        }


def legacy_serving_inertness_verdict(
    census: LegacyServingCensusV1,
    reads: Sequence[tuple[tuple[str, str], Mapping[str, object] | None]],
) -> LegacyServingInertnessV1:
    """Prove the retained rows own nothing: no waits, IDLE heads, and no retired dependency.

    A disappeared or changed enumerated row, an unknown or missing head state, a malformed
    retired field, or any obligation depending on retired authority is fatal `NOT_PROVEN`.
    """
    retired_fields = (
        "serving_day",
        "origin_day",
        "reservation_day",
        "cycle_epoch",
        "capacity_wait_id",
        "wait_revision",
    )
    reasons: list[str] = []
    if census.wait_row_count:
        reasons.append(f"capacity_wait_rows_present:{census.wait_row_count}")
    expected = {_raw_key(row): row for row in census.authority_rows}
    observed = {key: item for key, item in reads}
    if set(observed) != set(expected) or len(observed) != len(reads):
        reasons.append("authority_read_incomplete")
    wait_keys = {sk for _, sk in census.wait_keys}
    for key in sorted(expected):
        item = observed.get(key)
        where = f"{key[0]}/{key[1]}"
        if item is None:
            reasons.append(f"authority_row_disappeared:{where}")
            continue
        if canonical_json_bytes(item) != canonical_json_bytes(expected[key]):
            reasons.append(f"authority_row_changed:{where}")
        if key[1] == "HEAD":
            state = _attribute_text(item, "state")
            if state is None:
                reasons.append(f"head_state_missing:{where}")
            elif state not in SERVING_HEAD_STATES:
                reasons.append(f"head_state_unknown:{where}")
            elif state != "IDLE":
                reasons.append(f"head_not_idle:{where}")
            continue
        for name in retired_fields:
            if name not in item:
                continue
            value = item[name]
            text = _attribute_text(item, name)
            if text is None and not (
                isinstance(value, Mapping) and cast("Mapping[str, object]", value).get("NULL")
            ):
                reasons.append(f"malformed_authority_field:{where}:{name}")
            elif text:
                reasons.append(f"retired_authority_dependency:{where}:{name}")
        texts = canonical_json_bytes(item).decode()
        if "CAPACITY_WAIT#" in texts or any(sk in texts for sk in wait_keys):
            reasons.append(f"references_wait_key:{where}")
    documents = tuple(
        {"pk": key[0], "sk": key[1], "item": None if item is None else dict(item)}
        for key, item in sorted(reads, key=lambda pair: pair[0])
    )
    return LegacyServingInertnessV1(
        census_sha256=census.sha256,
        reads=documents,
        reads_sha256=hashlib.sha256(canonical_json_bytes(list(documents))).hexdigest(),
        reasons=tuple(reasons),
    )


_CENSUS_GATED: Final = (
    "caller_arn",
    "table_arn",
    "table_name",
    "rows",
    "day_state_counts",
    "day_missing_field_counts",
    "wait_state_counts",
    "wait_rollover_counts",
    "wait_missing_field_counts",
    "wait_row_count",
    "canonical_length",
    "sha256",
)


def legacy_serving_census_verdict(
    baseline: LegacyServingCensusV1, readback: LegacyServingCensusV1
) -> dict[str, object]:
    """Require identical caller/table identity and byte-identical selected rows and counts.

    Scan timestamps, page count, and total scanned count are provenance and may differ.
    """
    before, after = baseline.document(), readback.document()
    differences = [name for name in _CENSUS_GATED if before[name] != after[name]]
    return {
        "baseline_sha256": baseline.sha256,
        "readback_sha256": readback.sha256,
        "differences": differences,
        "passed": not differences,
    }


def legacy_serving_cutover_verdict(
    census_a: LegacyServingCensusV1,
    inertness: LegacyServingInertnessV1,
    census_b: LegacyServingCensusV1,
) -> dict[str, object]:
    """Two matching censuses around one passing strong inertness read (stage 2 entry)."""
    matched = legacy_serving_census_verdict(census_a, census_b)
    bound = inertness.census_sha256 == census_a.sha256
    return {
        "census_match": matched,
        "inertness_passed": inertness.passed,
        "inertness_reasons": list(inertness.reasons),
        "inertness_bound_to_census_a": bound,
        "passed": bool(matched["passed"]) and inertness.passed and bound,
    }


FENCE_MINIMUM_WAIT_SECONDS: Final = 900
"""The configured old TableWriter Lambda lifetime at the stage-1 pin (tableWriterTimeoutSeconds)."""


def fence_readback_verdict(
    readback: Mapping[str, object], *, execution_id: str, expected_revision: int = 12
) -> dict[str, object]:
    """Check the operator's `show-rolling-freshness-fence` output against held GRAPH 12."""
    fence = cast("Mapping[str, object]", readback.get("fence") or {})
    receipt = cast("Mapping[str, object]", readback.get("receipt") or {})
    checks = {
        "state_set": fence.get("state") == "SET",
        "migration_id": fence.get("migration_id") == execution_id,
        "receipt_command_id": receipt.get("command_id") == execution_id,
        "owner_is_caller": bool(fence.get("owner"))
        and fence.get("owner") == readback.get("caller"),
        "revision": fence.get("revision") == expected_revision
        and readback.get("mode_revision") == expected_revision,
        "mode_graph": readback.get("mode") == "GRAPH",
        "held": readback.get("hold") is True,
        "table": bool(readback.get("table")),
    }
    return {"checks": checks, "passed": all(checks.values())}


def fence_wait_verdict(deployed_at: str, readback_at: str, cutover_at: str) -> dict[str, object]:
    """T0 is the later of stage-1 deployment and fence readback; cutover waits 900 s after it."""
    t0 = max(utc_instant(deployed_at, "deployed_at"), utc_instant(readback_at, "readback_at"))
    elapsed = (utc_instant(cutover_at, "cutover_at") - t0).total_seconds()
    return {
        "t0": t0.isoformat(),
        "elapsed_seconds": elapsed,
        "minimum_seconds": FENCE_MINIMUM_WAIT_SECONDS,
        "passed": elapsed >= FENCE_MINIMUM_WAIT_SECONDS,
    }


# Trace completeness (criterion 6).

TRACE_COHORT_CLASSES: Final = (
    "bake.run",
    "bake.source_pin",
    "bake.fan_out",
    "bake.task",
    "bake.phase",
    "bake.finalize",
    "precompute.run",
    "insight.precompute.plan",
    "insight.model.call",
    "artifact.write",
    "athena.query",
)
PROMOTER_PASS_SPAN: Final = "execution_graph.promoter_pass"
PROMOTER_DOMAIN_SPAN: Final = "execution_graph.promoter_domain"
_LEXICAL_PARENTS: Final = {
    "bake.source_pin": "bake.run",
    "bake.fan_out": "bake.run",
    "bake.phase": "bake.task",
    "insight.precompute.plan": "precompute.run",
    PROMOTER_DOMAIN_SPAN: PROMOTER_PASS_SPAN,
}


@dataclass(frozen=True, slots=True)
class TraceCohortExpectation:
    """The exact cohort-span cardinality derived from one direct cohort's own records."""

    cohort_revision: str
    run_id: str
    bake_task_keys: tuple[str, ...]
    """Roster `page#variant` keys: one `bake.task` each."""
    task_writes: tuple[tuple[str, bool], ...]
    """`(task_id, written)` per task summary: each task records one `plan`, one or more
    `query`/`render` rounds (equal counts; none for a `requestless_tasks` member), and one
    `write` only when it wrote."""
    artifact_writes: int
    """Written pairs (the summaries' `pairs` of written tasks): one `artifact.write` each."""
    artifact_bytes: tuple[int, ...]
    """Per written task, its summary's `written_bytes`: the sum of that task's write spans."""
    athena_queries: int
    """The summaries' `query_executions`: one `athena.query` span per execution."""
    athena_succeeded_ids: frozenset[str]
    """Every retained SUCCEEDED execution ID; each span's `query_id` must be one of them."""
    athena_bytes_scanned: int
    """The summaries' `scanned_bytes`: the sum of the query spans' `bytes_scanned`."""
    model_requests: int
    """Insight slots the cohort's precompute generated (cache misses): one model call each."""
    requestless_tasks: frozenset[str] = frozenset()
    """Tasks whose summary records an unavailable effect with no executed request and no
    query/render time: a `no_request` row plans, binds nothing and never opens a round, so it
    records exactly one `plan` and no other phase (d2-live-20261016-r2)."""


@dataclass(frozen=True, slots=True)
class TraceCompletenessEvidence:
    """Every expected cohort, every attempted promoter domain, and the retrieved spans."""

    cohorts: tuple[TraceCohortExpectation, ...]
    promoter_attempts: tuple[tuple[str, str, str], ...]
    """`(aws_request_id, domain, cohort_revision)` per attempted promoter-domain child."""
    spans: tuple[Mapping[str, object], ...]
    retrieval_complete: bool


def _span_id(span: Mapping[str, object]) -> str:
    return str(span.get("spanId") or "")


def _cohort_trace_failures(
    expected: TraceCohortExpectation, spans: Sequence[Mapping[str, object]]
) -> list[str]:
    cohort = expected.cohort_revision
    by_class = {
        name: [
            span
            for span in spans
            if span.get("name") == name and _attribute(span, "cohort_revision") == cohort
        ]
        for name in TRACE_COHORT_CLASSES
    }
    wanted = {
        "bake.run": 1,
        "bake.source_pin": 1,
        "bake.fan_out": 1,
        "bake.task": len(expected.bake_task_keys),
        # The runtime finalizes twice: `graph_bake_precompute_armed`, then `graph_bake_finalized`.
        "bake.finalize": 2,
        "precompute.run": 1,
        "insight.precompute.plan": 1,
        "insight.model.call": expected.model_requests,
        "artifact.write": expected.artifact_writes,
        "athena.query": expected.athena_queries,
    }
    failures = [
        f"{cohort}:{name}: {len(by_class[name])} spans, expected {count}"
        for name, count in wanted.items()
        if len(by_class[name]) != count
    ]
    for name, members in by_class.items():
        for span in members:
            for field_name in ("run_id", "task_id", "cohort_revision"):
                value = _attribute(span, field_name)
                if not isinstance(value, str) or not value:
                    failures.append(f"{cohort}:{name}: a span has no {field_name}")
            if _attribute(span, "run_id") != expected.run_id:
                failures.append(f"{cohort}:{name}: a span names another run")
    ids = {name: {_span_id(span) for span in by_class[name]} for name in TRACE_COHORT_CLASSES}
    for child, parent in _LEXICAL_PARENTS.items():
        if child in by_class:
            for span in by_class[child]:
                if str(span.get("parentSpanId") or "") not in ids[parent]:
                    failures.append(f"{cohort}:{child}: wrong parent")
    # A phase belongs to its own task: one reparented under another task's span is not counted
    # as that task's record (each task's phase count alone would not see the move).
    task_of = {_span_id(span): _attribute(span, "task_id") for span in by_class["bake.task"]}
    for span in by_class["bake.phase"]:
        owner = task_of.get(str(span.get("parentSpanId") or ""))
        if owner is not None and owner != _attribute(span, "task_id"):
            failures.append(f"{cohort}:bake.phase: a phase under another task")
    tasks = sorted(
        f"{_attribute(span, 'page')}#{_attribute(span, 'variant')}"
        for span in by_class["bake.task"]
    )
    if tasks != sorted(expected.bake_task_keys):
        failures.append(f"{cohort}:bake.task: the task roster differs")
    failures.extend(_phase_failures(cohort, expected, by_class["bake.phase"]))
    per_task: dict[str, int] = {}
    for span in by_class["artifact.write"]:
        task = str(_attribute(span, "task_id"))
        per_task[task] = per_task.get(task, 0) + int(cast("int", _attribute(span, "bytes") or -1))
    if sorted(per_task.values()) != sorted(expected.artifact_bytes):
        failures.append(f"{cohort}:artifact.write: written bytes differ")
    queries = by_class["athena.query"]
    ids = [str(_attribute(span, "query_id")) for span in queries]
    if len(set(ids)) != len(ids) or not set(ids) <= expected.athena_succeeded_ids:
        failures.append(f"{cohort}:athena.query: query ids differ")
    scanned = sum(int(cast("int", _attribute(span, "bytes_scanned") or 0)) for span in queries)
    if scanned != expected.athena_bytes_scanned or any(
        _attribute(span, "state") != "SUCCEEDED" or _attribute(span, "statistics_unavailable")
        for span in queries
    ):
        failures.append(f"{cohort}:athena.query: statistics differ")
    finalized = [
        span
        for span in by_class["bake.finalize"]
        if _attribute(span, "outcome") == BAKE_FINALIZE_SUCCEEDED
    ]
    if len(finalized) != 1:
        failures.append(f"{cohort}:bake.finalize: not exactly one successful terminal")
    armed = [
        s for s in by_class["bake.finalize"] if _attribute(s, "outcome") == BAKE_FINALIZE_ARMED
    ]
    if len(armed) != 1:
        failures.append(f"{cohort}:bake.finalize: not exactly one precompute arming")
    return failures


def _phase_failures(
    cohort: str, expected: TraceCohortExpectation, phases: Sequence[Mapping[str, object]]
) -> list[str]:
    """Per task: one `plan`, equal `query`/`render` rounds, `write` iff written.

    A task records at least one round unless its summary marks it requestless, which records
    none: no executed request means no measurement batch, so no round opens.
    """
    counts: dict[tuple[str, str], int] = {}
    for span in phases:
        key = (str(_attribute(span, "task_id")), str(_attribute(span, "phase")))
        counts[key] = counts.get(key, 0) + 1
    failures: list[str] = []
    tasks = dict(expected.task_writes)
    if {task for task, _ in counts} - set(tasks):
        failures.append(f"{cohort}:bake.phase: a phase names another task")
    if {phase for _, phase in counts} - {"plan", "query", "render", "write"}:
        failures.append(f"{cohort}:bake.phase: an undeclared phase")
    for task, written in tasks.items():
        rounds = counts.get((task, "query"), 0)
        requestless = task in expected.requestless_tasks
        if (
            counts.get((task, "plan"), 0) != 1
            or (rounds != 0 if requestless else rounds < 1)
            or counts.get((task, "render"), 0) != rounds
            or counts.get((task, "write"), 0) != (1 if written else 0)
        ):
            failures.append(f"{cohort}:bake.phase: task {task} phases differ")
    return failures


def trace_completeness_verdict(evidence: TraceCompletenessEvidence) -> dict[str, object]:
    """Exact terminal cardinality, correlation, parents, bytes and statistics (criterion 6)."""
    failures: list[str] = []
    if not evidence.retrieval_complete:
        failures.append("the span retrieval is incomplete")
    if not evidence.cohorts:
        failures.append("no direct cohort is expected")
    for span in evidence.spans:
        if not span.get("endTimeUnixNano"):
            failures.append(f"{span.get('name')}: a nonterminal span")
        if span.get("name") in TRACE_COHORT_CLASSES and not _attribute(span, "cohort_revision"):
            failures.append(f"{span.get('name')}: a cohort span has no cohort_revision")
    for expected in evidence.cohorts:
        failures.extend(_cohort_trace_failures(expected, evidence.spans))
    passes = [span for span in evidence.spans if span.get("name") == PROMOTER_PASS_SPAN]
    children = [span for span in evidence.spans if span.get("name") == PROMOTER_DOMAIN_SPAN]
    for request_id in sorted({attempt[0] for attempt in evidence.promoter_attempts}):
        owned = [span for span in passes if _attribute(span, "aws_request_id") == request_id]
        if len(owned) != 1:
            failures.append(f"{request_id}: {len(owned)} promoter passes, expected exactly 1")
            continue
        for _, domain, cohort in (a for a in evidence.promoter_attempts if a[0] == request_id):
            matched = [
                span
                for span in children
                if _attribute(span, "aws_request_id") == request_id
                and _attribute(span, "domain") == domain
                and _attribute(span, "cohort_revision") == cohort
            ]
            if len(matched) != 1:
                failures.append(f"{request_id}:{domain}:{cohort}: {len(matched)} domain children")
            elif str(matched[0].get("parentSpanId") or "") != _span_id(owned[0]):
                failures.append(f"{request_id}:{domain}:{cohort}: domain child has a wrong parent")
            elif not _attribute(matched[0], "outcome"):
                failures.append(f"{request_id}:{domain}:{cohort}: domain child has no outcome")
    expected_children = len(evidence.promoter_attempts)
    if len(children) != expected_children:
        failures.append(f"{len(children)} promoter-domain children, expected {expected_children}")
    return {"failures": failures, "passed": not failures}


# Athena admission configuration (criterion 7).

ATHENA_STATIC_CALLER_ROWS: Final = {
    "baker": 20,
    "table_writer": 8,
    "inline_ingestion": 2,
    "provider_attempt_reconciler": 1,
    "ingest_operator": 1,
    "reprocess_operator": 1,
    "insight_precompute": 8,
}
ATHENA_ACTIVE_DML_QUOTA: Final = 200
ATHENA_CHAT_RESERVE: Final = 20
ATHENA_ACCEPTED_BATCH_ADMISSION: Final = 8


def _whole(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def athena_admission_config_verdict(
    receipt: Mapping[str, object],
    measurement: Mapping[str, object] | None = None,
    measurement_bytes: bytes | None = None,
) -> dict[str, object]:
    """Explicit 8 everywhere, or a digest-bound `AthenaAdmissionMeasurementV1` for another value.

    `receipt` carries `source` ("explicit_env"), `configured`, `synthesized`/`deployed`/
    `readback` maps, `static_rows`, `active_dml_quota`, `chat_reserve`, and for a non-8 value
    `measurement_sha256` plus the code/environment/workgroup/quota identities.
    """
    reasons: list[str] = []
    configured = _whole(receipt.get("configured"))
    if configured is None:
        reasons.append("absent")
    if receipt.get("source") != "explicit_env":
        reasons.append("implicit_fallback")
    observed: list[object] = [receipt.get("configured")]
    for name in ("synthesized", "deployed", "readback"):
        values = receipt.get(name)
        if not isinstance(values, Mapping) or not values:
            reasons.append(f"{name}_missing")
            continue
        observed.extend(cast("Mapping[str, object]", values).values())
    if configured is not None and any(_whole(value) != configured for value in observed):
        reasons.append("deployed_readback_mismatch")
    if (
        receipt.get("static_rows") != ATHENA_STATIC_CALLER_ROWS
        or receipt.get("active_dml_quota") != ATHENA_ACTIVE_DML_QUOTA
        or receipt.get("chat_reserve") != ATHENA_CHAT_RESERVE
    ):
        reasons.append("static_rows_or_reserve_changed")
    if configured is not None and configured != ATHENA_ACCEPTED_BATCH_ADMISSION:
        proven = (
            measurement is not None
            and measurement_bytes is not None
            and hashlib.sha256(measurement_bytes).hexdigest() == receipt.get("measurement_sha256")
            and measurement.get("schema_version") == "athena-admission-measurement-v1"
            and all(
                measurement.get(name) == receipt.get(name)
                for name in ("code_sha", "environment", "workgroup", "active_dml_quota")
            )
            and measurement.get("tested_value") == configured
            and measurement.get("conclusion") == "replacement_admitted"
            and measurement.get("static_rows") == ATHENA_STATIC_CALLER_ROWS
            and measurement.get("chat_reserve") == ATHENA_CHAT_RESERVE
        )
        if not proven:
            reasons.append("unproven_non_8")
    return {
        "value": configured,
        "reasons": reasons,
        "passed": not reasons,
        "disposition": "PASS" if not reasons else "NOT_PROVEN",
    }


# Paired governed chat (criterion 8).

CHAT_SAMPLE_SCHEMA: Final = "chat-latency-sample-v1"
CHAT_SAMPLE_COUNT: Final = 6
CHAT_P95_LIMIT_PERCENT: Final = 110
_CHAT_IDENTITY: Final = (
    "acceptance_execution_id",
    "environment",
    "account",
    "origin",
    "persona_id",
    "prompt_manifest_sha256",
    "sample_count",
    "accepted_generation",
    "generation_unit",
    "target_bake_run_id",
    "target_cohort_revision",
    "bake_run_started_at",
    "bake_finalize_ended_at",
    "outside_list_item",
)


def nearest_rank_p95(values: Sequence[int]) -> int:
    """The repository's exact nearest-rank rule: `sorted(values)[ceil(0.95 * n) - 1]`."""
    if not values:
        raise ProbeError("p95 needs at least one value")
    ordered = sorted(values)
    return ordered[-(-95 * len(ordered) // 100) - 1]


def _chat_rows(sample: Mapping[str, object], phase: str) -> list[Mapping[str, object]]:
    if sample.get("schema_version") != CHAT_SAMPLE_SCHEMA or sample.get("phase") != phase:
        raise ProbeError(f"the {phase} sample has the wrong schema or phase")
    for name in _CHAT_IDENTITY:
        if sample.get(name) in (None, ""):
            raise ProbeError(f"the {phase} sample carries no {name}")
    rows = sample.get("rows")
    if not isinstance(rows, list) or len(cast("list[object]", rows)) != CHAT_SAMPLE_COUNT:
        raise ProbeError(f"the {phase} sample does not hold exactly six rows")
    typed = cast("list[Mapping[str, object]]", rows)
    ids = [row.get("prompt_id") for row in typed]
    if len(set(ids)) != len(ids) or any(not isinstance(item, str) or not item for item in ids):
        raise ProbeError(f"the {phase} sample has missing or duplicate prompt ids")
    for row in typed:
        start = utc_instant(row.get("request_started_at"), f"{phase} row start")
        end = utc_instant(row.get("request_ended_at"), f"{phase} row end")
        latency = _whole(row.get("latency_ms"))
        if end <= start or latency is None or latency <= 0:
            raise ProbeError(f"the {phase} row {row.get('prompt_id')} has an invalid interval")
        if latency != (end - start) // timedelta(milliseconds=1):
            raise ProbeError(f"the {phase} row {row.get('prompt_id')} latency is not end - start")
    return typed


def _governed(row: Mapping[str, object], generation: object) -> bool:
    return (
        row.get("terminal_class") == "answer"
        and row.get("governed_answer") is True
        and row.get("governed_generation") == generation
        and row.get("http_status") == 200  # noqa: PLR2004 -- the one success status
        and row.get("auth_outcome") == "authenticated"
        and row.get("quota_refused") is False
        and row.get("throttled") is False
    )


def paired_bake_chat_verdict(
    baseline: Mapping[str, object],
    during_bake: Mapping[str, object],
    accepted_generation: str,
    outside_list_item: str,
    bake_interval: tuple[str, str],
    *,
    used_execution_ids: frozenset[str] = frozenset(),
) -> dict[str, object]:
    """Exact nearest-rank paired p95 under one execution, ACTIVE launch, and bake overlap."""
    reasons: list[str] = []
    try:
        base_rows = _chat_rows(baseline, "paired_no_bake")
        load_rows = _chat_rows(during_bake, "during_bake")
        start = utc_instant(bake_interval[0], "bake.run start")
        end = utc_instant(bake_interval[1], "final bake.finalize end")
        if end < start:
            raise ProbeError("the bake interval is reversed")
        for sample in (baseline, during_bake):
            if (
                utc_instant(sample["bake_run_started_at"], "bake start") != start
                or utc_instant(sample["bake_finalize_ended_at"], "bake end") != end
            ):
                reasons.append("bake_interval_mismatch")
        if any(baseline.get(name) != during_bake.get(name) for name in _CHAT_IDENTITY):
            reasons.append("identity_mismatch")
        if [row["prompt_id"] for row in base_rows] != [row["prompt_id"] for row in load_rows]:
            reasons.append("prompt_mismatch")
        if baseline.get("accepted_generation") != accepted_generation:
            reasons.append("generation_mismatch")
        if baseline.get("outside_list_item") != outside_list_item:
            reasons.append("outside_list_item_mismatch")
        if str(baseline.get("acceptance_execution_id")) in used_execution_ids:
            reasons.append("execution_id_reused")
        preflight = utc_instant(baseline.get("preflight_completed_at"), "preflight")
        launch = utc_instant(baseline.get("first_root_launch_at"), "first launch")
        if not (
            utc_instant(baseline.get("sample_started_at"), "baseline start") >= preflight
            and utc_instant(baseline.get("sample_ended_at"), "baseline end") < launch
        ):
            reasons.append("baseline_outside_preflight_launch")
        for row in load_rows:
            receipt = row.get("head_active_receipt")
            if not (
                isinstance(receipt, Mapping)
                and cast("Mapping[str, object]", receipt).get("state") == "ACTIVE"
                and cast("Mapping[str, object]", receipt).get("cohort_revision")
                == during_bake.get("target_cohort_revision")
            ):
                reasons.append("during_launch_not_active")
                break
        if not any(
            utc_instant(row["request_started_at"], "row") <= end
            and utc_instant(row["request_ended_at"], "row") >= start
            for row in load_rows
        ):
            reasons.append("no_bake_overlap")
        # The bounded-list absence leg moved to D2 live acceptance (owner, 2026-10-15); GRAPH
        # criterion 8 keeps only the receipt-backed presence of the selected campaign.
        for sample in (baseline, during_bake):
            if sample.get("outside_list_present_in_generation") is not True:
                reasons.append("outside_list_unproven")
        if not all(_governed(row, accepted_generation) for row in (*base_rows, *load_rows)):
            reasons.append("ungoverned_or_failed_row")
        base_p95 = nearest_rank_p95([cast("int", row["latency_ms"]) for row in base_rows])
        load_p95 = nearest_rank_p95([cast("int", row["latency_ms"]) for row in load_rows])
    except (KeyError, ProbeError) as error:
        return {"reasons": [f"invalid:{error}"], "passed": False, "disposition": "NOT_PROVEN"}
    if load_p95 * 100 > base_p95 * CHAT_P95_LIMIT_PERCENT:
        reasons.append("p95_over_110_percent")
    return {
        "baseline_p95_ms": base_p95,
        "during_bake_p95_ms": load_p95,
        "reasons": reasons,
        "passed": not reasons,
        "disposition": "PASS" if not reasons else "NOT_PROVEN",
    }


# The closed historical migration matrix (criterion 9).

HOLD_CLEAR_MATRIX_PATH: Final = (
    _ROOT / "validation" / "fixtures" / "graph-hold-clear-migration-v1.json"
)
HOLD_CLEAR_MATRIX_IDS: Final = (
    "HIST-1",
    "HIST-2",
    "HIST-3",
    "HIST-4",
    "HIST-5",
    "HIST-6",
    "HIST-7",
    "HIST-8",
    "HIST-9",
    "HIST-10a",
    "HIST-10b",
    "HIST-11",
    "HIST-12",
    "HIST-12b",
    "HIST-13",
    "HIST-14",
    "HIST-15",
    "HIST-16",
    "HIST-17",
    "HIST-18",
    "HIST-19",
    "HIST-20",
)
_MATRIX_STATUSES: Final = frozenset({"retained", "replaced", "retired"})


def hold_clear_matrix_verdict(
    matrix: Mapping[str, object],
    evidence: Mapping[str, Mapping[str, object]],
    *,
    execution_id: str,
) -> dict[str, object]:
    """Every historical row exactly once; retained/replaced rows pass on this execution.

    A retired row needs its recorded replacement reason and no execution evidence.
    """
    reasons: list[str] = []
    rows = matrix.get("rows")
    if matrix.get("schema_version") != "graph-hold-clear-migration-v1" or not isinstance(
        rows, list
    ):
        return {"reasons": ["matrix_malformed"], "passed": False}
    typed = cast("list[Mapping[str, object]]", rows)
    ids = [row.get("criterion_id") for row in typed]
    duplicates = sorted({str(item) for item in ids if ids.count(item) > 1})
    unknown = sorted(str(item) for item in set(ids) - set(HOLD_CLEAR_MATRIX_IDS))
    missing = sorted(set(HOLD_CLEAR_MATRIX_IDS) - {str(item) for item in ids})
    reasons.extend(f"duplicate:{item}" for item in duplicates)
    reasons.extend(f"unknown:{item}" for item in unknown)
    reasons.extend(f"missing:{item}" for item in missing)
    extra = sorted(set(evidence) - set(HOLD_CLEAR_MATRIX_IDS))
    reasons.extend(f"unexpected_evidence:{item}" for item in extra)
    for row in typed:
        criterion, status = str(row.get("criterion_id")), row.get("status")
        if status not in _MATRIX_STATUSES:
            reasons.append(f"unknown_status:{criterion}")
            continue
        found = evidence.get(criterion)
        if status == "retired":
            reason = row.get("reason")
            if not isinstance(reason, str) or not reason.startswith("reason: "):
                reasons.append(f"invalid_retirement_reason:{criterion}")
            if found is not None:
                reasons.append(f"retired_row_executed:{criterion}")
            continue
        if found is None:
            reasons.append(f"evidence_absent:{criterion}")
        elif found.get("execution_id") != execution_id:
            reasons.append(f"evidence_other_execution:{criterion}")
        elif found.get("passed") is not True:
            reasons.append(f"evidence_failed:{criterion}")
    # Alarms a row's evidence places out of GRAPH scope are listed by row, with their states;
    # they never change `passed`, which their own reads already decided.
    scoped = {
        criterion: found["out_of_graph_scope"]
        for criterion, found in sorted(evidence.items())
        if "out_of_graph_scope" in found
    }
    return {"reasons": reasons, "passed": not reasons, "out_of_graph_scope": scoped}


def load_hold_clear_matrix(path: Path = HOLD_CLEAR_MATRIX_PATH) -> Mapping[str, object]:
    """Read the committed matrix fixture."""
    try:
        return cast("Mapping[str, object]", json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError) as error:
        raise ProbeError(f"the hold-clear matrix is unreadable: {path}") from error


def _retirement_scan(worktree: Path, evidence_dir: Path) -> int:
    """`--retirement-scan`: local and read-only; writes the receipt and returns its verdict."""
    document = runtime_admission_retirement(worktree).document()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"runtime-admission-retirement-{_utc_stamp()}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(f"evidence: {path}")
    return 0 if document["passed"] else 1


def _legacy_census(target: Target, arguments: argparse.Namespace) -> int:
    """`--legacy-census`: census A, strong inertness reads, census B; or a postdeploy readback.

    Read-only. With `--census-baseline` it takes one readback and compares it byte for byte.
    """
    if not arguments.store_stack:
        raise ProbeError("--store-stack is required for --legacy-census")
    session = _session(target)
    table = _store_table(session, target.environment, str(arguments.store_stack))
    evidence_dir = Path(arguments.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc_stamp()

    def write(name: str, document: Mapping[str, object]) -> None:
        path = evidence_dir / f"{name}-{stamp}.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
        print(f"evidence: {path}")

    try:
        census_a = gather_legacy_serving_census(session, table, region=target.region)
    except LegacyCensusError as error:
        write("legacy-census-invalid", error.partial)
        raise
    write("legacy-census-a", census_a.document())
    if arguments.census_baseline:
        baseline = _census_from_document(json.loads(Path(arguments.census_baseline).read_text()))
        verdict = legacy_serving_census_verdict(baseline, census_a)
        write("legacy-census-preservation", verdict)
        return 0 if verdict["passed"] else 1
    inertness = legacy_serving_inertness_verdict(
        census_a, read_legacy_serving_authority(session, census_a)
    )
    write("legacy-inertness", inertness.document())
    census_b = gather_legacy_serving_census(session, table, region=target.region)
    write("legacy-census-b", census_b.document())
    verdict = legacy_serving_cutover_verdict(census_a, inertness, census_b)
    write("legacy-cutover", verdict)
    return 0 if verdict["passed"] else 1


def _acceptance_verdicts(target: Target, arguments: argparse.Namespace, worktree: Path) -> int:
    """`--acceptance-verdicts`: read-only; one receipt per items 13-14 live verdict.

    `--evidence-dir` is the acceptance execution's directory holding its retained control
    receipt and chat samples; every receipt and collected artifact is written beside them.
    """
    # The outside-list item is derived from the execution's retained selection receipt.
    for name in ("acceptance_execution_id", "accepted_generation", "store_stack", "env_file"):
        if not getattr(arguments, name):
            raise ProbeError(f"--{name.replace('_', '-')} is required for --acceptance-verdicts")
    # Run as a script, this module is `__main__`; the gatherers import it by name, so both must
    # be one module object for one `ProbeError` to reach `main`'s handler.
    sys.modules.setdefault("probe_ingestion_traceability_gate", sys.modules[__name__])
    from probe_ingestion_traceability_acceptance import (
        VerdictInputs,
        admission_receipt,
        collect_trace_artifacts,
        deployed_templates,
        lambda_readback,
        operator_processes,
        parse_matrix_sources,
        run_acceptance_verdicts,
        synthesized_templates,
    )
    from probe_turn_latency import athena_query_readback

    evidence_dir = Path(arguments.evidence_dir)
    code_sha = _git(worktree, "rev-parse", "HEAD").strip()
    inputs = VerdictInputs(
        execution_id=str(arguments.acceptance_execution_id),
        evidence_dir=evidence_dir,
        accepted_generation=str(arguments.accepted_generation),
        env_file=Path(arguments.env_file),
        cdk_out=Path(arguments.cdk_out or worktree / "infra" / "cdk.out"),
        worktree=worktree,
        matrix_sources=parse_matrix_sources(arguments.matrix_evidence),
        code_sha=code_sha,
        environment=target.environment,
    )
    session = _session(target)
    table = _store_table(session, target.environment, str(arguments.store_stack))

    def admission() -> Mapping[str, object]:
        return admission_receipt(
            env_text=inputs.env_file.read_text(encoding="utf-8"),
            synthesized=synthesized_templates(inputs.cdk_out, target.environment),
            deployed=deployed_templates(session, target.environment),
            readback=lambda_readback(session, target.environment),
            operators=operator_processes(worktree),
            code_sha=code_sha,
            environment=target.environment,
            acceptance_execution_id=inputs.execution_id,
        )

    written = run_acceptance_verdicts(
        inputs,
        collect_trace=lambda control: collect_trace_artifacts(
            session, target.environment, table, control
        ),
        collect_admission=admission,
        collect_selection=lambda query: athena_query_readback(session.client("athena"), query),
        evidence_root=Path(arguments.evidence_root or evidence_dir.parent),
    )
    passed = True
    for name, path in written.items():
        document = json.loads(path.read_text())
        passed = passed and document["passed"] is True
        print(f"{name}: {document['disposition']} {path}")
    return 0 if passed else 1


def _census_from_document(document: Mapping[str, object]) -> LegacyServingCensusV1:
    """Rebuild a retained census artifact; a wrong schema is refused."""
    if document.get("schema_version") != LEGACY_CENSUS_SCHEMA:
        raise ProbeError("the census baseline has the wrong schema")
    try:
        return LegacyServingCensusV1(
            caller_arn=str(document["caller_arn"]),
            region=str(document["region"]),
            table_arn=str(document["table_arn"]),
            table_name=str(document["table_name"]),
            scan_started_at=str(document["scan_started_at"]),
            scan_ended_at=str(document["scan_ended_at"]),
            page_count=int(cast("int", document["page_count"])),
            scanned_count=int(cast("int", document["scanned_count"])),
            rows=tuple(cast("list[Mapping[str, object]]", document["rows"])),
            day_state_counts=cast("Mapping[str, int]", document["day_state_counts"]),
            day_missing_field_counts=cast(
                "Mapping[str, int]", document["day_missing_field_counts"]
            ),
            wait_state_counts=cast("Mapping[str, int]", document["wait_state_counts"]),
            wait_rollover_counts=cast("Mapping[str, int]", document["wait_rollover_counts"]),
            wait_missing_field_counts=cast(
                "Mapping[str, int]", document["wait_missing_field_counts"]
            ),
            wait_row_count=int(cast("int", document["wait_row_count"])),
            wait_keys=tuple(
                (str(pair[0]), str(pair[1]))
                for pair in cast("list[list[object]]", document["wait_keys"])
            ),
            authority_rows=tuple(cast("list[Mapping[str, object]]", document["authority_rows"])),
            canonical_length=int(cast("int", document["canonical_length"])),
            sha256=str(document["sha256"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ProbeError("the census baseline is malformed") from error


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--probe", choices=("P0", "P1", "P2", "P3", "P4", "P5", "P6"))
    mode.add_argument("--provision-persona", metavar="LABEL")
    mode.add_argument("--asset-scan", action="store_true")
    mode.add_argument("--record-assets", metavar="STACK")
    # D7 criterion 1 (local, read-only) and criterion 2 (read-only census/inertness/cutover).
    mode.add_argument("--retirement-scan", action="store_true")
    mode.add_argument("--legacy-census", action="store_true")
    # Items 13-14 (read-only): one receipt per live verdict from one execution's artifacts.
    mode.add_argument("--acceptance-verdicts", action="store_true")
    parser.add_argument("--acceptance-execution-id")
    parser.add_argument("--accepted-generation")
    parser.add_argument("--env-file")
    parser.add_argument("--cdk-out")
    parser.add_argument(
        "--evidence-root",
        help="--acceptance-verdicts: where earlier accepted receipts are searched for reuse.",
    )
    parser.add_argument(
        "--matrix-evidence",
        action="append",
        default=[],
        help="--acceptance-verdicts: HIST-<id>=<path> for a row with no verdict receipt.",
    )
    parser.add_argument("--profile")
    parser.add_argument("--env")
    parser.add_argument("--region")
    parser.add_argument("--expected-account")
    parser.add_argument("--evidence-dir", required=True)
    parser.add_argument("--worktree", default=str(_ROOT))
    parser.add_argument("--sha")
    parser.add_argument("--set-stack", action="append", default=[])
    parser.add_argument("--assets-file", action="append", default=[])
    parser.add_argument("--diff-file", action="append", default=[])
    parser.add_argument("--lagging-stack", action="append", default=[])
    parser.add_argument("--lagging-file", action="append", default=[])
    parser.add_argument("--bucket")
    parser.add_argument("--raw-stack")
    parser.add_argument("--operation-id")
    parser.add_argument("--wait-seconds", type=int, default=1500)
    parser.add_argument("--store-stack")
    parser.add_argument("--p0-evidence")
    parser.add_argument("--p1-evidence")
    parser.add_argument("--baseline-graph", action="append", default=[])
    parser.add_argument("--status-persona", default="ops-status-reader")
    parser.add_argument("--requester-persona")
    parser.add_argument("--non-member-persona", default="probe-a")
    parser.add_argument("--persona-group")
    parser.add_argument("--p2-evidence")
    parser.add_argument("--p3-evidence")
    parser.add_argument("--p4-evidence")
    parser.add_argument("--staffed-endpoint-sha256")
    # S6 part 1 (P6(a), P6(b0)): the step, the b0 start evidence and the coordinator table.
    parser.add_argument(
        "--p6-step", choices=("a", "b0-start", "b0-finish", "alarm-window", "pre-cas", "e")
    )
    # S6 part 2 (plan B8): P6(e)'s run, its span window and the daily-cycle window count.
    parser.add_argument(
        "--run-id",
        action="append",
        help="P6(e): one named observed-cohort run; repeat for the full burst.",
    )
    parser.add_argument("--window-start")
    parser.add_argument("--windows-per-cycle", type=int)
    parser.add_argument(
        "--table-writer-log-group",
        help="P6(e): the TableWriter log group the slice-7 batch records are read from.",
    )
    parser.add_argument("--p6-start")
    parser.add_argument("--staffed-notice")
    parser.add_argument("--window-end")
    parser.add_argument("--coordinator-table")
    parser.add_argument("--coordinator-stack")
    parser.add_argument(
        "--census-baseline",
        help="--legacy-census: a retained predeploy census; take one readback and compare it.",
    )
    return parser


def _target(arguments: argparse.Namespace) -> Target:
    for name in ("profile", "env", "region", "expected_account"):
        if not getattr(arguments, name):
            raise ProbeError(f"--{name.replace('_', '-')} is required")
    if str(arguments.profile).casefold() == "default":
        raise ProbeError("an explicit nondefault profile is required")
    if re.fullmatch(rf"\d{{{_ACCOUNT_DIGITS}}}", str(arguments.expected_account)) is None:
        raise ProbeError("--expected-account must be a 12-digit account id")
    return Target(
        profile=str(arguments.profile),
        environment=str(arguments.env),
        region=str(arguments.region),
        expected_account=str(arguments.expected_account),
        evidence_dir=Path(arguments.evidence_dir),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one mode and return nonzero on any failure."""
    try:
        arguments = _parser().parse_args(argv)
        evidence_dir = Path(arguments.evidence_dir)
        if not evidence_dir.is_absolute():
            raise ProbeError("--evidence-dir must be an absolute path")
        worktree = Path(arguments.worktree).resolve()
        if arguments.record_assets:
            stamp = _utc_stamp()
            recorded = record_assets(worktree, str(arguments.record_assets))
            recorded["sha"] = _git(worktree, "rev-parse", "HEAD").strip()
            evidence_dir.mkdir(parents=True, exist_ok=True)
            path = evidence_dir / f"{arguments.record_assets}-assets-{stamp}.json"
            path.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
            print(f"assets: {path}")
            return 0
        if arguments.retirement_scan:
            return _retirement_scan(worktree, evidence_dir)
        target = _target(arguments)
        if arguments.legacy_census:
            return _legacy_census(target, arguments)
        if arguments.acceptance_verdicts:
            return _acceptance_verdicts(target, arguments, worktree)
        if arguments.provision_persona:
            if not arguments.persona_group:
                raise ProbeError("--persona-group is required")
            evidence = Evidence(f"persona-{arguments.provision_persona}", _utc_stamp())
            evidence.document.update(
                provision_persona(
                    _session(target),
                    target.environment,
                    str(arguments.provision_persona),
                    str(arguments.persona_group),
                )
            )
            print(f"evidence: {evidence.write(target)}")
            return 0 if evidence.document["passed"] else 1
        if arguments.asset_scan:
            if not arguments.sha:
                raise ProbeError("--sha is required")
            evidence = Evidence("asset-scan", _utc_stamp())
            result = asset_scan(
                _session(target),
                AssetScanRequest(
                    worktree,
                    str(arguments.sha),
                    worktree / "build" / "src",
                    target.environment,
                    frozenset(arguments.set_stack),
                    frozenset(arguments.lagging_stack),
                    parse_lagging_files(arguments.lagging_file),
                ),
            )
            evidence.document.update(result)
            print(f"evidence: {evidence.write(target)}")
            return 0 if result["passed"] else 1
        for name in ("sha", "bucket", "raw_stack", "operation_id"):
            if not getattr(arguments, name):
                raise ProbeError(f"--{name.replace('_', '-')} is required for a probe")
        if arguments.probe == "P6":
            for name in ("store_stack", "p6_step"):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P6")
            if arguments.p6_step in {"pre-cas", "e"}:
                return _graph_acceptance(target, arguments)
            if arguments.p6_step == "b0-start" and not (
                arguments.coordinator_table and arguments.coordinator_stack
            ):
                raise ProbeError("b0-start needs --coordinator-table and --coordinator-stack")
            if arguments.p6_step == "b0-start" and not arguments.staffed_notice:
                raise ProbeError("b0-start needs --staffed-notice")
            if arguments.p6_step in {"b0-finish", "alarm-window"} and not arguments.p6_start:
                raise ProbeError(f"{arguments.p6_step} needs --p6-start")
            if not (arguments.set_stack and arguments.assets_file):
                raise ProbeError("P6 needs --set-stack and --assets-file (step-7 readback)")
            from probe_ingestion_traceability_p6 import probe_p6

            return probe_p6(target, arguments)
        if arguments.probe == "P5":
            for name in (
                "store_stack",
                "p1_evidence",
                "p2_evidence",
                "p3_evidence",
                "p4_evidence",
                "staffed_endpoint_sha256",
            ):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P5")
            if len(arguments.baseline_graph) < 1:
                raise ProbeError("--baseline-graph is required for P5")
            from probe_ingestion_traceability_p5 import probe_p5

            return probe_p5(target, arguments)
        if arguments.probe == "P4":
            for name in ("store_stack", "p1_evidence", "p2_evidence", "p3_evidence"):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P4")
            if len(arguments.baseline_graph) < 1:
                raise ProbeError("--baseline-graph is required for P4")
            from probe_ingestion_traceability_p4 import probe_p4

            return probe_p4(target, arguments)
        if arguments.probe == "P3":
            for name in ("store_stack", "p1_evidence", "p2_evidence"):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P3")
            if len(arguments.baseline_graph) < 1:
                raise ProbeError("--baseline-graph is required for P3")
            from probe_ingestion_traceability_p3 import probe_p3

            return probe_p3(target, arguments)
        if arguments.probe == "P2":
            for name in ("store_stack", "p1_evidence"):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P2")
            if len(arguments.baseline_graph) < 1:
                raise ProbeError("--baseline-graph is required for P2 (M-4)")
            return _probe_p2(target, arguments)
        if arguments.probe == "P1":
            for name in ("store_stack", "p0_evidence"):
                if not getattr(arguments, name):
                    raise ProbeError(f"--{name.replace('_', '-')} is required for P1")
            return _probe_p1(target, arguments)
        return _probe_p0(target, arguments)
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
