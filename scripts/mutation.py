#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Mutation coverage: one unit per run, recorded, and skipped while current.

    propose                                    print import-mapped tiers (mutation-priorities.yaml)
    run --unit U [--semantic PROPOSALS.yaml]   mutate one unit (make mutate UNIT=U [SEMANTIC=...])
    next [--n N]                               run the next N missing or stale (make mutate-next)
    status [--summary]                         coverage per tier (make mutation-status); report only
    verdict --unit U --id S-NN --verdict V     change one survivor's verdict in place
            [--test NODEID] [--reason TEXT]    (make mutation-verdict)

Those make targets are its only callers; .kiro/steering/meta-testing-mutation.md owns when they
run. mutation-priorities.yaml owns which units exist, their
tier and order, and the tests mapped to each. Python units are mutated with cosmic-ray, which
edits the source in place and restores it; TypeScript units with StrykerJS, which works in a
sandbox copy under the package's gitignored .stryker-tmp/.

docs/test-run-records.md owns the fingerprint, the coverage map and the proposals format. A run
writes a `mutation` run record through scripts/run_record.py and updates the gitignored
.runs/mutation/coverage-map.json. A unit whose map entry carries the digest of its current
fingerprint is skipped; there is deliberately no option that forces a rerun.

Exit codes: 0 done or skipped, 1 the baseline or the tool failed, 2 refused (usage, an unknown or
untested unit, a dirty tree, a unit that is not current for --semantic), 4 the tracked tree is
dirty after a run in a way this module will not repair.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import IO, Any, cast

import yaml

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent
PRIORITIES = "mutation-priorities.yaml"
STEERING = ".kiro/steering/meta-testing-mutation.md"
SEMANTIC_SECTION = "## Which track owns which mutation"
TIERS = ("T1", "T2", "T3")
KIND = "mutation"
MAP_NAME = "coverage-map.json"
WORKSHEETS = "worksheets"
MAP_SCHEMA = 1
VERDICTS = ("OPEN", "KILLED", "EQUIVALENT")
TRACKS = ("mechanical", "semantic")
STAMP = "%Y%m%dT%H%M%S.%fZ"
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_DIRTY = 4
# Inherited variables a mutation child must never see: the recorder's pytest report and progress
# paths (a nested pytest would write the outer files) and deployment selectors (infra tests run
# without).
SCRUBBED_PREFIXES = ("ADLYTICS_",)
SCRUBBED = ("RUN_RECORD_PYTEST_REPORT", "RUN_RECORD_PYTEST_PROGRESS")
# The static cosmic-ray config. Its sha256 is the python operator-config hash in the fingerprint,
# so any change here makes every python unit stale.
COSMIC_RAY_TEMPLATE = """[cosmic-ray]
module-path = {module_path}
timeout = 20.0
excluded-modules = []
test-command = {test_command}

[cosmic-ray.distributor]
name = "local"
"""
PACKAGES = {"frontend": "frontend", "infra": "infra"}
STRYKER_CONFIG = "stryker.config.json"
# T1 holds the frontend figure-formatting library; every other frontend source file is T2.
T1_FRONTEND = frozenset(
    {"frontend/lib/exact-format.ts", "frontend/lib/display-value.ts", "frontend/lib/change.ts"}
)
FRONTEND_SOURCE_DIRS = (
    "frontend/lib/",
    "frontend/components/",
    "frontend/app/",
    "frontend/features/",
)
TIER_DESCRIPTIONS = {
    "T1": (
        "Domain logic, inbound request handlers and the frontend figure-formatting library: a "
        "blind spot here can show a wrong figure or admit a wrong request."
    ),
    "T2": "Other frontend source, outbound adapters, jobs and the remaining adapters.",
    "T3": "CDK infrastructure under infra/lib (generated files excluded).",
}
PRIORITIES_HEADER = """\
# Mutation priorities: the units `make mutate-next` works through, in tier then file order.
# Authored, not generated. `uv run python scripts/mutation.py propose` printed the first version,
# mapping each unit to the tests that import it; review and edit it by hand from then on.
#
# Extend it: add `- unit: <path>` with `tests: [<test files that exercise it>]` under the tier
# the unit belongs to (T1 domain, inbound handlers and frontend figure formatting; T2 other
# frontend, outbound adapters and jobs; T3 infra/lib). `mutate: [<paths>]` is optional and
# defaults to the unit itself; a python unit mutates exactly one module. `tests: []` marks a
# unit as untested, which every mutation command reports and skips. Moving a unit between tiers
# or reordering never makes its coverage stale; changing its mutate or tests list does.
# docs/test-run-records.md (Mutation fingerprint) says what makes an entry stale.
"""
SURVIVING = frozenset({"survived", "no-coverage"})
_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_VITEST_FAILED = re.compile(r"FAIL\s+(\S+\.test\.tsx?)\s+>\s+(.+?)\s*$", re.MULTILINE)
_JEST_FAILED = re.compile(r"FAIL\s+(\S+\.test\.ts)\b")
_DOTTED = re.compile(r"(?:domain|adapters)(?:\.[a-z_][a-z0-9_]*)+")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", re.MULTILINE)
_TS_SPECIFIER = re.compile(
    r"""(?:\bfrom\s*|\bimport\s*|\b(?:import|require|vi\.mock|jest\.mock)\(\s*)['"]([^'"\n]+)['"]"""
)


def _sibling(name: str) -> ModuleType:
    """Load a script beside this one as a module, reusing an already loaded copy."""
    loaded = sys.modules.get(name)
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load scripts/{name}.py")
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


run_record = _sibling("run_record")


class MutationError(Exception):
    """A refusal: the input or the repository state does not allow the command."""


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def _git(root: Path, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    # A-SUBPROCESS-001: fixed git argv built in this module; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607 - git from PATH, as every gate script runs it
        cwd=root,
        input=stdin,
        capture_output=True,
        check=False,
    )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _obj(value: object) -> dict[str, Any]:
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}


def _seq(value: object) -> list[Any]:
    return cast("list[Any]", value) if isinstance(value, list) else []


def _child_env() -> dict[str, str]:
    """Return the environment a mutation child runs with (see SCRUBBED)."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUBBED and not k.startswith(SCRUBBED_PREFIXES)
    }


def _collapse(text: str, limit: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def unit_slug(unit: str) -> str:
    """Return the unit path as one file-name segment: separators to hyphens, extension dropped."""
    stem = unit.rsplit(".", 1)[0] if "." in Path(unit).name else unit
    return re.sub(r"[^A-Za-z0-9_-]+", "-", stem).strip("-")


# --------------------------------------------------------------------------------------------
# Priorities
# --------------------------------------------------------------------------------------------


def language_of(path: str) -> str:
    """Return `python`, `frontend` or `infra` from the path prefix."""
    if path.startswith("src/") and path.endswith(".py"):
        return "python"
    for prefix, language in (("frontend/", "frontend"), ("infra/", "infra")):
        if path.startswith(prefix) and path.endswith((".ts", ".tsx")):
            return language
    raise MutationError(f"{path}: not a src/ .py, frontend/ or infra/ .ts(x) path")


@dataclass(frozen=True)
class Unit:
    """One entry of mutation-priorities.yaml."""

    unit: str
    tier: str
    mutate: tuple[str, ...]
    tests: tuple[str, ...]

    @property
    def language(self) -> str:
        """The unit's language, from its path prefix."""
        return language_of(self.unit)


@dataclass(frozen=True)
class Priorities:
    """Every unit, in tier then file order."""

    units: tuple[Unit, ...]

    def find(self, name: str) -> Unit | None:
        """Return the unit named `name`, or None."""
        return next((u for u in self.units if u.unit == name), None)


def _string_list(value: object, where: str) -> tuple[str, ...]:
    items = _seq(value)
    if not isinstance(value, list) or not all(isinstance(i, str) and i for i in items):
        raise MutationError(f"{where}: must be a list of paths")
    if len(set(items)) != len(items):
        raise MutationError(f"{where}: lists a path twice")
    return tuple(cast("list[str]", items))


