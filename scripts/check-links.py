#!/usr/bin/env python3
# SPDX-License-Identifier: MIT-0
"""Validate every internal link and heading anchor in docs/.

Catches the two failure modes that markdown tooling does not: a link to a file
that was renamed or deleted, and a section anchor that no longer matches a
heading after a section was renumbered.

Usage:  python3 scripts/check-links.py
Exits non-zero on any finding, so it can run in CI.
"""

import pathlib
import re
import subprocess
import sys
from collections import Counter

ROOT = pathlib.Path(__file__).resolve().parent.parent


def tracked_files():
    """Paths git tracks, or None when git is unavailable (a tarball checkout).

    Used for the one-way dependency rule below; without git the rule is
    unenforceable and silently skipped rather than guessed at.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
            text=True,
        ).stdout
    except OSError, subprocess.CalledProcessError:
        return None
    return {ROOT / p for p in out.split("\0") if p}


def ignored(paths):
    """Subset of paths that .gitignore patterns match (tracked or not).

    `--no-index` makes patterns authoritative even for still-tracked files, so
    a path scheduled to go local (ignored but not yet untracked) already counts
    as local custody and the rule below holds through the migration.
    """
    if not paths:
        return set()
    rel = [str(p.relative_to(ROOT)) for p in paths]
    try:
        out = subprocess.run(
            ["git", "check-ignore", "--no-index", "-z", "--stdin"],
            cwd=ROOT,
            capture_output=True,
            input="\0".join(rel) + "\0",
            text=True,
        ).stdout
    except OSError:
        return set()
    return {ROOT / p for p in out.split("\0") if p}


# Every tracked markdown file is scanned, wherever it lives. A fixed directory list
# once left contracts/, infra/, validation/, scripts/ and the security register
# outside the checker, and a dead link in validation/README.md passed for weeks.
#
# Local working documents and .kiro state are scanned too WHEN PRESENT, so the
# operator's working documents keep resolving into docs/; a clone that lacks them
# simply scans less. .kiro is scanned whole so gitignored local steering is covered.
LOCAL_SCAN_DIRS = [
    ROOT / "internal",
    ROOT / ".kiro",
]

# Fallback when git is unavailable (a tarball checkout): the directories and root
# files that carry the published markdown.
FALLBACK_SCAN_DIRS = [
    ROOT / "docs",
    ROOT / "contracts",
    ROOT / "evaluation",
    ROOT / "infra",
    ROOT / "scripts",
    ROOT / "tests",
    ROOT / "validation",
]
ROOT_FILES = [
    ROOT / "README.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "CODE_OF_CONDUCT.md",
]

LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
HEADING = re.compile(r"^#+\s+(.*)$", re.M)
FENCE = re.compile(r"```[\s\S]*?```")


def slugify(heading):
    """GitHub-style anchor slug.

    Note: GitHub maps EACH space to a hyphen rather than collapsing runs, so
    "structure — the model" becomes "structure--the-model" once the em-dash is
    stripped and its surrounding spaces both survive. Collapsing whitespace here
    produces a slug that looks plausible and silently fails to match.
    """
    s = heading.strip().lower()
    s = re.sub(r"`", "", s)
    s = re.sub(r"[^\w\s-]", "", s)
    return re.sub(r"\s", "-", s).strip("-")


def anchors(text):
    return {slugify(h) for h in HEADING.findall(text)}


def strip_fences(text):
    """Links inside code fences are illustrative, not navigational."""
    return FENCE.sub("", text)


findings = []
checked = 0

# One-way dependency rule (meta-style-steering, tracked and local content):
# tracked content never references local custody. A link from a tracked file to
# an ignored path would resolve on this machine — where local files exist — and
# rot silently for every clone that lacks them, so existence checking alone
# cannot catch it.
TRACKED = tracked_files()

if TRACKED is not None:
    scanned = {p for p in TRACKED if p.suffix == ".md" and p.is_file()}
else:
    scanned = {p for d in FALLBACK_SCAN_DIRS if d.is_dir() for p in d.rglob("*.md")}
    scanned |= {p for p in ROOT_FILES if p.is_file()}
scanned |= {p for d in LOCAL_SCAN_DIRS if d.is_dir() for p in d.rglob("*.md")}
# Dependency trees are never ours to check.
files = sorted(p for p in scanned if "node_modules" not in p.parts)
link_targets = set()


def _is_tracked(dest):
    """A file is tracked if listed; a directory is tracked if it holds a tracked file."""
    return dest in TRACKED or (dest.is_dir() and any(dest in p.parents for p in TRACKED))


for f in files:
    text = f.read_text()
    body = strip_fences(text)
    own_anchors = anchors(text)

    for m in LINK.finditer(body):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        checked += 1

        # in-page anchor
        if target.startswith("#"):
            if target[1:] not in own_anchors:
                findings.append(
                    f"{f.relative_to(ROOT)} -> {target}  (no such heading in this file)"
                )
            continue

        path_part, _, frag = target.partition("#")
        dest = (f.parent / path_part).resolve()

        if not dest.exists():
            findings.append(f"{f.relative_to(ROOT)} -> {target}  (file not found)")
            continue

        if TRACKED is not None and f in TRACKED:
            # A source that ignore patterns already claim is local custody in
            # transit (ignored while still tracked, mid-migration); the rule
            # binds sources that will remain tracked.
            link_targets.add((f, target, dest))

        if frag and dest.suffix == ".md":
            if frag not in anchors(dest.read_text()):
                findings.append(f"{f.relative_to(ROOT)} -> {target}  (no such heading in target)")

_local_paths = ignored(
    {d for _, _, d in link_targets} | {f for f, _, _ in link_targets},
)
for f, target, dest in sorted(
    (t for t in link_targets if t[0] not in _local_paths),
    key=lambda t: str(t[0]),
):
    if dest in _local_paths:
        findings.append(
            f"{f.relative_to(ROOT)} -> {target}  (tracked file links into local custody; "
            "tracked content never references gitignored paths)"
        )
    elif not _is_tracked(dest):
        # Present on this machine, absent from the index: resolves here, dead on a clone.
        findings.append(
            f"{f.relative_to(ROOT)} -> {target}  (target exists locally but is not tracked; "
            "stage it with the change that links to it)"
        )


def _tree(path):
    parts = path.relative_to(ROOT).parts
    return parts[0] if len(parts) > 1 else "root"


counts = Counter(_tree(f) for f in files)
breakdown = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
print("LINK AND ANCHOR CHECK")
print("=" * 60)
print(f"  files:  {len(files)}  ({breakdown})")
print(f"  links:  {checked} internal")
print()

if findings:
    print(f"FINDINGS ({len(findings)})")
    for x in findings:
        print(f"    ✗ {x}")
    sys.exit(1)

print("All internal links and anchors resolve.")
