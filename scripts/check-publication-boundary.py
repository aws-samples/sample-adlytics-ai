#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Fail when tracked content carries what must never be published.

The tracked tree is publication-appropriate at every commit (meta-delivery-git).
check-links enforces the one-way custody rule for markdown links; this gate covers
what links cannot see, in every tracked text file:

- a reference to a specific file inside local custody (internal/…, .kiro/specs/…,
  .kiro/steering/local-*.md). Naming where custody lives — a directory or a glob —
  is designation and stays legal; naming a file inside it is a dependency.
- a 12-digit AWS account ID other than a documented placeholder or a public
  publisher account. A cell of a declared microcent amount column in a generated
  fixture (`MICROCENT_COLUMNS`) is an amount, not an account, and is the one exception.
- a literal AWS profile value after --profile, PROFILE= or AWS_PROFILE=. Commands
  in tracked content use a placeholder; the operator binds the value locally.
- an encoded JWT or JWE header literal. Fixtures build tokens from readable parts
  (tests/unit/fictional_tokens.py), so a reader can see what a committed token says.
- a generated-looking secret literal: a quoted run of 20+ base64url characters with
  letters in both cases and two or more digit runs between letters. A fictional
  secret is spelled as words. This is a backstop for the fixture convention, not a
  secrets scanner: a standard-base64 value (`+`, `/`, `=`), an all-upper or
  all-lower value, one with a single inner digit run, an unquoted `KEY=value` or
  `key: value`, and an AWS access key ID all pass it. The publication scan's
  secrets rules cover those.
- in source files (.py, .ts, .tsx, .js, .mjs), a Bandit or Semgrep suppression
  marker that does not name a reviewed rule (docs/security-dispositions.md).

Files that .gitignore already claims (local custody still being untracked) are
skipped: they leave the index with the change that ignores them.

