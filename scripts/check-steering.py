#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Enforce the workspace steering front-matter and plain-text contract.

Usage: uv run python scripts/check-steering.py
Exits non-zero on any finding.
"""

import pathlib
import re
import subprocess
import sys
from collections import Counter, defaultdict
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.resolver import BaseResolver

ROOT = pathlib.Path(__file__).resolve().parent.parent
STEERING = ROOT / ".kiro" / "steering"

VALID_INCLUSION = {"always", "auto", "fileMatch", "manual"}
MODE_KEYS = {
    "always": {"inclusion", "description"},
    "auto": {"inclusion", "name", "description"},
    "fileMatch": {"inclusion", "fileMatchPattern", "description"},
    "manual": {"inclusion", "description"},
}

FRONT_MATTER = re.compile(r"\A---\n(?P<value>.*?)\n---\n", re.S)
FENCE = re.compile(r"```[\s\S]*?```")
TABLE_ROW = re.compile(r"^\s*\|", re.M)
MD_LINK = re.compile(r"\[[^\]]*\]\([^)]+\)")
BOLD = re.compile(r"\*\*")
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
AUTO_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _front_matter(text: str) -> tuple[str, str] | None:
    match = FRONT_MATTER.match(text)
    if match is None:
        return None
    return match.group("value"), text[match.end() :]


def _load_front_matter(value: str) -> dict[str, Any]:
    # A-YAML-SAFELOADER-001: a SafeLoader subclass; see docs/security-dispositions.md
    loaded = yaml.load(value, Loader=UniqueKeyLoader)  # noqa: S506  # nosec B506
    if not isinstance(loaded, dict):
        raise TypeError("front matter must be a YAML mapping")
    if not all(isinstance(key, str) for key in loaded):
        raise TypeError("front-matter keys must be strings")
    return loaded


WORKTREE_PREFIX = ".worktrees/*/"


def _pattern_matches_file(pattern: str) -> bool:
    # A clean checkout holds no git worktrees, so a worktree pattern is checked
    # against the repository-root path it mirrors.
    pattern = pattern.removeprefix(WORKTREE_PREFIX)
    expanded = f"{pattern}/*" if pattern.endswith("/**") else pattern
    return any(candidate.is_file() for candidate in ROOT.glob(expanded))


def _validate_description(name: str, inclusion: str, value: object) -> list[str]:
    issues: list[str] = []
    if not isinstance(value, str) or not value.strip():
        return [f"{name}: missing `description` — mandatory on every steering file"]

    description = value.strip()
    if "\n" in description:
        issues.append(f"{name}: description must be one line")
    if len(SENTENCE_END.findall(description)) != 1 or not description.endswith("."):
        issues.append(f"{name}: description must be one complete sentence")

    stem = pathlib.Path(name).stem.replace("-", " ")
    if description.lower().rstrip(".") == stem:
        issues.append(f"{name}: description only restates the filename")

    if inclusion == "always" and "on every turn" not in description.lower():
        issues.append(f"{name}: always description must state that it applies on every turn")
    elif inclusion == "auto":
        has_prefix = description.startswith(("Use when ", "Use for "))
        if not has_prefix and " when " not in description:
            issues.append(f"{name}: auto description must state when the concern applies")
    elif inclusion == "fileMatch":
        if not description.startswith("Governs ") or " when editing " not in description:
            issues.append(
                f"{name}: fileMatch description must name what it governs and what is edited"
            )
    elif inclusion == "manual" and (
        not description.startswith("Governs ")
        or not any(marker in description for marker in (" when ", " after "))
    ):
        issues.append(
            f"{name}: manual description must name the deliberate activity and lifecycle trigger"
        )
    return issues


def _validate_file_match(name: str, value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return [f"{name}: fileMatchPattern must be a non-empty YAML list"]
    if not all(isinstance(pattern, str) and pattern for pattern in value):
        return [f"{name}: every fileMatchPattern entry must be a non-empty string"]

    patterns = list(value)
    issues: list[str] = []
    if len(patterns) != len(set(patterns)):
        issues.append(f"{name}: fileMatchPattern contains a duplicate pattern")

    for pattern in patterns:
        path = pathlib.PurePosixPath(pattern)
        if path.is_absolute() or ".." in path.parts:
            issues.append(f"{name}: fileMatchPattern must stay inside the repository: {pattern}")
        elif not _pattern_matches_file(pattern):
            issues.append(f"{name}: fileMatchPattern matches no current file: {pattern}")
    return issues


def _validate_file(
    file: pathlib.Path,
) -> tuple[list[str], str | None, str | None, str | None]:
    name = file.name
    split = _front_matter(file.read_text())
    if split is None:
        return [f"{name}: front matter must be the first exact terminated block"], None, None, None
    raw_front_matter, body = split

    try:
        front_matter = _load_front_matter(raw_front_matter)
    except (ConstructorError, TypeError, yaml.YAMLError) as error:
        return [f"{name}: invalid front matter: {error}"], None, None, None

    inclusion = front_matter.get("inclusion")
    if inclusion not in VALID_INCLUSION:
        return (
            [f"{name}: inclusion is {inclusion!r}, expected one of {sorted(VALID_INCLUSION)}"],
            None,
            None,
            None,
        )

    findings = _validate_mode_fields(name, inclusion, front_matter)
    findings.extend(_validate_description(name, inclusion, front_matter.get("description")))
    if inclusion == "fileMatch":
        findings.extend(_validate_file_match(name, front_matter.get("fileMatchPattern")))
    findings.extend(_validate_body(name, body))
    findings.extend(_validate_path_mentions(file, body))

    description = front_matter.get("description")
    return (
        findings,
        inclusion,
        front_matter.get("name") if inclusion == "auto" else None,
        description.strip() if isinstance(description, str) and description.strip() else None,
    )


def _validate_mode_fields(name: str, inclusion: str, front_matter: dict[str, Any]) -> list[str]:
    allowed = MODE_KEYS[inclusion]
    unknown = sorted(set(front_matter) - allowed)
    missing = sorted(allowed - set(front_matter))
    findings: list[str] = []
    if unknown:
        findings.append(f"{name}: keys are invalid for {inclusion}: {unknown}")
    if missing:
        findings.append(f"{name}: keys are required for {inclusion}: {missing}")

    if inclusion == "auto":
        auto_name = front_matter.get("name")
        if not isinstance(auto_name, str) or not AUTO_NAME.fullmatch(auto_name):
            findings.append(f"{name}: auto `name` must be a lowercase dashed identifier")
    return findings


def _validate_body(name: str, body: str) -> list[str]:
    findings: list[str] = []
    plain_body = FENCE.sub("", body)
    if TABLE_ROW.search(plain_body):
        findings.append(f"{name}: contains a markdown table — steering is plain text")
    if MD_LINK.search(plain_body):
        findings.append(f"{name}: contains a markdown link — use a bare path instead")
    if BOLD.search(plain_body):
        findings.append(f"{name}: contains bold — steering is plain text")
    return findings


# Steering cites repository files by bare prose path (links are banned above), so
# link checking never sees a steering citation and a renamed or rewritten target
# rots silently — the agent quietly loses the rule's evidence. These checks make
# the citation itself the contract: the path must exist, a tracked steering file
# must not cite local custody (designating where custody lives uses globs, which
# are skipped), and a `path §N` mention must land on a real heading.
PATH_MENTION = re.compile(
    r"(?<![\w/])((?:docs|src|contracts|validation|infra|frontend|tests|scripts|"
    r"evaluation|internal|\.kiro)/[\w\-./*]*[\w*])"
)
SECTION_MENTION = re.compile(r"\s§([0-9]+[a-z]?)")
HEADING = re.compile(r"^#{1,6}\s+(.+)$", re.M)


def _ignored(paths: set[str]) -> set[str]:
    """Subset of relative paths that .gitignore patterns match (tracked or not)."""
    if not paths:
        return set()
    try:
        out = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            check=False,  # exit 1 means "nothing ignored", not failure
            input="\0".join(sorted(paths)) + "\0",
            text=True,
        ).stdout
    except OSError:
        return set()
    return {p for p in out.split("\0") if p}


def _tracked() -> set[str] | None:
    """Relative paths git tracks, or None when git is unavailable (a tarball checkout).

    Existence on disk is not enough: a citation of a file that is present locally but
    never staged passes every disk check and is dead on a clone.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except OSError, subprocess.CalledProcessError:
        return None
    return {p for p in out.split("\0") if p}