def parse_priorities(document: object) -> Priorities:
    """Validate a loaded mutation-priorities.yaml document and return its units."""
    top = _obj(document)
    if set(top) != {"version", "tiers"} or top.get("version") != 1:
        raise MutationError(f"{PRIORITIES}: needs exactly `version: 1` and `tiers`")
    tiers = _obj(top["tiers"])
    if list(tiers) != list(TIERS):
        raise MutationError(f"{PRIORITIES}: tiers must be exactly {', '.join(TIERS)}, in order")
    units: list[Unit] = []
    seen: set[str] = set()
    for tier in TIERS:
        body = _obj(tiers[tier])
        if set(body) != {"description", "units"} or not isinstance(body["description"], str):
            raise MutationError(f"{PRIORITIES}: {tier} needs a description and units")
        listed: object = body["units"]
        if not isinstance(listed, list):
            raise MutationError(f"{PRIORITIES}: {tier}.units must be a list")
        for index, raw in enumerate(_seq(cast("object", listed))):
            entry = _obj(raw)
            where = f"{PRIORITIES}: {tier}.units[{index}]"
            name = entry.get("unit")
            if not isinstance(name, str) or not name:
                raise MutationError(f"{where}: needs a unit path")
            if not set(entry) <= {"unit", "mutate", "tests"} or "tests" not in entry:
                raise MutationError(f"{where} ({name}): keys are unit, mutate (optional), tests")
            if name in seen:
                raise MutationError(f"{where}: {name} is listed twice")
            seen.add(name)
            language = language_of(name)
            mutate = _string_list(entry.get("mutate", [name]), f"{where}.mutate")
            if not mutate or any(language_of(p) != language for p in mutate):
                raise MutationError(f"{where}: every mutate path is a {language} source file")
            if language == "python" and len(mutate) != 1:
                raise MutationError(f"{where}: a python unit mutates exactly one module")
            tests = _string_list(entry["tests"], f"{where}.tests")
            units.append(Unit(name, tier, mutate, tests))
    return Priorities(tuple(units))


def load_priorities(path: Path) -> Priorities:
    """Read and validate mutation-priorities.yaml."""
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise MutationError(f"{path.name}: unreadable ({error})") from error
    return parse_priorities(document)


# --------------------------------------------------------------------------------------------
# propose: tiers and import-derived test mapping
# --------------------------------------------------------------------------------------------