Usage:  uv run python scripts/check-publication-boundary.py
Exits non-zero on any finding.
"""

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

CUSTODY_FILE = re.compile(
    r"(?<![\w/.-])("
    r"internal/[\w.-]+(?:/[\w.-]+)*\.\w+"
    r"|\.kiro/specs/[\w.-]+"
    r"|\.kiro/steering/local-[\w-]+\.md"
    r")"
)

ACCOUNT_ID = re.compile(r"(?<![\w.-])(\d{12})(?![\w])")
# AWS documentation placeholders, and the public account that publishes the AWS
# Lambda Web Adapter layer. Repeated-digit values are placeholders by construction.
ALLOWED_ACCOUNT_IDS = {
    "111122223333",
    "444455556666",
    "123456789012",
    "753240598075",
}

# Generated fixture columns that carry AMC microcent amounts (÷1e8, the source contract's
# `scale_divisor`). A realistic window spend reaches 12 digits, so a cell of exactly this
# column is not an account ID. Closed: any other column, or any other file, is still scanned.
MICROCENT_COLUMNS = {
    "tests/fixtures/generated/amc-reach-frequency.csv": "spend",
    # Pack 5's sponsored window spend is the same microcent `spend`, summed over a window.
    "tests/fixtures/generated/amc-exposure-groups.csv": "sponsored_window_spend",
    # Packs 6 and 7 carry the same microcent sponsored window spend per frequency bucket.
    "tests/fixtures/generated/amc-exposure-frequency-platform.csv": "sponsored_window_spend",
    "tests/fixtures/generated/amc-exposure-frequency-channel.csv": "sponsored_window_spend",
}
PROFILE_ASSIGNMENT = re.compile(r"\b(?:AWS_)?PROFILE=([A-Za-z0-9_][A-Za-z0-9_.-]*)")
PROFILE_FLAG = re.compile(r"--profile[ =]([A-Za-z0-9_][A-Za-z0-9_.-]*)")
# `--profile` in prose ("--profile must name…") is not a binding; a command line
# carries a second flag or starts with a command.
COMMAND_LINE = re.compile(
    r"--(?!profile\b)[a-z][\w-]*|^\s*(?:\$\s+)?(?:uv|python3?|aws|make|npx|cdk)\b"
)
# Illustrative values an operator must replace; never a real binding. The prose
# words cover flag names quoted in messages ("--secret and --profile are required").
ALLOWED_PROFILES = {"my-profile", "is", "are", "must", "and", "or", "stays", "flag"}

# An encoded JWT or JWE header literal. Fixtures build tokens from readable parts instead
# (tests/unit/fictional_tokens.py), so a reader can see what a committed token says.
ENCODED_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{2,}")

# A generated-looking secret: 20+ base64url characters with letters in both cases and digits
# scattered between letters. `-` and `_` are in the class because base64url is how most real
# tokens are written. A fictional secret is spelled as words ("Fictional-Agent-Secret-For-Tests").
GENERATED_SECRET = re.compile(r"""(["'`])([A-Za-z0-9_-]{20,})\1""")
_INNER_DIGITS = re.compile(r"(?<=[A-Za-z])[0-9]+(?=[A-Za-z])")
_MIN_INNER_DIGIT_RUNS = 2

# A suppression marker must name the rule it suppresses. Bandit reads a nosec whose IDs are all
# unknown as a blanket nosec (bandit/core/manager.py _parse_nosec_comment), so the IDs are a closed,
# reviewed set; Semgrep IDs must be namespaced (its ignore documentation).
# Matched in any letter case (O-MARKER-FORMS-001). Semgrep needs no comment leader: it honours a
# space then `nosem` or `nosemgrep` anywhere on the line, in any case (NOSEM_INLINE_RE in
# semgrep/constants.py, 1.173.0), and reads one with no IDs as a blanket suppression. So the
# marker word counts when it starts the line or follows whitespace, prose in a comment included,
# and also when glued to a `#`, `/`, `*`, `-` or `!` leader, which Semgrep ignores but is refused
# for strictness (O-MARKER-LEADERS-001). Write the word in backticks or after a quote to name it.
NOSEC = re.compile(r"#\s*nosec\b(?P<rest>[^#]*)", re.IGNORECASE)
REVIEWED_BANDIT_IDS = frozenset({"B105", "B106", "B310", "B505", "B506", "B608"})
SEMGREP_MARKER = re.compile(r"(?:^|(?<=[\s#/*!-]))nosem(?:grep)?(?P<rest>[^#]*)", re.IGNORECASE)
BLOCK_COMMENT_CLOSE = re.compile(r"\s*(?:\*/|-->).*$")
SEMGREP_IDS = re.compile(
    r":\s*[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)+(?:,\s*[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)+)*\s*"
)
MARKED_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".mjs")

# Lockfiles carry integrity hashes and version numbers, never authored content.
SKIP_NAMES = {"package-lock.json", "uv.lock"}


