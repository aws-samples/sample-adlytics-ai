# SPDX-License-Identifier: MIT-0
"""Refuse untracked debt markers in tracked code, and hold transitional code to a named exit.

Scope: tracked files under src/, infra/lib/, infra/bin/, frontend/ and scripts/, except Markdown,
lockfiles, generated paths (a `generated` directory, or a `generated_`/`generated-` file name) and
vendored directories.

1. No task-marker word (the four listed in `TASK_MARKERS`) in that scope. A marker records debt
   nobody owns: it states no condition under which the work is done and no document that
   decides it, so it never surfaces again.
2. Code meant to be deleted (a dual-format read, a compatibility shim, a legacy-path branch)
   carries a comment marker instead, on one line:

       <comment leader> retire: <removal condition>; owner: <tracked doc path>[#anchor]; id: <ID>

   The condition says when the code goes; the owner is the TRACKED document that decides it (an
   ADR, a design doc, or a retirement record under docs/). The owner must be a tracked Markdown
   file under docs/, held by git as a regular file and reached through no symbolic link (a tracked
   link would let untracked content stand as the record); an `#anchor`, when given, must name a
   heading of that document (the slug rule
   of scripts/check-links.py); and the owning record (the anchored section, or the whole document
   without an anchor) must carry the retirement clause the marker's `id` names, on one line:

       [- ]retire-id: <ID>; path: <the marker's repository path>; when: <removal trigger>

   The clause is what makes the record the owner of the condition: it binds the identifier to this
   file and states the trigger in the record, so a record that merely mentions the path (an
   inventory line, a code example) owns nothing. The identifier, not the prose, is what binds
   marker and record, so the two condition texts may be worded differently without drifting. An
   identifier is upper case with at least one hyphen (`RETIRE-V1-READ`); several files retired by
   one condition each have their own clause line with the same identifier. A marker without a
   condition, an owner or an identifier, or failing any of those, fails: tracked code never
   depends on an untracked or gitignored record, and an owner that merely exists is not an owner.
   A `retire:` that is not in a comment (an object key such as `retire: this.pythonFunction(...)`)
   is code, not a marker.

3. Every in-scope file is read as UTF-8 text. One that cannot be read (invalid UTF-8, or missing
   or not a file in the checkout) is a finding: the gate does not pass code it did not examine.

`--report` lists every retire marker with its file:line, condition, owner and id (the same
validation applies), so debt kept in code stays visible: `make report-retire-markers`.

Enumerates with git, like the publication-boundary gate; without git it prints a skip line that
`make verify-clean` and `make check-publication` refuse.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCOPES = ("src/", "infra/lib/", "infra/bin/", "frontend/", "scripts/")
# Spelled in parts so this file does not trip its own rule.
TASK_MARKERS = ("TO" + "DO", "FIX" + "ME", "X" * 3, "HA" + "CK")
TASK_MARKER = re.compile(r"\b(" + "|".join(TASK_MARKERS) + r")\b")
RETIRE_WORD = "re" + "tire:"
# The marker sits in a comment: after #, //, /*, a block-comment `*` or <!--.
RETIRE_MARKER = re.compile(r"(?:#|//|/\*|^\s*\*|<!--)\s*" + RETIRE_WORD + r"(?P<body>.*)$")
RETIRE_BODY = re.compile(
    r"^\s*(?P<condition>[^;]*?)\s*;\s*owner:\s*(?P<owner>[^\s;]+?)\s*"
    r"(?:;\s*id:\s*(?P<retirement>[^\s;]+?)\s*)?(?:\*/\}?|-->)?\s*$"
)
RETIREMENT_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+$")
# One retirement clause in the owning record: the identifier, the file it retires, the trigger.
RETIREMENT_CLAUSE = re.compile(
    r"^\s*(?:[-*]\s+)?retire-id:\s*(?P<retirement>[^\s;]+)\s*;\s*path:\s*`?(?P<path>[^\s;`]+)`?"
    r"\s*;\s*when:\s*(?P<trigger>.*?)\s*$"
)
OWNER_SCOPE = "docs/"
OWNER_SUFFIX = ".md"
# Index modes of a regular file; a symbolic link is 120000 and a submodule 160000.
REGULAR_MODES = frozenset({"100644", "100755"})
HEADING = re.compile(r"^(?P<level>#+)\s+(?P<text>.*)$")
EXCLUDED_SUFFIXES = (".md",)
EXCLUDED_NAMES = frozenset({"package-lock.json", "uv.lock", "yarn.lock", "pnpm-lock.yaml"})
VENDORED_DIRECTORIES = frozenset({"vendor", "vendored", "third_party", "node_modules"})


@dataclass(frozen=True, slots=True)
class RetireMarker:
    """One retire marker as written, valid or not."""

    path: str
    line: int
    condition: str
    owner: str
    retirement: str


def in_scope(path: str) -> bool:
    """Return whether a repository-relative path is gated."""
    if not path.startswith(SCOPES):
        return False
    parts = path.split("/")
    name = parts[-1]
    return not (
        name.endswith(EXCLUDED_SUFFIXES)
        or name in EXCLUDED_NAMES
        or name.startswith(("generated_", "generated-"))
        or "generated" in parts[:-1]
        or any(part in VENDORED_DIRECTORIES for part in parts[:-1])
    )


def retire_markers(path: str, text: str) -> list[RetireMarker]:
    """Return every retire marker in one file, with whatever condition and owner it names."""
    markers: list[RetireMarker] = []
    for number, line in enumerate(text.splitlines(), start=1):
        match = RETIRE_MARKER.search(line)
        if match is None:
            continue
        body = RETIRE_BODY.match(match.group("body"))
        if body is None:
            markers.append(RetireMarker(path, number, match.group("body").strip(), "", ""))
        else:
            markers.append(
                RetireMarker(
                    path, number, body["condition"], body["owner"], body["retirement"] or ""
                )
            )
    return markers


def slugify(heading: str) -> str:
    """GitHub-style anchor slug, as scripts/check-links.py computes it (each space a hyphen)."""
    slug = heading.strip().lower().replace("`", "")
    slug = re.sub(r"[^\w\s-]", "", slug)
    return re.sub(r"\s", "-", slug).strip("-")


@dataclass(frozen=True, slots=True)
class Owners:
    """The tracked paths, and a test and a reader for the documents a marker may name as owner.

    `regular` says whether a tracked path is a regular file reached through no symbolic link.
    """

    tracked: frozenset[str]
    regular: Callable[[str], bool]
    read: Callable[[str], str | None]


def section(document: str, anchor: str) -> str | None:
    """Return the text under the heading whose slug is `anchor`, or None when there is none.

    The section runs to the next heading at its level or above, so nested headings belong to it.
    """
    lines = document.splitlines()
    for start, line in enumerate(lines):
        heading = HEADING.match(line)
        if heading is None or slugify(heading["text"]) != anchor:
            continue
        level = len(heading["level"])
        body: list[str] = []
        for following in lines[start + 1 :]:
            nested = HEADING.match(following)
            if nested is not None and len(nested["level"]) <= level:
                break
            body.append(following)
        return "\n".join(body)
    return None


def owner_findings(marker: RetireMarker, owners: Owners) -> list[str]:
    """Return why a marker's owner does not own its removal, or nothing.

    The owner is a tracked docs/ Markdown record; its anchor, when given, is a heading there; and
    the record carries a `retire-id:` clause with the marker's identifier, naming the marker's
    file and stating a trigger, which is what binds this record's condition to this code.
    """
    where = f"{marker.path}:{marker.line}"
    owner, _, anchor = marker.owner.partition("#")
    if not (owner.startswith(OWNER_SCOPE) and owner.endswith(OWNER_SUFFIX)):
        return [
            f"{where}: retire marker owner {owner} is not a Markdown document under {OWNER_SCOPE}"
        ]
    if owner not in owners.tracked:
        return [f"{where}: retire marker owner {owner} is not a tracked file"]
    if not owners.regular(owner):
        return [
            f"{where}: retire marker owner {owner} is a symbolic link or sits under one; the "
            "owning record is a regular tracked file"
        ]
    document = owners.read(owner)
    if document is None:
        return [f"{where}: retire marker owner {owner} could not be read as UTF-8 text"]
    record = document
    if anchor:
        anchored = section(document, anchor)
        if anchored is None:
            return [f"{where}: retire marker owner {owner} has no heading #{anchor}"]
        record = anchored
    return clause_findings(marker, record)


def clause_findings(marker: RetireMarker, record: str) -> list[str]:
    """Return why the owning record has no clause binding the marker's id to its file, or nothing.

    A record that mentions the path without such a clause owns no condition for it.
    """
    where = f"{marker.path}:{marker.line}"
    clauses = [
        clause
        for clause in map(RETIREMENT_CLAUSE.match, record.splitlines())
        if clause is not None and clause["retirement"] == marker.retirement
    ]
    if not clauses:
        return [
            f"{where}: retire marker owner {marker.owner} has no 'retire-id: "
            f"{marker.retirement}' clause; the owning record states the condition the marker cites"
        ]
    # The whole path: `src/x.py` and `lib/src/x.py` or `src/x.pyi` are different files.
    own = [clause for clause in clauses if clause["path"] == marker.path]
    if not own:
        return [
            f"{where}: retire marker owner {marker.owner} clause {marker.retirement} does not "
            f"name {marker.path}; the clause names the code it retires"
        ]
    if not any(re.search(r"[A-Za-z]{3}", clause["trigger"]) for clause in own):
        return [
            f"{where}: retire marker owner {marker.owner} clause {marker.retirement} for "
            f"{marker.path} states no removal trigger"
        ]
    return []


def marker_findings(marker: RetireMarker, owners: Owners) -> list[str]:
    """Return why one retire marker is invalid, or nothing."""
    where = f"{marker.path}:{marker.line}"
    if not marker.owner:
        return [
            f"{where}: retire marker names no owning doc; write "
            f"'{RETIRE_WORD} <removal condition>; owner: <tracked doc path>; id: <ID>'"
        ]
    findings: list[str] = []
    if not re.search(r"[A-Za-z]{3}", marker.condition):
        findings.append(f"{where}: retire marker has no removal condition")
    if not marker.retirement:
        findings.append(
            f"{where}: retire marker names no retirement id; write "
            f"'{RETIRE_WORD} <removal condition>; owner: <tracked doc path>; id: <ID>'"
        )
        return findings
    if RETIREMENT_ID.match(marker.retirement) is None:
        findings.append(
            f"{where}: retire marker id {marker.retirement} is not an upper-case hyphenated "
            "identifier such as RETIRE-V1-READ"
        )
        return findings
    findings.extend(owner_findings(marker, owners))
    return findings


def file_findings(path: str, text: str, owners: Owners) -> list[str]:
    """Return every finding in one in-scope file."""
    findings = [
        f"{path}:{number}: {match.group(1)} marker; record the work in a tracked doc, or mark "
        f"transitional code '{RETIRE_WORD} <condition>; owner: <tracked doc>'"
        for number, line in enumerate(text.splitlines(), start=1)
        for match in TASK_MARKER.finditer(line)
    ]
    for marker in retire_markers(path, text):
        findings.extend(marker_findings(marker, owners))
    return findings


def _tracked() -> dict[str, str] | None:
    """Each path git tracks with its index mode, or None when git is unavailable (a tarball)."""
    try:
        # A-SUBPROCESS-001: fixed git argv; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        result = subprocess.run(
            ["git", "ls-files", "--stage", "-z"],  # noqa: S607
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None
    modes: dict[str, str] = {}
    for entry in result.stdout.split("\0"):
        if not entry:
            continue
        # `<mode> <object> <stage>\t<path>`
        meta, _, path = entry.partition("\t")
        modes[path] = meta.split(" ", 1)[0]
    return modes


def _unlinked(path: str) -> bool:
    """Return whether no component of `path` under the checkout is a symbolic link."""
    current = ROOT
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            return False
    return True


def _regular(modes: dict[str, str], path: str) -> bool:
    """Return whether git holds `path` as a regular file and the checkout reaches it unlinked."""
    return modes.get(path) in REGULAR_MODES and _unlinked(path)


def _read(path: str) -> str:
    """Read one in-scope file; the caller turns any failure into a finding."""
    return (ROOT / path).read_text(encoding="utf-8")


def unreadable_finding(path: str, error: OSError | UnicodeDecodeError) -> str:
    """Return the finding for an in-scope file the gate could not read, so it cannot pass."""
    if isinstance(error, UnicodeDecodeError):
        reason = f"is not valid UTF-8 (byte offset {error.start})"
    else:
        reason = f"could not be read ({error.strerror or type(error).__name__})"
    return f"{path}: {reason}; the gate cannot check a tracked file it cannot read"


def _read_owner(path: str) -> str | None:
    """Read an owning document, or return None (reported as a finding) when it cannot be read."""
    try:
        return (ROOT / path).read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
        return None


def main(argv: list[str]) -> int:
    """Run the gate, or with --report list every retire marker; nonzero on any finding."""
    report = argv == ["--report"]
    if argv and not report:
        print("usage: check-debt-markers.py [--report]", file=sys.stderr)  # noqa: T201
        return 2
    modes = _tracked()
    if modes is None:
        print(  # noqa: T201
            "DEBT MARKER CHECK skipped: git is unavailable, so tracked files cannot be listed"
        )
        return 0
    owners = Owners(frozenset(modes), partial(_regular, modes), _read_owner)
    scoped = sorted(path for path in modes if in_scope(path))
    findings: list[str] = []
    markers: list[RetireMarker] = []
    unreadable = 0
    for path in scoped:
        try:
            text = _read(path)
        except (OSError, UnicodeDecodeError) as error:
            findings.append(unreadable_finding(path, error))
            unreadable += 1
            continue
        findings.extend(file_findings(path, text, owners))
        markers.extend(retire_markers(path, text))
    print(  # noqa: T201
        f"DEBT MARKER CHECK: {len(scoped)} tracked files under {', '.join(SCOPES)}; "
        f"{len(markers)} retire marker(s); {unreadable} unreadable file(s)"
    )
    if report:
        for marker in markers:
            print(  # noqa: T201
                f"{marker.path}:{marker.line} | {marker.condition} | {marker.owner} | "
                f"{marker.retirement}"
            )
        if not markers:
            print("No retire markers.")  # noqa: T201
    for finding in findings:
        print(finding)  # noqa: T201
    if findings:
        print(f"DEBT MARKER CHECK FAILED: {len(findings)} finding(s).")  # noqa: T201
        return 1
    print(  # noqa: T201
        "No task markers; every retire marker names a condition and a tracked docs/ record "
        "whose retire-id clause names its file and trigger."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
