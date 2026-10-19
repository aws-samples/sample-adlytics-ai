# SPDX-License-Identifier: MIT-0
"""The debt-marker gate refuses task markers and retire markers without a condition and owner.

An owner is a tracked docs/ Markdown record whose anchored section (or whole text) carries the
`retire-id:` clause the marker's id names, binding the marker's file to a trigger; a source file, a
non-docs document, a missing anchor, an unrelated record, or a record that only mentions the path
fails.

Each refusal is paired with the compliant form beside it, so a gate that stopped refusing and a
gate that started refusing compliant code both fail here.
"""

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest
from gate_script_support import git, run

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
OWNER = "docs/conventions.md"
UNRELATED = "docs/unrelated.md"
TRACKED = frozenset({OWNER, UNRELATED, "README.md", "src/x.py", "frontend/x.ts"})
V1 = "RETIRE-V1-READ"
V3 = "RETIRE-V3-SHIM"
# The owning record has a clause per retired file; the unrelated one is tracked and owns nothing.
DOCS = {
    OWNER: (
        "# Conventions\n\n## Format\n\nThe v1 read in `src/x.py` goes when every reader is on v2.\n"
        f"\n- retire-id: {V1}; path: src/x.py; when: every reader is on format v2\n"
        "\n### Detail\n\nStill the format section.\n\n## Cutover\n\n"
        "The shim in frontend/x.ts goes after the v3 cutover; src/x.py is unaffected.\n"
        f"\n- retire-id: {V3}; path: `frontend/x.ts`; when: the v3 cutover completes\n"
    ),
    UNRELATED: "# Unrelated\n\nA tracked document that owns no retirement.\n",
    "README.md": "# Readme\n\nNames src/x.py but is not a docs/ record.\n",
}


def _owners(
    gate: ModuleType,
    read: Callable[[str], str | None] = DOCS.get,
    regular: Callable[[str], bool] = lambda _path: True,
) -> object:
    """Return the tracked set above, read from `DOCS` (or `read`), not from the checkout."""
    return gate.Owners(TRACKED, regular, read)


