# SPDX-License-Identifier: MIT-0
"""Every .gitleaks.toml allowlist is bound to one rule, one file and one exact literal.

Fictional fixtures trip the secret scanner (F-2), and the easy fix, a path-only or regex-only
allowlist, hides every finding in a file or one literal in every file. The convention is stated in
the config's header; this test holds every entry to it, including that the anchored path names a
file that exists and that the file still contains the allowlisted literal, so a stale entry fails
rather than lingering as a silent exemption.
"""

from __future__ import annotations

import string
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest

pytestmark = pytest.mark.unit

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPOSITORY_ROOT / ".gitleaks.toml"
ALLOWED_KEYS = {"description", "targetRules", "condition", "paths", "regexes"}
HISTORY_ONLY = "history only"
# An unescaped regex metacharacter means the pattern can match more than one literal.
_METACHARACTER = frozenset(".*+?()[]{}|^$")
# A backslash quotes ASCII punctuation, which then matches itself (Go RE2, as gitleaks compiles
# it, and Python). Before a letter or digit it is an operator, a class (`\d`, `\w`, `\s`), an
# assertion (`\b`, `\A`) or a code (`\x41`, `\1`), so the pattern is not one literal.
_QUOTABLE = frozenset(string.punctuation)


def _literal(pattern: str) -> str | None:
    """Return the exact text an anchored literal pattern matches, or None if it is not one."""
    if not (pattern.startswith("^") and pattern.endswith("$")) or len(pattern) < 3:
        return None
    body = pattern[1:-1]
    decoded: list[str] = []
    index = 0
    while index < len(body):
        character = body[index]
        if character == "\\":
            quoted = body[index + 1] if index + 1 < len(body) else ""
            if quoted not in _QUOTABLE:
                return None
            decoded.append(quoted)
            index += 2
            continue
        if character in _METACHARACTER:
            return None
        decoded.append(character)
        index += 1
    return "".join(decoded)


def convention_findings(config: dict[str, Any], root: Path) -> list[str]:
    """Return one finding per departure from the allowlist convention."""
    findings: list[str] = []
    if "allowlist" in config:
        findings.append("top-level [allowlist]: use one [[allowlists]] entry per literal")
    for rule in cast("list[dict[str, Any]]", config.get("rules", [])):
        if "allowlist" in rule or "allowlists" in rule:
            findings.append(f"rule {rule.get('id')!r} carries its own allowlist")
    entries = cast("list[dict[str, Any]]", config.get("allowlists", []))
    for index, entry in enumerate(entries):
        where = f"allowlists[{index}]"
        extra = sorted(set(entry) - ALLOWED_KEYS)
        if extra:
            findings.append(f"{where}: unexpected keys {extra}")
        description = entry.get("description")
        if not isinstance(description, str) or not description.strip():
            findings.append(f"{where}: no description naming the fixture's purpose")
        rules = entry.get("targetRules")
        if not (isinstance(rules, list) and len(cast("list[object]", rules)) == 1):
            findings.append(f"{where}: targetRules must name exactly one rule")
        if entry.get("condition") != "AND":
            findings.append(f'{where}: condition must be "AND"')
        findings.extend(_anchored_pair(entry, where, root))
    return findings


def _anchored_pair(entry: dict[str, Any], where: str, root: Path) -> list[str]:
    paths = entry.get("paths")
    regexes = entry.get("regexes")
    if not (isinstance(paths, list) and len(cast("list[object]", paths)) == 1):
        return [f"{where}: exactly one paths entry required (no regex-only allowlist)"]
    if not (isinstance(regexes, list) and len(cast("list[object]", regexes)) == 1):
        return [f"{where}: exactly one regexes entry required (no path-only allowlist)"]
    path = _literal(str(cast("list[object]", paths)[0]))
    literal = _literal(str(cast("list[object]", regexes)[0]))
    if path is None:
        return [f"{where}: paths must be anchored ^...$ to one literal file path"]
    if literal is None:
        return [f"{where}: regexes must be anchored ^...$ to one exact literal"]
    target = root / path
    if not target.is_file():
        return [f"{where}: {path} does not exist"]
    present = literal in target.read_text(encoding="utf-8")
    history_only = HISTORY_ONLY in str(entry.get("description", ""))
    return _content_findings(f"{where}: {path}", literal if not present else None, history_only)


def _content_findings(label: str, absent_literal: str | None, history_only: bool) -> list[str]:
    """Hold an entry to its file: the literal is there, or the entry says it is history only.

    A literal removed from the tree can still sit in pushed history, which a full-history scan
    reads, so such an entry stays and its description says so; a literal that comes back makes
    the claim false.
    """
    if absent_literal is not None and not history_only:
        return [f"{label} no longer contains {absent_literal!r}; say {HISTORY_ONLY!r} or remove it"]
    if absent_literal is None and history_only:
        return [f"{label} contains the literal, so the entry is not {HISTORY_ONLY!r}"]
    return []


