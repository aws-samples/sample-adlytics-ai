# SPDX-License-Identifier: MIT-0
"""A deep-input code change always moves every deep cache key (ADR-076 decision 1, R5-5).

The deep enrichment runs only on a cache miss, so its input cannot be digested into the key
it is looked up by. Instead the deep key carries a declared revision, and this test fails
until a new revision is appended whenever the in-repository import closure of the deep-input
code, or the metric dictionary whose labels name deep entries, changes.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from adapters.deep_input_revision import DEEP_INPUT_REVISION, DEEP_INPUT_REVISIONS

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-076")]
ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ROOTS = (
    "adapters.deep_insight_inputs",
    "domain.presentation.metric_focus",
    # C4 shapes the returns deep context through the card input it extends (design 4.11).
    "adapters.retail_insight_requests",
)
REQUIRED_MODULES = frozenset(
    {
        "adapters.deep_insight_inputs",
        "domain.presentation.metric_focus",
        "domain.presentation.campaign_portfolio",
        "domain.presentation.retail_health",
        "domain.presentation.insights",
        "adapters.retail_insight_requests",
    }
)
"""The shared callees that shape deep input; a walk that misses one hashes too little."""
SELF = "adapters.deep_input_revision"
"""The declaration itself: the deep serve path imports it, and hashing it would be circular."""
METRIC_DICTIONARY = ROOT / "contracts" / "shared" / "metric-dictionary.yaml"
SRC_PACKAGES = frozenset(path.name for path in SRC.iterdir() if (path / "__init__.py").exists())


def _module_file(name: str) -> Path | None:
    base = SRC.joinpath(*name.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(name: str, path: Path) -> set[str]:
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes())):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                parts = package.split(".")
                anchor = ".".join(parts[: len(parts) - node.level + 1])
                base = f"{anchor}.{node.module}" if node.module else anchor
            else:
                base = node.module or ""
            found.add(base)
            # `from package import module` names a submodule when one exists.
            found.update(f"{base}.{alias.name}" for alias in node.names)
    return {
        item
        for item in found
        if item.split(".")[0] in SRC_PACKAGES and _module_file(item) is not None
    }


def import_closure() -> dict[str, Path]:
    """Every in-repository module the deep-input roots import, transitively."""
    pending: list[str] = list(ROOTS)
    closure: dict[str, Path] = {}
    while pending:
        name = pending.pop()
        if name in closure or name == SELF:
            continue
        path = _module_file(name)
        if path is None:
            raise AssertionError(f"deep-input closure module {name} has no source file.")
        closure[name] = path
        pending.extend(_imports(name, path) - closure.keys())
    return closure


def closure_digest() -> str:
    digest = hashlib.sha256()
    for name, path in sorted(import_closure().items()):
        digest.update(name.encode() + b"\0" + path.read_bytes())
    digest.update(METRIC_DICTIONARY.read_bytes())
    return digest.hexdigest()


def test_the_closure_holds_every_module_that_shapes_deep_input() -> None:
    closure = import_closure()
    assert closure.keys() >= REQUIRED_MODULES
    assert SELF not in closure


def test_the_declared_deep_input_revision_matches_the_code() -> None:
    expected = closure_digest()
    assert DEEP_INPUT_REVISIONS[-1][1] == expected, (
        "Deep-input code changed. Append a new revision to DEEP_INPUT_REVISIONS in "
        f"src/adapters/deep_input_revision.py with digest {expected}; never edit an old one."
    )


def test_revisions_are_append_only_and_unique() -> None:
    names = [name for name, _ in DEEP_INPUT_REVISIONS]
    digests = [digest for _, digest in DEEP_INPUT_REVISIONS]
    assert names == sorted(names) and len(set(names)) == len(names)
    assert len(set(digests)) == len(digests)
    assert names[-1] == DEEP_INPUT_REVISION


def test_a_changed_closure_byte_changes_the_digest(tmp_path: Path) -> None:
    """Positive control: the digest reads file bytes, so any edit moves it."""
    closure = import_closure()
    name, path = sorted(closure.items())[0]
    edited = tmp_path / "edited.py"
    edited.write_bytes(path.read_bytes() + b"\n# edit\n")
    before, after = hashlib.sha256(), hashlib.sha256()
    for key, source in sorted(closure.items()):
        before.update(key.encode() + b"\0" + source.read_bytes())
        after.update(key.encode() + b"\0" + (edited if key == name else source).read_bytes())
    assert before.hexdigest() != after.hexdigest()