def _is_tracked(token: str, tracked: set[str]) -> bool:
    """Report whether a file is listed, or a directory holds a listed file."""
    return token in tracked or any(p.startswith(token.rstrip("/") + "/") for p in tracked)


def _section_exists(target: pathlib.Path, section: str) -> bool:
    """Match a `§X` mention to a heading that starts with X ('## 3.', '### 4b.')."""
    if target.suffix != ".md" or not target.is_file():
        return True  # only markdown targets carry checkable headings
    marker = re.compile(rf"^{re.escape(section)}[.\s)]")
    return any(marker.match(h.strip()) for h in HEADING.findall(target.read_text()))


def _validate_path_mentions(file: pathlib.Path, body: str) -> list[str]:
    findings: list[str] = []
    name = file.name
    plain_body = FENCE.sub("", body).replace("`", "")
    mentions = []
    for m in PATH_MENTION.finditer(plain_body):
        token = m.group(1).rstrip(".,;:")
        if "*" in token:
            continue  # a glob designates where custody lives; it is not a citation
        mentions.append((token, m.end()))
    is_local = name.startswith("local-")
    ignored = set() if is_local else _ignored({t for t, _ in mentions})
    tracked = None if is_local else _tracked()
    for token, end in mentions:
        target = ROOT / token
        if token in ignored:
            findings.append(
                f"{name}: tracked steering cites local custody ({token}); "
                "the dependency rule is one-way"
            )
            continue
        if not target.exists():
            findings.append(f"{name}: cites {token}, which does not exist")
            continue
        if tracked is not None and not _is_tracked(token, tracked):
            findings.append(
                f"{name}: cites {token}, which exists locally but is not tracked; "
                "stage it or the citation is dead on a clone"
            )
            continue
        section = SECTION_MENTION.match(plain_body, end)
        if section and not _section_exists(target, section.group(1)):
            findings.append(
                f"{name}: cites {token} §{section.group(1)}, but no such heading exists there"
            )
    return findings