def _parse(text: str) -> dict[str, Any]:
    return tomllib.loads(text)


def test_every_allowlist_in_the_repository_config_meets_the_convention() -> None:
    config = _parse(CONFIG.read_text(encoding="utf-8"))

    assert len(config["allowlists"]) > 0
    assert convention_findings(config, REPOSITORY_ROOT) == []


def test_the_config_header_states_the_convention() -> None:
    header = CONFIG.read_text(encoding="utf-8").split("title =", 1)[0]

    assert "tests/unit/test_gitleaks_allowlist_convention.py" in header
    assert "never a path-only or regex-only allowlist" in header


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text('LABEL = "fictional-literal-one"\n')
    return tmp_path


VALID = """
[[allowlists]]
description = "Fictional literal in the x test"
targetRules = ["generic-api-key"]
condition = "AND"
paths = ['''^tests/test_x\\.py$''']
regexes = ['''^fictional-literal-one$''']
"""


def test_a_conforming_entry_passes(fixture_root: Path) -> None:
    assert convention_findings(_parse(VALID), fixture_root) == []


@pytest.mark.parametrize(
    ("change", "finding"),
    [
        (("regexes = ['''^fictional-literal-one$''']\n", ""), "no path-only allowlist"),
        (("paths = ['''^tests/test_x\\.py$''']\n", ""), "no regex-only allowlist"),
        (("^tests/test_x\\.py$", "tests/test_x\\.py"), "anchored ^...$ to one literal file path"),
        (("^tests/test_x\\.py$", "^tests/.*\\.py$"), "anchored ^...$ to one literal file path"),
        (
            ("^fictional-literal-one$", "^fictional-literal-\\w+$"),
            "anchored ^...$ to one exact literal",
        ),
        (('condition = "AND"', 'condition = "OR"'), 'condition must be "AND"'),
        (('["generic-api-key"]', '["generic-api-key", "aws-access-token"]'), "exactly one rule"),
        (('description = "Fictional literal in the x test"', 'description = ""'), "no description"),
        (
            ("fictional-literal-one", "fictional-literal-two"),
            "no longer contains 'fictional-literal-two'",
        ),
        (
            ("Fictional literal in the x test", "Fictional literal, history only"),
            "is not 'history only'",
        ),
        (("test_x", "test_absent"), "tests/test_absent.py does not exist"),
    ],
)
def test_a_departing_entry_fails(fixture_root: Path, change: tuple[str, str], finding: str) -> None:
    old, new = change
    assert old in VALID
    findings = convention_findings(_parse(VALID.replace(old, new)), fixture_root)

    assert len(findings) == 1
    assert finding in findings[0]


@pytest.mark.parametrize("escape", ["\\d", "\\w", "\\s", "\\W", "\\b", "\\A", "\\x41", "\\1"])
def test_a_class_or_code_escape_is_not_an_exact_literal(fixture_root: Path, escape: str) -> None:
    # The file holds the escape's letter, so reading `\d` as a quoted `d` would find the
    # "literal" and pass: only the escape itself can be refused here.
    decoded_as_quoted = "fictional-literal-one" + escape[1:]
    (fixture_root / "tests" / "test_x.py").write_text(f'LABEL = "{decoded_as_quoted}"\n')
    text = VALID.replace("^fictional-literal-one$", f"^fictional-literal-one{escape}$")

    assert convention_findings(_parse(text), fixture_root) == [
        "allowlists[0]: regexes must be anchored ^...$ to one exact literal"
    ]


def test_an_escaped_punctuation_character_is_part_of_the_literal(fixture_root: Path) -> None:
    text = VALID.replace("^fictional-literal-one$", "^fictional\\-literal\\-one$")

    assert convention_findings(_parse(text), fixture_root) == []


def test_a_history_only_entry_passes_when_its_literal_left_the_file(fixture_root: Path) -> None:
    text = VALID.replace(
        "Fictional literal in the x test", "Fictional literal, history only"
    ).replace("fictional-literal-one", "fictional-literal-zero")

    assert convention_findings(_parse(text), fixture_root) == []


def test_a_top_level_allowlist_fails(fixture_root: Path) -> None:
    text = VALID + "\n[allowlist]\npaths = ['''^tests/test_x\\.py$''']\n"

    assert convention_findings(_parse(text), fixture_root) == [
        "top-level [allowlist]: use one [[allowlists]] entry per literal"
    ]