def _git(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """Run one git command in the repository root and return its stdout."""
    return subprocess.run(  # noqa: S603 - fixed git argv, no shell, no external input
        ["git", *args],  # noqa: S607
        cwd=ROOT,
        capture_output=True,
        check=check,
        text=True,
        input=stdin,
    ).stdout


def _candidates() -> list[str]:
    """Return tracked paths that stay tracked: not lockfiles, not claimed by .gitignore."""
    tracked = [p for p in _git("ls-files", "-z").split("\0") if p]
    if not tracked:
        return []
    # check-ignore exits 1 when nothing matches; that is an answer, not a failure.
    out = _git(
        "check-ignore",
        "--no-index",
        "-z",
        "--stdin",
        stdin="\0".join(tracked) + "\0",
        check=False,
    )
    claimed = set(out.split("\0"))
    return [p for p in tracked if p not in claimed and pathlib.PurePath(p).name not in SKIP_NAMES]


def _text(path: pathlib.Path) -> str | None:
    """Return a file's text, or None when it is unreadable or binary."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None  # binary
    return raw.decode("utf-8", errors="replace")


def _generated_secret(value: str) -> bool:
    """Return True for a literal shaped like a generated credential, not a word or digest."""
    return (
        value != value.lower()
        and value != value.upper()
        and len(_INNER_DIGITS.findall(value)) >= _MIN_INNER_DIGIT_RUNS
    )


def _credential_findings(where: str, line: str) -> list[str]:
    """Return gate C and D findings for one line: encoded JWTs and generated-looking secrets."""
    found: list[str] = []
    if ENCODED_JWT.search(line):
        found.append(f"{where}: carries an encoded JWT literal; build it from readable parts")
    for m in GENERATED_SECRET.finditer(line):
        if _generated_secret(m.group(2)):
            found.append(
                f"{where}: carries a generated-looking secret literal; "
                "spell a fictional value as words"
            )
    return found


def _marker_findings(where: str, line: str) -> list[str]:
    """Return gate E findings for one source line: suppressions that name no reviewed rule."""
    found: list[str] = []
    for m in NOSEC.finditer(line):
        ids = [t.strip() for t in m.group("rest").split(",")]
        if not ids or not all(t in REVIEWED_BANDIT_IDS for t in ids):
            found.append(f"{where}: nosec must name only reviewed Bandit test IDs")
    for m in SEMGREP_MARKER.finditer(line):
        if SEMGREP_IDS.fullmatch(BLOCK_COMMENT_CLOSE.sub("", m.group("rest"))) is None:
            found.append(f"{where}: `nosemgrep` must name namespaced rule IDs")
    return found


def _amount_cell(rel: str, header: str, line: str) -> str | None:
    """Return this line's cell of the file's declared microcent column, if it has one."""
    column = MICROCENT_COLUMNS.get(rel)
    if column is None or line == header:
        return None
    names = header.split(",")
    cells = line.split(",")
    return cells[names.index(column)] if column in names and len(cells) == len(names) else None


def _findings(rel: str, text: str) -> list[str]:
    """Return every boundary violation in one file's text, located by line."""
    found: list[str] = []
    lines = text.splitlines()
    header = lines[0] if lines else ""
    for lineno, line in enumerate(lines, start=1):
        amount = _amount_cell(rel, header, line)
        for m in CUSTODY_FILE.finditer(line):
            found.append(f"{rel}:{lineno}: references local custody file {m.group(1)}")
        for m in ACCOUNT_ID.finditer(line):
            value = m.group(1)
            if value not in ALLOWED_ACCOUNT_IDS and len(set(value)) > 1 and value != amount:
                found.append(f"{rel}:{lineno}: carries a 12-digit account-shaped value {value}")
        bindings = [m.group(1) for m in PROFILE_ASSIGNMENT.finditer(line)]
        if COMMAND_LINE.search(line):
            bindings += [m.group(1) for m in PROFILE_FLAG.finditer(line)]
        for value in bindings:
            if value not in ALLOWED_PROFILES:
                found.append(
                    f"{rel}:{lineno}: binds a literal AWS profile ({value}); use a placeholder"
                )
        found.extend(_credential_findings(f"{rel}:{lineno}", line))
        if rel.endswith(MARKED_SUFFIXES):
            found.extend(_marker_findings(f"{rel}:{lineno}", line))
    return found


def main() -> int:
    """Scan tracked content and report every finding; return the exit status."""
    try:
        candidates = _candidates()
    except OSError, subprocess.CalledProcessError:
        sys.stdout.write(
            "PUBLICATION BOUNDARY CHECK skipped: git is unavailable, "
            "so tracked content is unknown.\n"
        )
        return 0

    findings: list[str] = []
    scanned = 0
    for rel in candidates:
        text = _text(ROOT / rel)
        if text is None:
            continue
        scanned += 1
        findings.extend(_findings(rel, text))

    lines = ["PUBLICATION BOUNDARY CHECK", "=" * 60, f"  scanned: {scanned} tracked text files", ""]
    if findings:
        lines.append(f"FINDINGS ({len(findings)})")
        lines += [f"    ✗ {finding}" for finding in findings]
    else:
        lines.append(
            "No custody file references, account IDs, literal profiles, encoded JWTs, "
            "generated-looking secrets or unnamed suppressions in tracked content."
        )
    sys.stdout.write("\n".join(lines) + "\n")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