def _is_generated(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return "generated" in parts[:-1] or name.startswith(("generated_", "generated-"))


def tier_of(path: str) -> str | None:  # noqa: PLR0911 - one return per D7 rule
    """Return the D7 tier of a tracked source path, or None when it is never a unit."""
    name = path.rsplit("/", 1)[-1]
    if _is_generated(path) or name == "__init__.py" or name.endswith(".d.ts"):
        return None
    if ".test." in name or "/test/" in path or "/e2e/" in path or path.startswith("tests/"):
        return None
    if path.endswith(".py") and path.startswith("src/"):
        if path.startswith("src/ports/"):
            return None
        if path.startswith(("src/domain/", "src/adapters/inbound/")):
            return "T1"
        if path.startswith("src/adapters/"):
            return "T2"
        return None
    if path.endswith((".ts", ".tsx")):
        if path in T1_FRONTEND:
            return "T1"
        if path.startswith(FRONTEND_SOURCE_DIRS):
            return "T2"
        if path.startswith("infra/lib/"):
            return "T3"
    return None


def module_of(path: str) -> str:
    """Return the import name of a src-layout module: src/domain/x/y.py -> domain.x.y."""
    return ".".join(Path(path[len("src/") :]).with_suffix("").parts)


def python_imports(text: str) -> set[str]:
    """Return every module name an absolute import in `text` may resolve to."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            # `from pkg import submodule` imports the submodule.
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and _DOTTED.fullmatch(node.value)
        ):
            # A module the test loads by name (importlib.import_module, a fresh reimport).
            names.add(node.value)
    return names


def ts_specifiers(text: str) -> set[str]:
    """Return the module specifiers of every import, export-from, require and mock in `text`."""
    return set(_TS_SPECIFIER.findall(text))


def resolve_ts(specifier: str, test_path: str, tracked: frozenset[str]) -> str | None:
    """Resolve an import specifier of `test_path` to a tracked file, or None (a package)."""
    if specifier.startswith("@/") and test_path.startswith("frontend/"):
        base = "frontend/" + specifier[2:]
    elif specifier.startswith(("./", "../")):
        base = os.path.normpath(str(Path(test_path).parent / specifier)).replace(os.sep, "/")
    else:
        return None
    stem = base[: -len(".js")] if base.endswith(".js") else base
    for candidate in (base, f"{stem}.ts", f"{stem}.tsx", f"{stem}/index.ts", f"{stem}/index.tsx"):
        if candidate in tracked:
            return candidate
    return None


def is_test_file(path: str) -> bool:
    """Whether `path` is a test file the mapping reads."""
    name = path.rsplit("/", 1)[-1]
    if path.startswith(("tests/unit/", "tests/contract/")):
        return name.startswith("test_") and name.endswith(".py")
    if path.startswith("frontend/test/"):
        return name.endswith((".test.ts", ".test.tsx"))
    # infra tests sit directly under infra/test/; infra/test/support/ holds helpers.
    return (
        path.startswith("infra/test/")
        and "/" not in path[len("infra/test/") :]
        and name.endswith(".test.ts")
    )


def test_targets(test_path: str, text: str, tracked: frozenset[str]) -> set[str]:
    """Return the tracked source files (python: module names) a test file imports."""
    if test_path.endswith(".py"):
        return python_imports(text)
    found = (resolve_ts(s, test_path, tracked) for s in ts_specifiers(text))
    return {f for f in found if f is not None}


def maps_to(unit: str, targets: set[str]) -> bool:
    """Whether a test whose imports are `targets` exercises `unit` directly."""
    if unit.endswith(".py"):
        return module_of(unit) in targets
    return unit in targets


def tracked_files(root: Path) -> frozenset[str]:
    """Return every path HEAD's index tracks."""
    out = _git(root, "ls-files", "-z")
    if out.returncode != 0:
        raise MutationError("git ls-files failed")
    return frozenset(p for p in out.stdout.decode().split("\0") if p)


def propose(root: Path) -> dict[str, Any]:
    """Return the mutation-priorities document for the tracked tree."""
    tracked = tracked_files(root)
    targets = {
        t: test_targets(t, (root / t).read_text(errors="replace"), tracked)
        for t in sorted(tracked)
        if is_test_file(t) and (root / t).is_file()
    }
    tiers: dict[str, Any] = {}
    for tier in TIERS:
        units: list[dict[str, Any]] = []
        for path in sorted(p for p in tracked if tier_of(p) == tier):
            mapped = [
                t for t, found in targets.items() if same_language(path, t) and maps_to(path, found)
            ]
            units.append({"unit": path, "tests": mapped})
        tiers[tier] = {"description": TIER_DESCRIPTIONS[tier], "units": units}
    return {"version": 1, "tiers": tiers}


def same_language(unit: str, test: str) -> bool:
    """Whether `test` belongs to the unit's language and package."""
    if unit.endswith(".py"):
        return test.endswith(".py")
    return test.split("/", 1)[0] == unit.split("/", 1)[0]


def render_priorities(document: dict[str, Any]) -> str:
    """Return the YAML text for `document`, with the authored-file header."""
    body = yaml.safe_dump(document, sort_keys=False, width=100, default_flow_style=False)
    return PRIORITIES_HEADER + body


# --------------------------------------------------------------------------------------------
# Fingerprint
# --------------------------------------------------------------------------------------------


def semantic_categories(steering_text: str) -> list[str]:
    """Return the semantic-category bullets of the steering section, whitespace-normalised."""
    lines = steering_text.splitlines()
    try:
        start = lines.index(SEMANTIC_SECTION)
    except ValueError as error:
        raise MutationError(f"{STEERING}: section {SEMANTIC_SECTION!r} not found") from error
    bullets: list[str] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if line.startswith("- "):
            bullets.append(line[2:].strip())
        elif bullets and line.startswith(" ") and line.strip():
            bullets[-1] += " " + line.strip()
        elif bullets and not line.strip():
            continue
    if not bullets:
        raise MutationError(f"{STEERING}: section {SEMANTIC_SECTION!r} lists no categories")
    return [" ".join(b.split()) for b in bullets]


def semantic_set_version(categories: Sequence[str]) -> str:
    """Return the first 12 hex of the sha256 of the category bullets."""
    return _sha256("\n".join(categories).encode())[:12]


def head_blobs(root: Path) -> dict[str, str]:
    """Return path -> blob id for every file in HEAD (`git rev-parse HEAD:<path>`, batched)."""
    out = _git(root, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    if out.returncode != 0:
        raise MutationError("git ls-tree HEAD failed: is there a commit?")
    blobs: dict[str, str] = {}
    for record in out.stdout.decode().split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            blobs[path] = meta.split()[2]
    return blobs


def tool_name(language: str) -> str:
    """Return the mutation tool of a language."""
    return "cosmic-ray" if language == "python" else "stryker"


def tool_version(root: Path, language: str) -> str:
    """Return the installed version of the language's mutation tool, or `not-installed`."""
    if language == "python":
        try:
            return metadata.version("cosmic-ray")
        except metadata.PackageNotFoundError:
            return "not-installed"
    manifest = root / PACKAGES[language] / "node_modules/@stryker-mutator/core/package.json"
    try:
        version = _obj(json.loads(manifest.read_text())).get("version")
    except OSError, ValueError:
        return "not-installed"
    return version if isinstance(version, str) else "not-installed"


def operator_config_sha256(root: Path, language: str, blobs: dict[str, str]) -> str:
    """Return the sha256 of the static cosmic-ray template or of the tracked stryker config."""
    if language == "python":
        return _sha256(COSMIC_RAY_TEMPLATE.encode())
    blob = blobs.get(f"{PACKAGES[language]}/{STRYKER_CONFIG}")
    if blob is None:
        return "absent"
    data = _git(root, "cat-file", "blob", blob)
    if data.returncode != 0:
        raise MutationError(f"cannot read {PACKAGES[language]}/{STRYKER_CONFIG} from HEAD")
    return _sha256(data.stdout)


@dataclass
class Inputs:
    """What every fingerprint in one invocation is computed from, read once."""

    root: Path
    blobs: dict[str, str]
    categories: list[str]
    _tools: dict[str, str] = field(default_factory=dict[str, str])
    _configs: dict[str, str] = field(default_factory=dict[str, str])

    def tool(self, language: str) -> str:
        """Return the tool version of `language` (cached)."""
        if language not in self._tools:
            self._tools[language] = tool_version(self.root, language)
        return self._tools[language]

    def config(self, language: str) -> str:
        """Return the operator-config hash of `language` (cached)."""
        if language not in self._configs:
            self._configs[language] = operator_config_sha256(self.root, language, self.blobs)
        return self._configs[language]


def read_inputs(root: Path, steering: Path) -> Inputs:
    """Read HEAD's blobs and the steering's semantic categories."""
    try:
        text = steering.read_text()
    except OSError as error:
        raise MutationError(f"{STEERING}: unreadable ({error})") from error
    return Inputs(root, head_blobs(root), semantic_categories(text))


def fingerprint(unit: Unit, inputs: Inputs) -> dict[str, Any]:
    """Return the fingerprint docs/test-run-records.md defines for `unit`."""

    def blobs_of(paths: Iterable[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for path in paths:
            blob = inputs.blobs.get(path)
            if blob is None:
                raise MutationError(f"{unit.unit}: {path} is not in HEAD")
            found[path] = blob
        return found

    language = unit.language
    return {
        "unit_blobs": blobs_of(unit.mutate),
        "test_blobs": blobs_of(unit.tests),
        "tool": {"name": tool_name(language), "version": inputs.tool(language)},
        "operator_config_sha256": inputs.config(language),
        "semantic_set_version": semantic_set_version(inputs.categories),
    }


def digest(print_: dict[str, Any]) -> str:
    """Return the sha256 of the fingerprint's canonical JSON."""
    return _sha256(json.dumps(print_, sort_keys=True, separators=(",", ":")).encode())


def unit_state(unit: Unit, entry: dict[str, Any] | None, current_digest: str | None) -> str:
    """Return `untested`, `missing`, `current` or `stale`."""
    if not unit.tests:
        return "untested"
    if entry is None:
        return "missing"
    return (
        "current"
        if current_digest is not None and entry.get("digest") == current_digest
        else "stale"
    )


# --------------------------------------------------------------------------------------------
# The coverage map
# --------------------------------------------------------------------------------------------


def map_path(runs: Path) -> Path:
    """Return the coverage map's path under the record root."""
    return runs / KIND / MAP_NAME


def load_map(path: Path) -> dict[str, Any]:
    """Return the coverage map, or an empty one when it does not exist yet."""
    if not path.exists():
        return {"schema": MAP_SCHEMA, "units": {}}
    try:
        document = _obj(json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise MutationError(f"{path}: unreadable coverage map ({error})") from error
    if document.get("schema") != MAP_SCHEMA or not isinstance(document.get("units"), dict):
        raise MutationError(f"{path}: not a schema {MAP_SCHEMA} coverage map")
    return document


def save_map(path: Path, document: dict[str, Any]) -> None:
    """Write the coverage map atomically: a temporary file beside it, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    temporary.replace(path)


def survivor_key(item: dict[str, Any]) -> tuple[str, str, str, str]:
    """Return the carry-forward key (track, file, operator, mutation text); lines move."""
    return (str(item["track"]), str(item["file"]), str(item["operator"]), str(item["mutation"]))


def group_survivors(mutants: Sequence[dict[str, Any]], track: str) -> list[dict[str, Any]]:
    """Group surviving mutants by (file, line, operator); the first mutation names the group."""
    groups: dict[tuple[str, int, str], dict[str, Any]] = {}
    for mutant in mutants:
        if mutant["status"] not in SURVIVING:
            continue
        key = (str(mutant["file"]), int(mutant["line"]), str(mutant["operator"]))
        group = groups.get(key)
        if group is None:
            groups[key] = {
                "track": track,
                "file": key[0],
                "line": key[1],
                "location": f"{key[0]}:{key[1]}",
                "operator": key[2],
                "mutation": mutant["mutation"],
                "status": mutant["status"],
                "count": 1,
            }
        else:
            group["count"] += 1
    return [groups[k] for k in sorted(groups)]


def _next_number(survivors: Sequence[dict[str, Any]]) -> int:
    numbers = [int(str(s["id"])[2:]) for s in survivors if re.fullmatch(r"S-\d+", str(s["id"]))]
    return max(numbers, default=0) + 1


def merge_survivors(  # noqa: PLR0913 - the merge's inputs, passed by keyword
    prior: Sequence[dict[str, Any]],
    groups: Sequence[dict[str, Any]],
    mutants: Sequence[dict[str, Any]],
    *,
    track: str,
    record: str,
    at: str,
    scope: set[tuple[str, str, str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Return the entry's survivors after this run; ids and verdicts carry forward, never renumber.

    `scope` limits which earlier survivors of `track` this run speaks for (a semantic run speaks
    only for the proposals it executed); None means every survivor of the track.
    """
    survivors = [json.loads(json.dumps(s)) for s in prior]
    by_key = {survivor_key(s): s for s in survivors}
    killers: dict[tuple[str, str, str, str], str | None] = {}
    for mutant in mutants:
        if mutant["status"] == "killed":
            killers.setdefault(survivor_key({**mutant, "track": track}), mutant.get("killed_by"))
    number = _next_number(survivors)
    seen: set[tuple[str, str, str, str]] = set()
    for group in groups:
        key = survivor_key(group)
        seen.add(key)
        entry = by_key.get(key)
        if entry is None:
            entry = {
                "id": f"S-{number:02d}",
                "track": track,
                "file": group["file"],
                "location": group["location"],
                "operator": group["operator"],
                "mutation": group["mutation"],
                "count": group["count"],
                "verdict": "OPEN",
                "present": True,
                "first_seen": record,
                "last_seen": record,
                "history": [{"at": at, "record": record, "verdict": "OPEN", "note": "survived"}],
            }
            if group.get("status") == "no-coverage":
                entry["note"] = "no test covers this mutant"
            number += 1
            survivors.append(entry)
            by_key[key] = entry
            continue
        entry.update(location=group["location"], count=group["count"], present=True)
        entry["last_seen"] = record
        if entry["verdict"] == "KILLED":
            entry["verdict"] = "OPEN"
            entry.pop("test", None)
            entry["history"].append(
                {
                    "at": at,
                    "record": record,
                    "verdict": "OPEN",
                    "note": "survived again: the test its KILLED verdict named no longer kills it",
                }
            )
    for entry in survivors:
        key = survivor_key(entry)
        if entry["track"] != track or key in seen or (scope is not None and key not in scope):
            continue
        was_present = bool(entry.get("present"))
        entry["present"] = False
        if entry["verdict"] != "OPEN":
            continue
        if key in killers:
            entry["verdict"] = "KILLED"
            entry["test"] = killers[key] or "unparsed"
            entry["history"].append(
                {"at": at, "record": record, "verdict": "KILLED", "test": entry["test"]}
            )
        elif was_present:
            entry["history"].append(
                {
                    "at": at,
                    "record": record,
                    "verdict": "OPEN",
                    "note": "not generated by this run (the source changed); stays OPEN",
                }
            )
    return survivors


def set_verdict(  # noqa: PLR0913, PLR0917 - the command's arguments
    root: Path,
    entry: dict[str, Any],
    survivor_id: str,
    verdict: str,
    *,
    test: str | None,
    reason: str | None,
    at: str,
) -> None:
    """Change one survivor's verdict in place and append the change to its history."""
    if verdict not in VERDICTS:
        raise MutationError(f"verdict must be one of {', '.join(VERDICTS)}")
    survivor = next((s for s in _seq(entry.get("survivors")) if s.get("id") == survivor_id), None)
    if survivor is None:
        raise MutationError(f"{entry.get('unit')}: no survivor {survivor_id}")
    if verdict == "KILLED":
        if not test:
            raise MutationError("KILLED names the test that kills it: give --test <node id>")
        if not (root / test.split("::", 1)[0]).is_file():
            raise MutationError(f"--test {test}: its file does not exist")
    if verdict == "EQUIVALENT" and not reason:
        raise MutationError("EQUIVALENT states why the mutation cannot change behaviour: --reason")
    survivor["verdict"] = verdict
    survivor.pop("test", None)
    survivor.pop("reason", None)
    change: dict[str, Any] = {"at": at, "verdict": verdict, "note": "set by mutation-verdict"}
    if verdict == "KILLED":
        survivor["test"] = change["test"] = test
    if verdict == "EQUIVALENT":
        survivor["reason"] = change["reason"] = reason
    survivor["history"].append(change)


# --------------------------------------------------------------------------------------------
# Tool output parsing
# --------------------------------------------------------------------------------------------


def first_failed_test(output: str) -> str | None:
    """Return the first FAILED or ERROR pytest node ID in `output`."""
    match = _PYTEST_FAILED.search(output)
    return match.group(1) if match else None


def mutation_from_diff(diff: str) -> str:
    """Return "`<original>` becomes `<mutated>`" from a unified diff's changed lines."""
    removed: list[str] = []
    added: list[str] = []
    for line in diff.splitlines():
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("-"):
            removed.append(line[1:].strip())
        elif line.startswith("+"):
            added.append(line[1:].strip())
    return f"`{_collapse(' / '.join(removed))}` becomes `{_collapse(' / '.join(added))}`"


def parse_cosmic_ray_dump(text: str, root: Path) -> list[dict[str, Any]]:
    """Return one result per job of `cosmic-ray dump` (JSON lines of [work item, result])."""
    mutants: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        item, result = json.loads(line)
        result = _obj(result) if result is not None else None
        for raw in _seq(_obj(item).get("mutations")):
            mutation = _obj(raw)
            module = Path(str(mutation.get("module_path", "")))
            try:
                file = module.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                file = module.as_posix()
            operator = str(mutation.get("operator_name"))
            if result is None:
                status, text_of, killed_by = (
                    "error",
                    f"{operator} occurrence {mutation.get('occurrence')}",
                    None,
                )
            else:
                output = str(result.get("output") or "")
                outcome = result.get("test_outcome")
                if result.get("worker_outcome") != "normal" or outcome == "incompetent":
                    status = "error"
                elif outcome == "killed":
                    status = "timeout" if output.strip() == "timeout" else "killed"
                elif outcome == "survived":
                    status = "survived"
                else:
                    status = "error"
                text_of = mutation_from_diff(str(result.get("diff") or ""))
                killed_by = first_failed_test(output) if status == "killed" else None
            mutants.append(
                {
                    "file": file,
                    "line": int(_seq(mutation.get("start_pos"))[0]),
                    "operator": operator,
                    "mutation": text_of,
                    "status": status,
                    "killed_by": killed_by,
                }
            )
    return mutants


_STRYKER_STATUS = {
    "Killed": "killed",
    "Survived": "survived",
    "NoCoverage": "no-coverage",
    "Timeout": "timeout",
    "CompileError": "error",
    "RuntimeError": "error",
    "Pending": "error",
}


def _slice(source: str, location: dict[str, Any]) -> str:
    """Return the source text between 1-based start and end positions (end exclusive)."""
    lines = source.splitlines()
    start, end = _obj(location.get("start")), _obj(location.get("end"))
    first, last = int(start.get("line", 1)) - 1, int(end.get("line", 1)) - 1
    if not 0 <= first <= last < len(lines):
        return ""
    if first == last:
        return lines[first][int(start["column"]) - 1 : int(end["column"]) - 1]
    chunk = [lines[first][int(start["column"]) - 1 :], *lines[first + 1 : last]]
    chunk.append(lines[last][: int(end["column"]) - 1])
    return "\n".join(chunk)


def parse_stryker_report(report: dict[str, Any], package: str) -> list[dict[str, Any]]:
    """Return one result per mutant of a StrykerJS json report (paths relative to `package`)."""
    names: dict[str, str] = {}
    for test_file, body in _obj(report.get("testFiles")).items():
        for raw in _seq(_obj(body).get("tests")):
            test = _obj(raw)
            names[str(test.get("id"))] = f"{package}/{test_file}::{test.get('name')}"
    mutants: list[dict[str, Any]] = []
    for file, raw_body in _obj(report.get("files")).items():
        body = _obj(raw_body)
        source = str(body.get("source", ""))
        for raw in _seq(body.get("mutants")):
            mutant = _obj(raw)
            status = _STRYKER_STATUS.get(str(mutant.get("status")))
            if status is None:  # Ignored: never run, so never counted
                continue
            location = _obj(mutant.get("location"))
            killed = [str(t) for t in _seq(mutant.get("killedBy"))]
            original = _collapse(_slice(source, location))
            replacement = _collapse(str(mutant.get("replacement")))
            mutants.append(
                {
                    "file": f"{package}/{file}",
                    "line": int(_obj(location.get("start")).get("line", 0)),
                    "operator": str(mutant.get("mutatorName")),
                    "mutation": f"`{original}` becomes `{replacement}`",
                    "status": status,
                    "killed_by": names.get(killed[0], killed[0])
                    if killed and status == "killed"
                    else None,
                }
            )
    return mutants


def counts_of(mutants: Sequence[dict[str, Any]]) -> dict[str, int]:
    """Return the generated, killed, survived, timeout, no-coverage and error counts."""
    statuses = [m["status"] for m in mutants]
    return {
        "generated": len(statuses),
        "killed": statuses.count("killed"),
        "survived": statuses.count("survived"),
        "timeout": statuses.count("timeout"),
        "no_coverage": statuses.count("no-coverage"),
        "errors": statuses.count("error"),
    }


def killing_test(language: str, output: str) -> str | None:
    """Return the first failing test a test run names (node ID, or file for jest)."""
    if language == "python":
        return first_failed_test(output)
    if language == "frontend":
        match = _VITEST_FAILED.search(output)
        return f"frontend/{match.group(1)}::{match.group(2)}" if match else None
    match = _JEST_FAILED.search(output)
    return f"infra/{match.group(1)}" if match else None


# --------------------------------------------------------------------------------------------
# Runners (injectable, so the tests drive the skip logic without a tool)
# --------------------------------------------------------------------------------------------


@dataclass
class ToolRun:
    """What the mutation tool produced for one unit."""

    exit_code: int
    console: bytes
    mutants: list[dict[str, Any]]
    files: dict[str, bytes]
    argv: list[str]
    duration_s: float
    # True when the tool's own report (the cosmic-ray dump, Stryker's mutation.json) is absent.
    report_missing: bool = False


def _rel(unit: Unit, paths: Iterable[str]) -> list[str]:
    """Return `paths` relative to the unit's package directory (TypeScript)."""
    prefix = PACKAGES[unit.language] + "/"
    return [p[len(prefix) :] for p in paths]


def test_argv(unit: Unit) -> tuple[list[str], str]:
    """Return the argv and cwd (relative to the root) that run the unit's mapped tests."""
    if unit.language == "python":
        return [
            sys.executable,
            "-m",
            "pytest",
            *unit.tests,
            "-q",
            "-x",
            "-p",
            "no:cacheprovider",
        ], "."
    if unit.language == "frontend":
        return ["npx", "--no-install", "vitest", "run", *_rel(unit, unit.tests)], "frontend"
    return ["npx", "--no-install", "jest", *_rel(unit, unit.tests)], "infra"


def run_tests(unit: Unit, root: Path) -> tuple[int, bytes]:
    """Run the unit's mapped tests, streamed and captured."""
    argv, cwd = test_argv(unit)
    return cast("tuple[int, bytes]", run_record.run_child(argv, root / cwd, _child_env()))


def cosmic_ray_config(unit: Unit, root: Path) -> str:
    """Return the per-run cosmic-ray config text."""
    command = " ".join(
        shlex.quote(a)
        for a in ["python", "-m", "pytest", *unit.tests, "-q", "-x", "-p", "no:cacheprovider"]
    )
    return COSMIC_RAY_TEMPLATE.format(
        module_path=json.dumps(str((root / unit.mutate[0]).resolve())),
        test_command=json.dumps(command),
    )


def run_cosmic_ray(unit: Unit, root: Path, staging: Path) -> ToolRun:
    """Init and exec a cosmic-ray session in `staging`, then read it with `cosmic-ray dump`."""
    config, session = staging / "cosmic-ray.toml", staging / "session.sqlite"
    config.write_text(cosmic_ray_config(unit, root))
    tool = ["uv", "run", "--frozen", "cosmic-ray"]
    env = _child_env()
    started = time.monotonic()
    console: list[bytes] = []
    code = 0
    for step in (["init", str(config), str(session)], ["exec", str(config), str(session)]):
        code, log = run_record.run_child([*tool, *step], root, env)
        console.append(log)
        if code != 0:
            break
    dumped = _capture_run([*tool, "dump", str(session)], root, env) if code == 0 else (code, b"")
    duration = round(time.monotonic() - started, 6)
    files = {"raw/cosmic-ray.toml": config.read_bytes(), "raw/cosmic-ray-dump.jsonl": dumped[1]}
    if session.is_file():
        files["raw/session.sqlite"] = session.read_bytes()
    mutants = (
        parse_cosmic_ray_dump(dumped[1].decode(errors="replace"), root) if dumped[0] == 0 else []
    )
    return ToolRun(
        code or dumped[0],
        b"".join(console),
        mutants,
        files,
        [*tool, "exec", "<staging>"],
        duration,
        report_missing=dumped[0] != 0,
    )


def _capture_run(argv: list[str], cwd: Path, env: dict[str, str]) -> tuple[int, bytes]:
    """Run a fixed tool argv and capture stdout (not streamed)."""
    # A-SUBPROCESS-001: fixed tool argv built in this module; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    result = subprocess.run(  # noqa: S603
        argv, cwd=cwd, env=env, capture_output=True, check=False
    )
    return result.returncode, result.stdout


def stryker_config(unit: Unit, root: Path, report: Path) -> dict[str, Any]:
    """Return the per-run config: the tracked stryker.config.json plus this unit's scope."""
    package = PACKAGES[unit.language]
    try:
        tracked = _obj(json.loads((root / package / STRYKER_CONFIG).read_text()))
    except (OSError, ValueError) as error:
        raise MutationError(f"{package}/{STRYKER_CONFIG}: unreadable ({error})") from error
    config = {k: v for k, v in tracked.items() if k != "$schema"}
    config.update(
        mutate=_rel(unit, unit.mutate),
        testFiles=_rel(unit, unit.tests),
        jsonReporter={"fileName": str(report)},
    )
    return config


def run_stryker(unit: Unit, root: Path, staging: Path) -> ToolRun:
    """Run StrykerJS in the unit's package with a per-run config written to `staging`."""
    package = PACKAGES[unit.language]
    report = staging / "mutation.json"
    config = staging / STRYKER_CONFIG
    config.write_text(json.dumps(stryker_config(unit, root, report), indent=2) + "\n")
    argv = ["npx", "--no-install", "stryker", "run", str(config)]
    started = time.monotonic()
    code, console = run_record.run_child(argv, root / package, _child_env())
    duration = round(time.monotonic() - started, 6)
    files = {"raw/stryker.config.json": config.read_bytes()}
    mutants: list[dict[str, Any]] = []
    if report.is_file():
        files["raw/mutation.json"] = report.read_bytes()
        if code == 0:
            mutants = parse_stryker_report(_obj(json.loads(report.read_text())), package)
    return ToolRun(
        code,
        console,
        mutants,
        files,
        [*argv[:4], "<staging>"],
        duration,
        report_missing=not report.is_file(),
    )


@dataclass
class Runners:
    """The processes a run starts; the tests replace them."""

    baseline: Callable[[Unit, Path], tuple[int, bytes]] = run_tests
    tests: Callable[[Unit, Path], tuple[int, bytes]] = run_tests
    mechanical: Callable[[Unit, Path, Path], ToolRun] | None = None

    def tool(self, unit: Unit) -> Callable[[Unit, Path, Path], ToolRun]:
        """Return the mechanical tool for `unit`'s language."""
        if self.mechanical is not None:
            return self.mechanical
        return run_cosmic_ray if unit.language == "python" else run_stryker


# --------------------------------------------------------------------------------------------
# Tree safety
# --------------------------------------------------------------------------------------------


def dirty_tracked(root: Path) -> list[str]:
    """Return every tracked path `git status --porcelain --untracked-files=no` reports."""
    out = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=no")
    if out.returncode != 0:
        raise MutationError("git status failed")
    paths: list[str] = []
    records = iter(out.stdout.decode().split("\0"))
    for record in records:
        if not record:
            continue
        paths.append(record[3:])
        if record[0] in "RC":  # a rename or copy carries its source as the next record
            next(records, None)
    return paths


def settle_tree(root: Path, unit: Unit, out: IO[str]) -> bool:
    """Leave the tracked tree clean after a run, restoring only the unit's own files.

    Returns False (and touches nothing) when anything outside the unit is dirty.
    """
    dirty = dirty_tracked(root)
    if not dirty:
        return True
    if set(dirty) <= set(unit.mutate):
        restore = _git(root, "restore", "--source=HEAD", "--", *dirty)
        out.write(
            "mutation: WARNING the run left mutated source on disk; restored from HEAD: "
            f"{', '.join(dirty)}\n"
        )
        if restore.returncode == 0 and not dirty_tracked(root):
            return True
    out.write(
        "mutation: REFUSED DIRTY_AFTER_RUN the tracked tree is dirty after the run "
        f"({', '.join(dirty)}); nothing was touched. Restore it before doing anything else.\n"
    )
    return False


# --------------------------------------------------------------------------------------------
# Records and worksheets
# --------------------------------------------------------------------------------------------


def _md_cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def worksheet(  # noqa: PLR0913, PLR0917 - the worksheet's fields
    unit: Unit,
    record: str,
    tree: str,
    current_digest: str,
    counts: dict[str, int],
    survivors: Sequence[dict[str, Any]],
    categories: Sequence[str],
) -> str:
    """Return the semantic worksheet for one mechanical run."""
    lines = [
        f"# Mutation worksheet: {unit.unit}",
        "",
        f"- Record: {record}",
        f"- Tree: {tree}",
        f"- Fingerprint digest: {current_digest}",
        "- Mechanical: " + ", ".join(f"{v} {k.replace('_', '-')}" for k, v in counts.items()),
        f"- Tests: {', '.join(unit.tests)}",
        "",
        "## Surviving mechanical mutants",
        "",
    ]
    if survivors:
        lines += [
            "| ID | Verdict | Location | Operator | Count | Mutation |",
            "|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {s['id']} | {s['verdict']} | {_md_cell(s['location'])} | {_md_cell(s['operator'])} "
            f"| {s['count']} | {_md_cell(s['mutation'])} |"
            for s in survivors
        ]
    else:
        lines.append("None.")
    lines += [
        "",
        f"## Semantic categories (set {semantic_set_version(categories)})",
        "",
        *[f"- {c}" for c in categories],
        "",
        "## Proposals",
        "",
        "Write one proposal per plausible semantic mutation as a YAML list in a file outside the",
        "record (this copy under .runs/mutation/worksheets/ is writable), then run",
        f"`make mutate UNIT={unit.unit} SEMANTIC=<file>`. Each patch is a unified diff against",
        "HEAD that touches only the unit's files; the run applies it, runs the mapped tests,",
        "and reverts it.",
        "",
        "```yaml",
        "- id: P-01",
        "  category: <the category above it belongs to>",
        "  description: <the mutation, as a careful engineer could plausibly have written it>",
        "  patch: |",
        f"    --- a/{unit.mutate[0]}",
        f"    +++ b/{unit.mutate[0]}",
        "    @@ -<line>,<n> +<line>,<n> @@",
        "```",
        "",
    ]
    return "\n".join(lines)


@dataclass
class Context:
    """Where one invocation reads and writes."""

    root: Path
    runs: Path
    out: IO[str]
    priorities_path: Path | None = None
    steering_path: Path | None = None
    _priorities: Priorities | None = None

    @property
    def priorities(self) -> Priorities:
        """mutation-priorities.yaml, loaded once."""
        if self._priorities is None:
            self._priorities = load_priorities(self.priorities_path or self.root / PRIORITIES)
        return self._priorities

    def inputs(self) -> Inputs:
        """Return the fingerprint inputs of HEAD."""
        return read_inputs(self.root, self.steering_path or self.root / STEERING)

    def say(self, line: str) -> None:
        """Print one line."""
        self.out.write(f"mutation: {line}\n")
        self.out.flush()


def _write(  # noqa: PLR0913, PLR0917 - the record's parts
    ctx: Context,
    run: dict[str, Any],
    steps: list[dict[str, Any]],
    files: dict[str, bytes],
    now: datetime,
) -> str:
    directory = run_record.write_record(KIND, run, steps=steps, files=files, root=ctx.runs, now=now)
    return f"{KIND}/{Path(directory).name}"


def _base_run(  # noqa: PLR0913, PLR0917 - the record's identifying fields
    ctx: Context, unit: Unit, track: str, print_: dict[str, Any], argv: list[str]
) -> tuple[dict[str, Any], float]:
    """Return the run.json fields known before the baseline starts, and the monotonic start.

    A mutation run has no canonical command, so `canonical` is always false.
    """
    run = {
        "canonical": False,
        "unit": unit.unit,
        "tier": unit.tier,
        "language": unit.language,
        "track": track,
        "fingerprint": print_,
        "digest": digest(print_),
        "argv": argv,
        "cwd": ".",
        **run_record.provenance(ctx.root),
        "porcelain_before": run_record.porcelain_count(ctx.root),
        "utc_start": _utc(),
        "delta": None,
    }
    return run, time.monotonic()


def _finish_run(  # noqa: PLR0913 - the record's closing fields, passed by keyword
    ctx: Context,
    run: dict[str, Any],
    started: float,
    *,
    exit_code: int,
    report_missing: bool,
    tool_versions: dict[str, str | None],
    **fields: Any,
) -> None:
    """Close the run: end time, elapsed time since the baseline started, cleanliness, outcome."""
    after = run_record.porcelain_count(ctx.root)
    run.update(
        utc_end=_utc(),
        duration_s=round(time.monotonic() - started, 6),
        porcelain_after=after,
        clean=run["porcelain_before"] == 0 and after == 0,
        exit_code=exit_code,
        report_missing=report_missing,
        outcome="pass" if exit_code == 0 and not report_missing else "fail",
        tool_versions=tool_versions,
        **fields,
    )


def _record_ref(now: datetime, tree: str) -> str:
    return f"{KIND}/{now.strftime(STAMP)}-{tree[:12]}"


# --------------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------------


def _resolve(ctx: Context, name: str) -> Unit:
    unit = ctx.priorities.find(name)
    if unit is None:
        raise MutationError(f"REFUSED UNKNOWN_UNIT {name} is not in {PRIORITIES}")
    if not unit.tests:
        raise MutationError(f"REFUSED UNTESTED {name} maps no tests in {PRIORITIES}")
    return unit


def _preflight(ctx: Context, unit: Unit, runners: Runners) -> tuple[int, bytes, float] | int:
    dirty = dirty_tracked(ctx.root)
    if dirty:
        raise MutationError(
            f"REFUSED DIRTY_TREE commit or stash first; tracked changes: {', '.join(dirty[:5])}"
        )
    ctx.say(f"baseline: running the {len(unit.tests)} mapped test file(s) of {unit.unit}")
    started = time.monotonic()
    code, log = runners.baseline(unit, ctx.root)
    if code != 0:
        ctx.say(f"REFUSED BASELINE_RED the mapped tests fail before any mutation (exit {code})")
        return EXIT_FAILED
    return code, log, round(time.monotonic() - started, 6)


def run_unit(  # noqa: PLR0913, PLR0917, PLR0915 - one mechanical run, step by step
    ctx: Context, name: str, semantic: Path | None = None, runners: Runners | None = None
) -> int:
    """Run the mechanical pass (or, with `semantic`, the proposals) for one unit."""
    runners = runners or Runners()
    unit = _resolve(ctx, name)
    inputs = ctx.inputs()
    print_ = fingerprint(unit, inputs)
    current = digest(print_)
    cmap_path = map_path(ctx.runs)
    cmap = load_map(cmap_path)
    entry = _obj(cmap["units"].get(unit.unit)) or None
    if semantic is not None:
        return _run_semantic(ctx, unit, semantic, print_, entry, cmap, runners)
    if entry is not None and entry.get("digest") == current:
        ctx.say(f"{unit.unit}: current (record {entry.get('record')}), skipping")
        return 0
    # The record's clock and porcelain_before start before the baseline its duration includes.
    run, started = _base_run(
        ctx, unit, "mechanical", print_, ["scripts/mutation.py", "run", "--unit", unit.unit]
    )
    preflight = _preflight(ctx, unit, runners)
    if isinstance(preflight, int):
        return preflight
    _, baseline_log, baseline_s = preflight

    staging = Path(tempfile.mkdtemp(prefix="mutation-"))
    try:
        ctx.say(
            f"{unit.unit}: {tool_name(unit.language)} {inputs.tool(unit.language)} ({unit.tier})"
        )
        try:
            result = runners.tool(unit)(unit, ctx.root, staging)
        finally:
            settled = settle_tree(ctx.root, unit, ctx.out)
        if not settled:
            return EXIT_DIRTY
        counts = counts_of(result.mutants)
        now = datetime.now(UTC)
        record = _record_ref(now, str(run["tree"]))
        at = _utc()
        prior = _seq(entry.get("survivors")) if entry else []
        groups = group_survivors(result.mutants, "mechanical")
        merged = merge_survivors(
            prior, groups, result.mutants, track="mechanical", record=record, at=at
        )
        this_run = [
            s for s in merged if s["track"] == "mechanical" and s.get("last_seen") == record
        ]
        sheet = worksheet(
            unit, record, str(run["tree"]), current, counts, this_run, inputs.categories
        )
        _finish_run(
            ctx,
            run,
            started,
            exit_code=result.exit_code,
            report_missing=result.report_missing,
            tool_versions={
                **run_record.tool_versions(ctx.root / (PACKAGES.get(unit.language, ".")), None),
                tool_name(unit.language): inputs.tool(unit.language),
            },
            counts=counts,
        )
        steps = [
            {
                "name": "baseline",
                "argv": test_argv(unit)[0],
                "exit_code": 0,
                "duration_s": baseline_s,
            },
            {
                "name": tool_name(unit.language),
                "argv": result.argv,
                "exit_code": result.exit_code,
                "duration_s": result.duration_s,
            },
        ]
        files = {
            "console.log": baseline_log + result.console,
            "results.json": (json.dumps(result.mutants, indent=2) + "\n").encode(),
            "survivors.json": (json.dumps(this_run, indent=2) + "\n").encode(),
            "semantic-worksheet.md": sheet.encode(),
            **result.files,
        }
        written = _write(ctx, run, steps, files, now)
        if run["outcome"] != "pass":
            why = "wrote no report" if result.exit_code == 0 else f"failed (exit {result.exit_code})"
            ctx.say(
                f"{unit.unit}: {tool_name(unit.language)} {why}; "
                f"record {written}; the coverage map is unchanged"
            )
            return EXIT_FAILED
        sheets = ctx.runs / KIND / WORKSHEETS
        sheets.mkdir(parents=True, exist_ok=True)
        sheet_path = sheets / f"{unit_slug(unit.unit)}-{Path(written).name}.md"
        sheet_path.write_text(sheet)
        cmap["units"][unit.unit] = {
            "unit": unit.unit,
            "tier": unit.tier,
            "language": unit.language,
            "fingerprint": print_,
            "digest": current,
            "record": written,
            "counts": counts,
            "survivors": merged,
            "semantic_runs": _seq(entry.get("semantic_runs")) if entry else [],
        }
        save_map(cmap_path, cmap)
        open_count = sum(1 for s in merged if s["verdict"] == "OPEN" and s.get("present"))
        ctx.say(
            f"{unit.unit}: {counts['generated']} generated, {counts['killed']} killed, "
            f"{counts['survived']} survived, {counts['timeout']} timeout, "
            f"{counts['no_coverage']} no-coverage, {counts['errors']} errors; "
            f"{open_count} OPEN survivor group(s)"
        )
        ctx.say(f"record {written}")
        ctx.say(f"worksheet {sheet_path}")
        return 0
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def load_proposals(path: Path) -> list[dict[str, str]]:
    """Read and validate a proposals YAML list of {id, category, description, patch}."""
    try:
        document = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as error:
        raise MutationError(f"{path}: unreadable proposals ({error})") from error
    if not isinstance(document, list) or not document:
        raise MutationError(f"{path}: proposals are a non-empty YAML list")
    proposals: list[dict[str, str]] = []
    ids: set[str] = set()
    for index, raw in enumerate(_seq(cast("object", document))):
        item = _obj(raw)
        keys = ("id", "category", "description", "patch")
        if set(item) != set(keys) or not all(
            isinstance(item[k], str) and item[k].strip() for k in keys
        ):
            raise MutationError(f"{path}: proposal {index} needs exactly {', '.join(keys)}")
        if item["id"] in ids:
            raise MutationError(f"{path}: proposal id {item['id']} is used twice")
        ids.add(item["id"])
        proposals.append({k: str(item[k]) for k in keys})
    return proposals


def _patch_bytes(patch: str) -> bytes:
    return (patch if patch.endswith("\n") else patch + "\n").encode()


def _patch_paths(root: Path, patch: bytes) -> list[str] | None:
    numstat = _git(root, "apply", "--numstat", "-", stdin=patch)
    if numstat.returncode != 0:
        return None
    return [
        line.split("\t", 2)[2]
        for line in numstat.stdout.decode().splitlines()
        if line.count("\t") >= 2  # noqa: PLR2004 - added, deleted, path
    ]


def _run_semantic(  # noqa: PLR0913, PLR0917, PLR0915 - one proposals run, step by step
    ctx: Context,
    unit: Unit,
    proposals_path: Path,
    print_: dict[str, Any],
    entry: dict[str, Any] | None,
    cmap: dict[str, Any],
    runners: Runners,
) -> int:
    current = digest(print_)
    if entry is None or entry.get("digest") != current:
        raise MutationError(
            f"REFUSED NOT_CURRENT {unit.unit}: run `make mutate UNIT={unit.unit}` first; the "
            "semantic pass needs a current mechanical entry"
        )
    proposals = load_proposals(proposals_path)
    raw = proposals_path.read_bytes()
    proposals_sha = _sha256(raw)
    for done in _seq(entry.get("semantic_runs")):
        if done.get("proposals_sha256") == proposals_sha and done.get("digest") == current:
            ctx.say(
                f"{unit.unit}: these proposals are already recorded for the current fingerprint "
                f"(record {done.get('record')}), skipping"
            )
            return 0
    run, started = _base_run(
        ctx,
        unit,
        "semantic",
        print_,
        ["scripts/mutation.py", "run", "--unit", unit.unit, "--semantic", proposals_path.name],
    )
    preflight = _preflight(ctx, unit, runners)
    if isinstance(preflight, int):
        return preflight
    _, console, baseline_s = preflight
    results: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = [
        {"name": "baseline", "argv": test_argv(unit)[0], "exit_code": 0, "duration_s": baseline_s}
    ]
    for proposal in proposals:
        patch = _patch_bytes(proposal["patch"])
        hunk = _HUNK.search(proposal["patch"])
        paths = _patch_paths(ctx.root, patch)
        file = paths[0] if paths else unit.mutate[0]
        result: dict[str, Any] = {
            "id": proposal["id"],
            "file": file,
            "line": int(hunk.group(1)) if hunk else 0,
            "operator": proposal["category"],
            "mutation": _collapse(proposal["description"]),
            "patch_sha256": _sha256(patch),
            "killed_by": None,
        }
        # Proposal-local: `started` times the whole run, from before the baseline.
        proposal_started = time.monotonic()
        # The mapped tests' exit code, or None when the proposal never reached them.
        test_code: int | None = None
        if paths is None or _git(ctx.root, "apply", "--check", "-", stdin=patch).returncode != 0:
            result.update(status="error", note="the patch does not apply to HEAD")
        elif not set(paths) <= set(unit.mutate):
            result.update(status="error", note=f"the patch touches files outside the unit: {paths}")
        elif _git(ctx.root, "apply", "-", stdin=patch).returncode != 0:
            result.update(status="error", note="git apply failed")
        else:
            ctx.say(f"{proposal['id']}: applied; running the mapped tests")
            code, log = 0, b""
            try:
                code, log = runners.tests(unit, ctx.root)
            finally:
                reverted = _git(ctx.root, "apply", "-R", "-", stdin=patch).returncode == 0
                restored = (reverted and not dirty_tracked(ctx.root)) or settle_tree(
                    ctx.root, unit, ctx.out
                )
            if not restored:
                return EXIT_DIRTY
            console += log
            test_code = code
            killed = code != 0
            result.update(
                status="killed" if killed else "survived",
                killed_by=killing_test(unit.language, log.decode(errors="replace"))
                if killed
                else None,
            )
        step: dict[str, Any] = {
            "name": proposal["id"],
            "argv": test_argv(unit)[0],
            "exit_code": test_code,
            "duration_s": round(time.monotonic() - proposal_started, 6),
        }
        if test_code is None:
            step["not_run"] = True
        steps.append(step)
        ctx.say(f"{proposal['id']}: {result['status']}")
        results.append(result)

    counts = counts_of(results)
    now = datetime.now(UTC)
    record = _record_ref(now, str(run["tree"]))
    at = _utc()
    scope = {survivor_key({**r, "track": "semantic"}) for r in results}
    groups = [
        {**r, "track": "semantic", "location": f"{r['file']}:{r['line']}", "count": 1}
        for r in results
        if r["status"] == "survived"
    ]
    merged = merge_survivors(
        _seq(entry.get("survivors")),
        groups,
        results,
        track="semantic",
        record=record,
        at=at,
        scope=scope,
    )
    this_run = [s for s in merged if s["track"] == "semantic" and s.get("last_seen") == record]
    _finish_run(
        ctx,
        run,
        started,
        exit_code=0,
        report_missing=False,
        tool_versions=run_record.tool_versions(ctx.root / PACKAGES.get(unit.language, "."), None),
        counts=counts,
        proposals_sha256=proposals_sha,
    )
    files = {
        "console.log": console,
        "results.json": (json.dumps(results, indent=2) + "\n").encode(),
        "survivors.json": (json.dumps(this_run, indent=2) + "\n").encode(),
        "raw/proposals.yaml": raw,
    }
    written = _write(ctx, run, steps, files, now)
    entry = dict(entry)
    entry["survivors"] = merged
    entry["semantic_runs"] = [
        *_seq(entry.get("semantic_runs")),
        {"proposals_sha256": proposals_sha, "digest": current, "record": written, "counts": counts},
    ]
    cmap["units"][unit.unit] = entry
    save_map(map_path(ctx.runs), cmap)
    ctx.say(
        f"{unit.unit}: {counts['generated']} proposals, {counts['killed']} killed, "
        f"{counts['survived']} survived, {counts['errors']} errors; record {written}"
    )
    return 0


def states(ctx: Context) -> list[tuple[Unit, str, dict[str, Any] | None]]:
    """Return every unit with its state and coverage-map entry, in priority order."""
    inputs = ctx.inputs()
    units = _obj(load_map(map_path(ctx.runs))["units"])
    found: list[tuple[Unit, str, dict[str, Any] | None]] = []
    for unit in ctx.priorities.units:
        entry = _obj(units.get(unit.unit)) or None
        try:
            current = digest(fingerprint(unit, inputs)) if unit.tests else None
        except MutationError:
            current = None
        found.append((unit, unit_state(unit, entry, current), entry))
    return found


def run_next(ctx: Context, n: int, runners: Runners | None = None) -> int:
    """Run the first `n` missing or stale units in tier then file order."""
    ran = 0
    for unit, state, _ in states(ctx):
        if ran >= n:
            break
        if state == "untested":
            ctx.say(f"{unit.unit}: untested (no mapped tests), skipping")
            continue
        if state in {"missing", "stale"}:
            code = run_unit(ctx, unit.unit, runners=runners)
            ran += 1
            if code != 0:
                return code
    if ran == 0:
        ctx.say("nothing to run: every tested unit is current")
    return 0


def _open_survivors(entry: dict[str, Any] | None) -> list[dict[str, Any]]:
    return [s for s in _seq((entry or {}).get("survivors")) if s.get("verdict") == "OPEN"]


def status(ctx: Context, summary: bool = False) -> int:
    """Print coverage per tier; exit 0 whatever the coverage is."""
    found = states(ctx)
    lines = ["mutation coverage (report only; mutation-priorities.yaml order):"]
    for tier in TIERS:
        tally: dict[str, int] = dict.fromkeys(("current", "stale", "missing", "untested"), 0)
        open_count = 0
        for unit, state, entry in found:
            if unit.tier == tier:
                tally[state] += 1
                open_count += len(_open_survivors(entry))
        lines.append(
            f"  {tier}  current {tally['current']}  stale {tally['stale']}  "
            f"missing {tally['missing']}  untested {tally['untested']}  OPEN {open_count}"
        )
    if not summary:
        upcoming = [u.unit for u, s, _ in found if s in {"missing", "stale"}][:5]
        lines.append("next for make mutate-next: " + (", ".join(upcoming) or "none"))
        stale = [u.unit for u, s, _ in found if s == "stale"]
        if stale:
            lines.append("stale: " + ", ".join(stale))
        for unit, _, entry in found:
            for survivor in _open_survivors(entry):
                lines.append(
                    f"  OPEN {unit.unit} {survivor['id']} {survivor['track']} "
                    f"{survivor['location']} {survivor['operator']}: {survivor['mutation']}"
                )
    ctx.out.write("\n".join(lines) + "\n")
    return 0


def verdict(  # noqa: PLR0913, PLR0917 - the command's arguments
    ctx: Context, unit: str, survivor_id: str, value: str, test: str | None, reason: str | None
) -> int:
    """Change one survivor's verdict in place."""
    path = map_path(ctx.runs)
    cmap = load_map(path)
    entry = _obj(cmap["units"].get(unit))
    if not entry:
        raise MutationError(f"REFUSED UNKNOWN_UNIT {unit} has no coverage-map entry")
    set_verdict(ctx.root, entry, survivor_id, value, test=test, reason=reason, at=_utc())
    save_map(path, cmap)
    ctx.say(f"{unit} {survivor_id}: {value}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the command line and run one command."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("propose", help="print the import-mapped priorities YAML")
    run_parser = commands.add_parser("run", help="mutate one unit")
    run_parser.add_argument("--unit", required=True)
    run_parser.add_argument("--semantic", type=Path)
    next_parser = commands.add_parser("next", help="run the next missing or stale units")
    next_parser.add_argument("--n", type=int, default=5)
    status_parser = commands.add_parser("status", help="coverage per tier (report only)")
    status_parser.add_argument("--summary", action="store_true")
    verdict_parser = commands.add_parser("verdict", help="change one survivor's verdict")
    verdict_parser.add_argument("--unit", required=True)
    verdict_parser.add_argument("--id", required=True)
    verdict_parser.add_argument("--verdict", required=True, choices=VERDICTS)
    verdict_parser.add_argument("--test")
    verdict_parser.add_argument("--reason")
    args = parser.parse_args(argv)
    try:
        if args.command == "propose":
            sys.stdout.write(render_priorities(propose(ROOT)))
            return 0
        ctx = Context(ROOT, Path(run_record.record_root(cwd=ROOT)), sys.stdout)
        if args.command == "run":
            return run_unit(ctx, args.unit, args.semantic)
        if args.command == "next":
            if args.n < 1:
                raise MutationError("--n must be at least 1")
            return run_next(ctx, args.n)
        if args.command == "status":
            return status(ctx, summary=args.summary)
        return verdict(ctx, args.unit, args.id, args.verdict, args.test, args.reason)
    except MutationError as error:
        sys.stderr.write(f"mutation: {error}\n")
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