@pytest.fixture(scope="module")
def gate() -> ModuleType:
    """Load the hyphenated script as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_debt_markers", ROOT / "scripts" / "check-debt-markers.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclass resolves its own module while it is created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("word", ["TODO", "FIXME", "XXX", "HACK"])
def test_a_planted_task_marker_fails(gate: ModuleType, word: str) -> None:
    findings = gate.file_findings("src/x.py", f"x = 1\n# {word}: tidy this\n", _owners(gate))

    assert len(findings) == 1
    assert findings[0].startswith(f"src/x.py:2: {word} marker;")


def test_prose_without_a_marker_word_passes(gate: ModuleType) -> None:
    text = "# todo lists and XXXX placeholders and a hackathon are not markers\n"

    assert gate.file_findings("src/x.py", text, _owners(gate)) == []


def test_a_valid_retire_marker_passes_and_is_reported(gate: ModuleType) -> None:
    text = (
        f"# retire: when every reader is on format v2; owner: {OWNER}#format; id: {V1}\nread_v1()\n"
    )

    assert gate.file_findings("src/x.py", text, _owners(gate)) == []
    assert gate.retire_markers("src/x.py", text) == [
        gate.RetireMarker("src/x.py", 1, "when every reader is on format v2", f"{OWNER}#format", V1)
    ]


@pytest.mark.parametrize(
    "comment",
    [
        "// retire: after the v3 cutover; owner: {owner}; id: {id}",
        "/* retire: after the v3 cutover; owner: {owner}; id: {id} */",
        "/* retire: after the v3 cutover; owner: {owner}; id: {id}*/",
        " * retire: after the v3 cutover; owner: {owner}; id: {id}",
        "<!-- retire: after the v3 cutover; owner: {owner}; id: {id} -->",
    ],
)
def test_each_comment_form_carries_a_valid_marker(gate: ModuleType, comment: str) -> None:
    text = comment.format(owner=OWNER, id=V3) + "\n"

    assert gate.file_findings("frontend/x.ts", text, _owners(gate)) == []
    assert [marker.retirement for marker in gate.retire_markers("frontend/x.ts", text)] == [V3]


def test_a_marker_whose_owner_is_not_a_tracked_file_fails(gate: ModuleType) -> None:
    text = f"# retire: after the v3 cutover; owner: docs/no-such-retirement-record.md; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate)) == [
        "src/x.py:1: retire marker owner docs/no-such-retirement-record.md is not a tracked file"
    ]


@pytest.mark.parametrize(
    ("owner", "finding"),
    [
        ("src/x.py", "retire marker owner src/x.py is not a Markdown document under docs/"),
        ("README.md", "retire marker owner README.md is not a Markdown document under docs/"),
        (
            f"{OWNER}#not-a-real-anchor",
            f"retire marker owner {OWNER} has no heading #not-a-real-anchor",
        ),
        (UNRELATED, f"retire marker owner {UNRELATED} has no 'retire-id: {V1}' clause;"),
        # The heading exists and its section mentions the file, but the clause is in another one.
        (
            f"{OWNER}#cutover",
            f"retire marker owner {OWNER}#cutover has no 'retire-id: {V1}' clause;",
        ),
    ],
)
def test_an_owner_that_does_not_own_the_removal_fails(
    gate: ModuleType, owner: str, finding: str
) -> None:
    text = f"# retire: when every reader is on format v2; owner: {owner}; id: {V1}\n"

    findings = gate.file_findings("src/x.py", text, _owners(gate))

    assert len(findings) == 1
    assert findings[0].startswith(f"src/x.py:1: {finding}")


@pytest.mark.parametrize("named", ["lib/src/x.py", "src/x.pyi", "src/x.py.bak", "src/x.py/old"])
def test_an_owner_naming_a_longer_path_does_not_name_the_file(gate: ModuleType, named: str) -> None:
    docs = {UNRELATED: f"# Record\n\nretire-id: {V1}; path: {named}; when: the cutover\n"}
    text = f"# retire: after the cutover; owner: {UNRELATED}; id: {V1}\n"

    findings = gate.file_findings("src/x.py", text, _owners(gate, docs.get))

    assert findings == [
        f"src/x.py:1: retire marker owner {UNRELATED} clause {V1} does not name src/x.py; the "
        "clause names the code it retires"
    ]


def test_a_record_that_only_mentions_the_path_fails_and_its_clause_passes(
    gate: ModuleType,
) -> None:
    mention = "# Record\n\nThis inventory mentions src/x.py but owns no removal condition.\n"
    clause = mention + f"\n- retire-id: {V1}; path: src/x.py; when: the v3 cutover ships\n"
    text = f"# retire: after the v3 cutover; owner: {UNRELATED}; id: {V1}\n"

    mentioned = gate.file_findings("src/x.py", text, _owners(gate, {UNRELATED: mention}.get))
    owned = gate.file_findings("src/x.py", text, _owners(gate, {UNRELATED: clause}.get))

    assert mentioned == [
        f"src/x.py:1: retire marker owner {UNRELATED} has no 'retire-id: {V1}' clause; the "
        "owning record states the condition the marker cites"
    ]
    assert owned == []


def test_a_clause_without_a_trigger_fails(gate: ModuleType) -> None:
    docs = {UNRELATED: f"# Record\n\nretire-id: {V1}; path: src/x.py; when: \n"}
    text = f"# retire: after the v3 cutover; owner: {UNRELATED}; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate, docs.get)) == [
        f"src/x.py:1: retire marker owner {UNRELATED} clause {V1} for src/x.py states no removal "
        "trigger"
    ]


def test_one_identifier_may_retire_several_files_each_by_its_own_clause(gate: ModuleType) -> None:
    docs = {
        UNRELATED: (
            f"# Record\n\n- retire-id: {V1}; path: frontend/x.ts; when: the v3 cutover ships\n"
            f"- retire-id: {V1}; path: src/x.py; when: the v3 cutover ships\n"
        )
    }
    text = f"# retire: after the v3 cutover; owner: {UNRELATED}; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate, docs.get)) == []
    assert gate.file_findings("frontend/x.ts", text, _owners(gate, docs.get)) == []
    # The identifier names the condition; a clause for another identifier does not stand in.
    other = text.replace(V1, "RETIRE-OTHER")
    assert gate.file_findings("src/x.py", other, _owners(gate, docs.get)) == [
        f"src/x.py:1: retire marker owner {UNRELATED} has no 'retire-id: RETIRE-OTHER' clause; "
        "the owning record states the condition the marker cites"
    ]


def test_a_marker_without_a_well_formed_identifier_fails(gate: ModuleType) -> None:
    bare = f"# retire: when every reader is on format v2; owner: {OWNER}#format\n"
    lower = f"# retire: when every reader is on format v2; owner: {OWNER}#format; id: v1-read\n"

    bare_findings = gate.file_findings("src/x.py", bare, _owners(gate))

    assert len(bare_findings) == 1
    assert bare_findings[0].startswith("src/x.py:1: retire marker names no retirement id;")
    assert gate.file_findings("src/x.py", lower, _owners(gate)) == [
        "src/x.py:1: retire marker id v1-read is not an upper-case hyphenated identifier such as "
        "RETIRE-V1-READ"
    ]


def test_an_anchor_covers_its_nested_headings(gate: ModuleType) -> None:
    docs = {
        OWNER: (
            "# Doc\n\n## Format\n\n### Detail\n\n"
            f"- retire-id: {V1}; path: src/x.py; when: the cutover\n\n## Next\n"
        )
    }
    text = f"# retire: after the cutover; owner: {OWNER}#format; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate, docs.get)) == []


def test_an_unreadable_owner_fails(gate: ModuleType) -> None:
    text = f"# retire: after the cutover; owner: {OWNER}; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate, lambda _path: None)) == [
        f"src/x.py:1: retire marker owner {OWNER} could not be read as UTF-8 text"
    ]


def test_an_owner_that_is_not_a_regular_file_fails_before_it_is_read(gate: ModuleType) -> None:
    text = f"# retire: when every reader is on format v2; owner: {OWNER}#format; id: {V1}\n"
    read: list[str] = []

    def reader(path: str) -> str | None:
        read.append(path)
        return DOCS.get(path)

    owners = _owners(gate, reader, regular=lambda path: path != OWNER)

    assert gate.file_findings("src/x.py", text, owners) == [
        f"src/x.py:1: retire marker owner {OWNER} is a symbolic link or sits under one; the "
        "owning record is a regular tracked file"
    ]
    assert read == []


def test_the_checkout_reader_reads_a_document_and_reports_a_missing_one(gate: ModuleType) -> None:
    assert "# " in gate._read_owner("docs/repo-structure.md")
    assert gate._read_owner("docs/no-such-retirement-record.md") is None


def test_a_marker_without_a_condition_fails(gate: ModuleType) -> None:
    text = f"# retire: ; owner: {OWNER}#format; id: {V1}\n"

    assert gate.file_findings("src/x.py", text, _owners(gate)) == [
        "src/x.py:1: retire marker has no removal condition"
    ]


def test_a_marker_without_an_owner_fails(gate: ModuleType) -> None:
    findings = gate.file_findings("src/x.py", "# retire: after the v3 cutover\n", _owners(gate))

    assert len(findings) == 1
    assert findings[0].startswith("src/x.py:1: retire marker names no owning doc;")


def test_a_retire_key_in_code_is_not_a_marker(gate: ModuleType) -> None:
    text = "const fns = { retire: this.pythonFunction('Retire') };\n"

    assert gate.retire_markers("infra/lib/x.ts", text) == []
    assert gate.file_findings("infra/lib/x.ts", text, _owners(gate)) == []


@pytest.mark.parametrize(
    ("path", "gated"),
    [
        ("src/adapters/x.py", True),
        ("infra/lib/stacks/x.ts", True),
        ("infra/bin/app.ts", True),
        ("frontend/src/x.tsx", True),
        ("scripts/x.sh", True),
        ("infra/test/x.test.ts", False),
        ("tests/unit/x.py", False),
        ("scripts/README.md", False),
        ("frontend/package-lock.json", False),
        ("frontend/generated/route-manifest.json", False),
        ("infra/lib/generated/alarm-destinations.ts", False),
        ("src/domain/data_contracts/generated_rows.py", False),
        ("infra/lib/constructs/agent/generated-tools.ts", False),
        ("frontend/vendor/lib.js", False),
    ],
)
def test_the_scope_is_tracked_code_minus_docs_generated_and_vendored(
    gate: ModuleType, path: str, gated: bool
) -> None:
    assert gate.in_scope(path) is gated


def _fixture_repo(tmp_path: Path, files: dict[str, bytes], links: dict[str, str]) -> Path:
    """Commit `files` (bytes as written) and symbolic `links` into a repository holding the gate.

    The gate is copied to `scripts/` so its checkout root is the fixture, and is left untracked so
    only the fixture paths are scanned.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "check-debt-markers.py", repo / "scripts")
    git(repo, "init", "-q", "-b", "main")
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_bytes(content)
    for name, target in links.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).symlink_to(target)
    git(repo, "add", "--", *files, *links)
    return repo