def _duplicate_findings(
    names: dict[str, list[str]],
    descriptions: dict[str, list[str]],
) -> list[str]:
    findings: list[str] = []
    for auto_name, owners in names.items():
        if len(owners) > 1:
            findings.append(f"auto name {auto_name!r} is duplicated by {owners}")
    for description, owners in descriptions.items():
        if len(owners) > 1:
            findings.append(f"description is duplicated by {owners}: {description}")
    return findings


def _print_result(files: list[pathlib.Path], modes: Counter[str], findings: list[str]) -> None:
    lines = [
        "STEERING FILE CHECK",
        "=" * 60,
        f"  files:  {len(files)}",
        f"  modes:  {dict(sorted(modes.items()))}",
        "",
    ]
    if findings:
        lines.append(f"FINDINGS ({len(findings)})")
        lines.extend(f"    ✗ {finding}" for finding in findings)
    else:
        lines.append(
            "Every steering file has valid mode-specific front matter and plain-text content."
        )
    sys.stdout.write("\n".join(lines) + "\n")


def main() -> int:
    """Validate all workspace steering files and return a process exit code."""
    findings: list[str] = []
    files = sorted(STEERING.glob("*.md"))
    names: defaultdict[str, list[str]] = defaultdict(list)
    descriptions: defaultdict[str, list[str]] = defaultdict(list)
    modes: Counter[str] = Counter()

    for file in files:
        file_findings, inclusion, auto_name, description = _validate_file(file)
        findings.extend(file_findings)
        if inclusion is not None:
            modes[inclusion] += 1
        if auto_name is not None:
            names[auto_name].append(file.name)
        if description is not None:
            descriptions[description].append(file.name)

    findings.extend(_duplicate_findings(names, descriptions))
    _print_result(files, modes, findings)
    return int(bool(findings))


if __name__ == "__main__":
    sys.exit(main())