def _run_gate(repo: Path) -> subprocess.CompletedProcess[str]:
    return run([sys.executable, str(repo / "scripts" / "check-debt-markers.py")], repo)


def test_an_invalid_utf8_scoped_file_fails_and_a_utf8_one_passes(tmp_path: Path) -> None:
    marker = b"# " + b"TO" + b"DO" + b": tidy\n"
    bad = _fixture_repo(tmp_path / "bad", {"src/x.py": b"x = '\xff'\n" + marker}, {})
    good = _fixture_repo(tmp_path / "good", {"src/x.py": "x = 'é'\n".encode()}, {})

    failed = _run_gate(bad)
    passed = _run_gate(good)

    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert (
        "src/x.py: is not valid UTF-8 (byte offset 5); the gate cannot check a tracked file it "
        "cannot read" in failed.stdout.splitlines()
    )
    assert "1 unreadable file(s)" in failed.stdout
    assert "DEBT MARKER CHECK FAILED: 1 finding(s)." in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "0 unreadable file(s)" in passed.stdout


def test_a_symlinked_owner_fails_and_a_regular_tracked_owner_passes(tmp_path: Path) -> None:
    code = b"# retire: after the v3 cutover; owner: docs/retirement.md; id: RETIRE-V1-READ\nv1()\n"
    record = b"# Retirement\n\nretire-id: RETIRE-V1-READ; path: src/x.py; when: the v3 cutover\n"
    linked = _fixture_repo(
        tmp_path / "linked", {"src/x.py": code}, {"docs/retirement.md": "../notes/record.md"}
    )
    # The link's target is untracked content that would otherwise satisfy the clause binding.
    (linked / "notes").mkdir()
    (linked / "notes" / "record.md").write_bytes(record)
    regular = _fixture_repo(
        tmp_path / "regular", {"src/x.py": code, "docs/retirement.md": record}, {}
    )

    failed = _run_gate(linked)
    passed = _run_gate(regular)

    assert git(linked, "ls-files", "--stage", "docs/retirement.md").startswith("120000 ")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert (
        "src/x.py:1: retire marker owner docs/retirement.md is a symbolic link or sits under "
        "one; the owning record is a regular tracked file" in failed.stdout.splitlines()
    )
    assert "DEBT MARKER CHECK FAILED: 1 finding(s)." in failed.stdout
    assert passed.returncode == 0, passed.stdout + passed.stderr
    assert "1 retire marker(s)" in passed.stdout


def test_the_repository_passes_the_gate(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main([]) == 0
    out = capsys.readouterr().out
    assert (
        "No task markers; every retire marker names a condition and a tracked docs/ record "
        "whose retire-id clause names its file and trigger." in out
    )


def test_the_report_lists_markers_or_says_there_are_none(
    gate: ModuleType, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main(["--report"]) == 0
    out = capsys.readouterr().out
    markers = [line for line in out.splitlines() if line.count(" | ") == 3]
    assert markers or "No retire markers." in out
