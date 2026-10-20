# SPDX-License-Identifier: MIT-0
"""Mutation coverage: the fingerprint, skip-on-current and survivor verdicts.

A unit whose coverage-map entry matches its fingerprint is never re-run, and any change to its
source blob, a mapped test's blob, the operator config or the semantic category set makes it
stale. Survivor ids and verdicts carry forward across runs and are never renumbered or deleted,
and a KILLED verdict always names its test. A failure here means the owner either pays for a
rerun that cannot find anything new, or trusts a coverage entry the code has moved away from.

Each fixture repository is committed with git plumbing (write-tree, commit-tree, update-ref), so
no commit hook runs, and every tool the run would start is replaced by a stub.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from gate_script_support import git

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
UNIT = "src/domain/sample.py"
TEST = "tests/unit/test_sample.py"
SOURCE = "def is_positive(value: int) -> bool:\n    return value > 0\n"
TEST_TEXT = (
    "from domain.sample import is_positive\n\n\n"
    "def test_one_is_positive() -> None:\n    assert is_positive(1)\n"
)
STEERING = (
    "# Mutation review\n\n## Which track owns which mutation\n\nIntro.\n\n"
    "- A lookup table mapping the right key to the wrong value, where both\n"
    "  values are valid members of the target type.\n"
    "- An enum member substituted for a sibling member.\n\n## Resolving a survivor\n\n- Other.\n"
)
PRIORITIES = (
    "version: 1\ntiers:\n"
    f"  T1:\n    description: core\n    units:\n    - unit: {UNIT}\n      tests: [{TEST}]\n"
    "    - unit: src/domain/untested.py\n      tests: []\n"
    "  T2:\n    description: other\n    units: []\n"
    "  T3:\n    description: infra\n    units: []\n"
)
IDENTITY = ("-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid")


@pytest.fixture(scope="module")
def mutation() -> ModuleType:
    """Load scripts/mutation.py as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location("mutation", ROOT / "scripts" / "mutation.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def commit_files(repo: Path, files: dict[str, str]) -> str:
    """Write and commit `files` with plumbing, so no hook runs; return the new HEAD."""
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "--", *files)
    tree = git(repo, "write-tree")
    try:
        parents = ["-p", git(repo, "rev-parse", "--verify", "-q", "HEAD")]
    except RuntimeError:
        parents = []
    commit = git(repo, *IDENTITY, "commit-tree", tree, *parents, "-m", "fixture")
    git(repo, "update-ref", "HEAD", commit)
    return commit


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a repository with a tested unit, an untested unit, the steering and priorities."""
    # make verify-clean exports the outer commit; a record here must describe the fixture checkout.
    monkeypatch.delenv("RUN_RECORD_SOURCE_COMMIT", raising=False)
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    commit_files(
        root,
        {
            UNIT: SOURCE,
            "src/domain/untested.py": "X = 1\n",
            TEST: TEST_TEXT,
            ".kiro/steering/meta-testing-mutation.md": STEERING,
            "mutation-priorities.yaml": PRIORITIES,
        },
    )
    return root


def context(mutation: ModuleType, repo: Path) -> Any:
    """Return a context over `repo` whose records and map live outside the repository."""
    return mutation.Context(repo, repo.parent / "runs", io.StringIO())


def state_of(mutation: ModuleType, repo: Path, unit: str = UNIT) -> str:
    """Return the unit's state as `status` computes it, from a freshly read context."""
    found = {u.unit: s for u, s, _ in mutation.states(context(mutation, repo))}
    return found[unit]


def record_current(mutation: ModuleType, repo: Path, record: str = "mutation/fixture") -> None:
    """Write a coverage-map entry carrying the unit's current digest."""
    ctx = context(mutation, repo)
    unit = ctx.priorities.find(UNIT)
    print_ = mutation.fingerprint(unit, ctx.inputs())
    cmap = mutation.load_map(mutation.map_path(ctx.runs))
    cmap["units"][UNIT] = {
        "unit": UNIT,
        "digest": mutation.digest(print_),
        "fingerprint": print_,
        "record": record,
        "survivors": [],
    }
    mutation.save_map(mutation.map_path(ctx.runs), cmap)


def refusing_runners(mutation: ModuleType) -> Any:
    """Runners whose every process fails the test if it is started."""

    def never(*_args: object) -> Any:
        pytest.fail("a current unit started a process")

    return mutation.Runners(baseline=never, tests=never, mechanical=never)


def test_the_fingerprint_names_every_blob_the_tool_and_both_versions(
    mutation: ModuleType, repo: Path
) -> None:
    ctx = context(mutation, repo)
    print_ = mutation.fingerprint(ctx.priorities.find(UNIT), ctx.inputs())
    assert print_["unit_blobs"] == {UNIT: git(repo, "rev-parse", f"HEAD:{UNIT}")}
    assert print_["test_blobs"] == {TEST: git(repo, "rev-parse", f"HEAD:{TEST}")}
    assert print_["tool"]["name"] == "cosmic-ray"
    assert len(print_["semantic_set_version"]) == 12
    assert len(print_["operator_config_sha256"]) == 64


def test_a_unit_without_an_entry_is_missing_and_one_without_tests_is_untested(
    mutation: ModuleType, repo: Path
) -> None:
    assert state_of(mutation, repo) == "missing"
    assert state_of(mutation, repo, "src/domain/untested.py") == "untested"


def test_a_current_unit_is_skipped_without_starting_any_process(
    mutation: ModuleType, repo: Path
) -> None:
    record_current(mutation, repo, "mutation/20261019T000000.000000Z-abcdefabcdef")
    assert state_of(mutation, repo) == "current"
    ctx = context(mutation, repo)
    assert mutation.run_unit(ctx, UNIT, runners=refusing_runners(mutation)) == 0
    assert "current (record mutation/20261019T000000.000000Z-abcdefabcdef), skipping" in (
        ctx.out.getvalue()
    )


def test_a_changed_source_blob_makes_the_unit_stale(mutation: ModuleType, repo: Path) -> None:
    record_current(mutation, repo)
    commit_files(repo, {UNIT: SOURCE.replace("> 0", ">= 0")})
    assert state_of(mutation, repo) == "stale"


def test_a_changed_test_blob_makes_the_unit_stale(mutation: ModuleType, repo: Path) -> None:
    record_current(mutation, repo)
    commit_files(
        repo, {TEST: TEST_TEXT + "\n\ndef test_two() -> None:\n    assert is_positive(2)\n"}
    )
    assert state_of(mutation, repo) == "stale"


def test_an_unrelated_commit_keeps_the_unit_current(mutation: ModuleType, repo: Path) -> None:
    record_current(mutation, repo)
    commit_files(repo, {"README.md": "unrelated\n"})
    assert state_of(mutation, repo) == "current"


def test_a_changed_operator_config_makes_the_unit_stale(
    mutation: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record_current(mutation, repo)
    monkeypatch.setattr(mutation, "COSMIC_RAY_TEMPLATE", mutation.COSMIC_RAY_TEMPLATE + "\n")
    assert state_of(mutation, repo) == "stale"


def test_a_changed_semantic_category_set_makes_the_unit_stale(
    mutation: ModuleType, repo: Path
) -> None:
    record_current(mutation, repo)
    steering = STEERING.replace("- An enum member", "- A sibling enum member")
    commit_files(repo, {".kiro/steering/meta-testing-mutation.md": steering})
    assert state_of(mutation, repo) == "stale"


def test_a_steering_file_without_the_category_section_fails_loudly(
    mutation: ModuleType,
) -> None:
    with pytest.raises(mutation.MutationError, match="not found"):
        mutation.semantic_categories("# Mutation review\n\n## Before running\n\n- A rule.\n")


def test_an_unknown_or_untested_unit_is_refused(mutation: ModuleType, repo: Path) -> None:
    ctx = context(mutation, repo)
    with pytest.raises(mutation.MutationError, match="UNKNOWN_UNIT"):
        mutation.run_unit(ctx, "src/domain/absent.py", runners=refusing_runners(mutation))
    with pytest.raises(mutation.MutationError, match="UNTESTED"):
        mutation.run_unit(ctx, "src/domain/untested.py", runners=refusing_runners(mutation))


def fake_tool(mutation: ModuleType, mutants: list[dict[str, Any]], dirty: str | None = None) -> Any:
    """Return a mechanical tool stub yielding `mutants`, optionally leaving `dirty` modified."""

    def tool(_unit: object, root: Path, _staging: Path) -> Any:
        if dirty is not None:
            (root / dirty).write_text("mutated\n")
        return mutation.ToolRun(
            0, b"tool output\n", mutants, {"raw/tool.txt": b"raw\n"}, ["tool"], 0.5
        )

    return tool


def mutant(status: str, mutation_text: str, killed_by: str | None = None) -> dict[str, Any]:
    """One normalised mutant of the fixture unit."""
    return {
        "file": UNIT,
        "line": 2,
        "operator": "core/ReplaceComparisonOperator_Gt_GtE",
        "mutation": mutation_text,
        "status": status,
        "killed_by": killed_by,
    }


def green(*_args: object) -> tuple[int, bytes]:
    """Return the result of a passing test run."""
    return 0, b"1 passed\n"


def test_a_missing_unit_runs_writes_a_verifiable_record_and_then_is_skipped(
    mutation: ModuleType, repo: Path
) -> None:
    ctx = context(mutation, repo)
    runners = mutation.Runners(
        baseline=green,
        tests=green,
        mechanical=fake_tool(mutation, [mutant("survived", "`a > 0` becomes `a >= 0`")]),
    )
    assert mutation.run_unit(ctx, UNIT, runners=runners) == 0
    entry = mutation.load_map(mutation.map_path(ctx.runs))["units"][UNIT]
    record = ctx.runs / entry["record"]
    assert mutation.run_record.verify_record(record) == []
    run = json.loads((record / "run.json").read_text())
    assert run["unit"] == UNIT
    assert run["track"] == "mechanical"
    assert run["digest"] == entry["digest"]
    # The common run.json contract, derived rather than asserted by the writer.
    assert run["canonical"] is False
    assert run["source_commit"] == run["head"]
    assert run["context"] == "checkout"
    assert (run["porcelain_before"], run["porcelain_after"], run["clean"]) == (0, 0, True)
    assert run["report_missing"] is False
    assert run["outcome"] == "pass"
    assert run["utc_start"] <= run["utc_end"]
    assert {"python", "git", "platform", "cosmic-ray"} <= set(run["tool_versions"])
    assert run["counts"] == {
        "generated": 1,
        "killed": 0,
        "survived": 1,
        "timeout": 0,
        "no_coverage": 0,
        "errors": 0,
    }
    assert [s["id"] for s in entry["survivors"]] == ["S-01"]
    assert (record / "semantic-worksheet.md").is_file()
    assert list((ctx.runs / "mutation" / "worksheets").glob("src-domain-sample-*.md"))

    again = context(mutation, repo)
    assert mutation.run_unit(again, UNIT, runners=refusing_runners(mutation)) == 0
    assert "skipping" in again.out.getvalue()


def test_an_untracked_file_is_recorded_as_an_unclean_run(mutation: ModuleType, repo: Path) -> None:
    (repo / "scratch.txt").write_text("untracked\n")
    ctx = context(mutation, repo)
    runners = mutation.Runners(baseline=green, tests=green, mechanical=fake_tool(mutation, []))
    assert mutation.run_unit(ctx, UNIT, runners=runners) == 0
    entry = mutation.load_map(mutation.map_path(ctx.runs))["units"][UNIT]
    run = json.loads((ctx.runs / entry["record"] / "run.json").read_text())
    assert (run["porcelain_before"], run["porcelain_after"], run["clean"]) == (1, 1, False)


def test_a_tool_that_writes_no_report_fails_and_leaves_the_map_unchanged(
    mutation: ModuleType, repo: Path
) -> None:
    def no_report(_unit: object, _root: Path, _staging: Path) -> Any:
        return mutation.ToolRun(0, b"", [], {}, ["tool"], 0.1, report_missing=True)

    ctx = context(mutation, repo)
    runners = mutation.Runners(baseline=green, tests=green, mechanical=no_report)
    assert mutation.run_unit(ctx, UNIT, runners=runners) == mutation.EXIT_FAILED
    assert "wrote no report" in ctx.out.getvalue()
    assert not mutation.map_path(ctx.runs).exists()
    (record,) = mutation.run_record.record_dirs(ctx.runs, "mutation")
    run = json.loads((record / "run.json").read_text())
    assert (run["report_missing"], run["outcome"]) == (True, "fail")


def test_a_dirty_tree_is_refused_before_the_baseline(mutation: ModuleType, repo: Path) -> None:
    (repo / UNIT).write_text("uncommitted\n")
    with pytest.raises(mutation.MutationError, match="DIRTY_TREE"):
        mutation.run_unit(context(mutation, repo), UNIT, runners=refusing_runners(mutation))


def test_mutated_unit_source_left_on_disk_is_restored_loudly(
    mutation: ModuleType, repo: Path
) -> None:
    ctx = context(mutation, repo)
    runners = mutation.Runners(
        baseline=green, tests=green, mechanical=fake_tool(mutation, [], UNIT)
    )
    assert mutation.run_unit(ctx, UNIT, runners=runners) == 0
    assert (repo / UNIT).read_text() == SOURCE
    assert "restored from HEAD" in ctx.out.getvalue()


def test_any_other_dirty_file_after_a_run_stops_without_touching_it(
    mutation: ModuleType, repo: Path
) -> None:
    ctx = context(mutation, repo)
    runners = mutation.Runners(
        baseline=green, tests=green, mechanical=fake_tool(mutation, [], TEST)
    )
    assert mutation.run_unit(ctx, UNIT, runners=runners) == mutation.EXIT_DIRTY
    assert (repo / TEST).read_text() == "mutated\n"
    assert "DIRTY_AFTER_RUN" in ctx.out.getvalue()
    assert not mutation.map_path(ctx.runs).exists()


def test_a_red_baseline_refuses_before_the_tool_runs(mutation: ModuleType, repo: Path) -> None:
    def red(*_args: object) -> tuple[int, bytes]:
        return 1, b"1 failed\n"

    def never(*_args: object) -> Any:
        pytest.fail("the tool ran on a red baseline")

    ctx = context(mutation, repo)
    runners = mutation.Runners(baseline=red, tests=red, mechanical=never)
    assert mutation.run_unit(ctx, UNIT, runners=runners) == mutation.EXIT_FAILED
    assert "BASELINE_RED" in ctx.out.getvalue()


def merge(  # noqa: PLR0913, PLR0917 - a test helper over one run's inputs
    mutation: ModuleType, prior: list[dict[str, Any]], mutants: list[dict[str, Any]], record: str
) -> Any:
    """Merge one mechanical run's mutants into `prior`."""
    groups = mutation.group_survivors(mutants, "mechanical")
    return mutation.merge_survivors(
        prior, groups, mutants, track="mechanical", record=record, at="2026-10-19T00:00:00Z"
    )


def test_verdicts_carry_forward_by_key_and_ids_are_never_renumbered(mutation: ModuleType) -> None:
    first = merge(
        mutation,
        [],
        [
            mutant("survived", "`a` becomes `b`"),
            {**mutant("survived", "`c` becomes `d`"), "line": 5},
        ],
        "mutation/one",
    )
    assert [(s["id"], s["verdict"]) for s in first] == [("S-01", "OPEN"), ("S-02", "OPEN")]
    first[1]["verdict"] = "EQUIVALENT"
    first[1]["reason"] = "dataclass slots argument"

    # The code moved down three lines: S-01 is now killed, S-02 still survives, one is new.
    moved = [
        {**mutant("killed", "`a` becomes `b`", "tests/unit/test_sample.py::test_one"), "line": 5},
        {**mutant("survived", "`c` becomes `d`"), "line": 8},
        {**mutant("survived", "`e` becomes `f`"), "line": 9},
    ]
    second = merge(mutation, first, moved, "mutation/two")
    by_id = {s["id"]: s for s in second}
    assert list(by_id) == ["S-01", "S-02", "S-03"]
    assert by_id["S-01"]["verdict"] == "KILLED"
    assert by_id["S-01"]["test"] == "tests/unit/test_sample.py::test_one"
    assert by_id["S-01"]["present"] is False
    assert by_id["S-02"]["verdict"] == "EQUIVALENT"
    assert by_id["S-02"]["location"] == f"{UNIT}:8"
    assert by_id["S-03"]["first_seen"] == "mutation/two"

    # S-01 survives again: back to OPEN under its old id, with the change in its history.
    third = merge(mutation, second, [mutant("survived", "`a` becomes `b`")], "mutation/three")
    reopened = next(s for s in third if s["id"] == "S-01")
    assert reopened["verdict"] == "OPEN"
    assert "test" not in reopened
    assert [h["verdict"] for h in reopened["history"]] == ["OPEN", "KILLED", "OPEN"]
    assert [s["id"] for s in third] == ["S-01", "S-02", "S-03"]


def test_an_open_survivor_no_longer_generated_stays_open(mutation: ModuleType) -> None:
    first = merge(mutation, [], [mutant("survived", "`a` becomes `b`")], "mutation/one")
    second = merge(mutation, first, [], "mutation/two")
    assert second[0]["verdict"] == "OPEN"
    assert second[0]["present"] is False


def survivors_entry(mutation: ModuleType) -> dict[str, Any]:
    """Return a map entry with one OPEN survivor."""
    return {
        "unit": UNIT,
        "survivors": merge(mutation, [], [mutant("survived", "`a` becomes `b`")], "r"),
    }


def test_killed_without_a_test_or_with_an_absent_test_file_is_refused(
    mutation: ModuleType, repo: Path
) -> None:
    entry = survivors_entry(mutation)
    with pytest.raises(mutation.MutationError, match="--test"):
        mutation.set_verdict(repo, entry, "S-01", "KILLED", test=None, reason=None, at="t")
    with pytest.raises(mutation.MutationError, match="does not exist"):
        mutation.set_verdict(
            repo, entry, "S-01", "KILLED", test="tests/unit/absent.py::t", reason=None, at="t"
        )
    with pytest.raises(mutation.MutationError, match="--reason"):
        mutation.set_verdict(repo, entry, "S-01", "EQUIVALENT", test=None, reason=None, at="t")
    assert entry["survivors"][0]["verdict"] == "OPEN"


def test_a_killed_verdict_names_its_test_and_is_appended_to_history(
    mutation: ModuleType, repo: Path
) -> None:
    entry = survivors_entry(mutation)
    node = f"{TEST}::test_one_is_positive"
    mutation.set_verdict(repo, entry, "S-01", "KILLED", test=node, reason=None, at="t")
    survivor = entry["survivors"][0]
    assert survivor["verdict"] == "KILLED"
    assert survivor["test"] == node
    assert survivor["history"][-1] == {
        "at": "t",
        "verdict": "KILLED",
        "note": "set by mutation-verdict",
        "test": node,
    }


def test_a_semantic_proposal_is_applied_tested_reverted_and_recorded(
    mutation: ModuleType, repo: Path
) -> None:
    ctx = context(mutation, repo)
    record_current(mutation, repo)
    seen: list[str] = []

    def tests(_unit: object, root: Path) -> tuple[int, bytes]:
        seen.append((root / UNIT).read_text())
        return 1, b"FAILED tests/unit/test_sample.py::test_one_is_positive - assert False\n"

    patch = (
        f"--- a/{UNIT}\n+++ b/{UNIT}\n@@ -1,2 +1,2 @@\n def is_positive(value: int) -> bool:\n"
        "-    return value > 0\n+    return value > 1\n"
    )
    proposals = repo.parent / "proposals.yaml"
    proposals.write_text(
        json.dumps(
            [
                {"id": "P-01", "category": "boundary", "description": "off by one", "patch": patch},
                {
                    "id": "P-02",
                    "category": "boundary",
                    "description": "does not apply",
                    "patch": patch.replace("value > 0", "value < 0"),
                },
            ]
        )
    )
    runners = mutation.Runners(baseline=green, tests=tests, mechanical=None)
    assert mutation.run_unit(ctx, UNIT, semantic=proposals, runners=runners) == 0
    assert seen == [SOURCE.replace("> 0", "> 1")]
    assert (repo / UNIT).read_text() == SOURCE
    entry = mutation.load_map(mutation.map_path(ctx.runs))["units"][UNIT]
    assert entry["semantic_runs"][0]["counts"]["killed"] == 1
    assert entry["semantic_runs"][0]["counts"]["errors"] == 1
    record = ctx.runs / entry["semantic_runs"][0]["record"]
    assert mutation.run_record.verify_record(record) == []
    results = json.loads((record / "results.json").read_text())
    assert results[0]["killed_by"] == "tests/unit/test_sample.py::test_one_is_positive"
    # The killed proposal records its test run's real exit; the unapplied one never ran.
    steps = {s["name"]: s for s in json.loads((record / "steps.json").read_text())}
    assert list(steps) == ["baseline", "P-01", "P-02"]
    assert steps["P-01"]["exit_code"] == 1
    assert "not_run" not in steps["P-01"]
    assert steps["P-02"]["exit_code"] is None
    assert steps["P-02"]["not_run"] is True
    # A killed mutant and an unapplied proposal are results, not a failed run.
    run = json.loads((record / "run.json").read_text())
    assert (run["exit_code"], run["outcome"]) == (0, "pass")

    again = context(mutation, repo)
    assert (
        mutation.run_unit(again, UNIT, semantic=proposals, runners=refusing_runners(mutation)) == 0
    )
    assert "already recorded" in again.out.getvalue()


def test_a_semantic_run_duration_spans_the_baseline_and_every_proposal(
    mutation: ModuleType, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A clock that moves only while a test process "runs": 10 s baseline, 100 s per proposal.
    clock = [0.0]
    monkeypatch.setattr(mutation, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def baseline(_unit: object, _root: Path) -> tuple[int, bytes]:
        clock[0] += 10
        return 0, b"1 passed\n"

    def tests(_unit: object, root: Path) -> tuple[int, bytes]:
        clock[0] += 100
        return (1, b"1 failed\n") if "> 1" in (root / UNIT).read_text() else (0, b"1 passed\n")

    ctx = context(mutation, repo)
    record_current(mutation, repo)
    patch = (
        f"--- a/{UNIT}\n+++ b/{UNIT}\n@@ -1,2 +1,2 @@\n def is_positive(value: int) -> bool:\n"
        "-    return value > 0\n+    return value > {bound}\n"
    )
    proposals = repo.parent / "proposals.yaml"
    proposals.write_text(
        json.dumps(
            [
                {
                    "id": name,
                    "category": "b",
                    "description": "d",
                    "patch": patch.format(bound=bound),
                }
                for name, bound in (("P-01", 1), ("P-02", -1))
            ]
        )
    )
    runners = mutation.Runners(baseline=baseline, tests=tests, mechanical=None)
    assert mutation.run_unit(ctx, UNIT, semantic=proposals, runners=runners) == 0
    entry = mutation.load_map(mutation.map_path(ctx.runs))["units"][UNIT]
    record = ctx.runs / entry["semantic_runs"][0]["record"]
    run = json.loads((record / "run.json").read_text())
    assert run["duration_s"] == 210
    steps = json.loads((record / "steps.json").read_text())
    assert [(s["name"], s["exit_code"], s["duration_s"]) for s in steps] == [
        ("baseline", 0, 10),
        ("P-01", 1, 100),
        ("P-02", 0, 100),
    ]


def test_the_semantic_pass_needs_a_current_mechanical_entry(
    mutation: ModuleType, repo: Path
) -> None:
    proposals = repo.parent / "proposals.yaml"
    proposals.write_text("- {id: P-01, category: c, description: d, patch: x}\n")
    with pytest.raises(mutation.MutationError, match="NOT_CURRENT"):
        mutation.run_unit(
            context(mutation, repo), UNIT, semantic=proposals, runners=refusing_runners(mutation)
        )


def test_the_cosmic_ray_dump_is_read_per_job(mutation: ModuleType, repo: Path) -> None:
    module = str(repo / UNIT)
    diff = (
        f"--- a{module}\n+++ b{module}\n@@ -1,2 +1,2 @@\n"
        "-    return value > 0\n+    return value >= 0\n"
    )
    lines = [
        [
            {
                "mutations": [
                    {
                        "module_path": module,
                        "operator_name": "core/Op",
                        "occurrence": 0,
                        "start_pos": [2, 17],
                    }
                ]
            },
            {
                "worker_outcome": "normal",
                "test_outcome": "killed",
                "output": "F\nFAILED tests/unit/test_sample.py::test_one_is_positive - x\n",
                "diff": diff,
            },
        ],
        [
            {
                "mutations": [
                    {
                        "module_path": module,
                        "operator_name": "core/Op",
                        "occurrence": 1,
                        "start_pos": [2, 17],
                    }
                ]
            },
            {
                "worker_outcome": "normal",
                "test_outcome": "killed",
                "output": "timeout",
                "diff": diff,
            },
        ],
        [
            {
                "mutations": [
                    {
                        "module_path": module,
                        "operator_name": "core/Op",
                        "occurrence": 2,
                        "start_pos": [2, 17],
                    }
                ]
            },
            {"worker_outcome": "normal", "test_outcome": "survived", "output": "", "diff": diff},
        ],
    ]
    found = mutation.parse_cosmic_ray_dump("\n".join(json.dumps(x) for x in lines), repo)
    assert [m["status"] for m in found] == ["killed", "timeout", "survived"]
    assert found[0]["file"] == UNIT
    assert found[0]["line"] == 2
    assert found[0]["killed_by"] == "tests/unit/test_sample.py::test_one_is_positive"
    assert found[2]["mutation"] == "`return value > 0` becomes `return value >= 0`"


def test_the_stryker_report_names_the_killing_test_and_the_original_text(
    mutation: ModuleType,
) -> None:
    report: dict[str, Any] = {
        "files": {
            "lib/x.ts": {
                "source": "export const ok = (a: number) => a > 0;\n",
                "mutants": [
                    {
                        "mutatorName": "EqualityOperator",
                        "replacement": "a >= 0",
                        "status": "Killed",
                        "killedBy": ["0"],
                        "location": {
                            "start": {"line": 1, "column": 34},
                            "end": {"line": 1, "column": 39},
                        },
                    },
                    {
                        "mutatorName": "EqualityOperator",
                        "replacement": "a < 0",
                        "status": "NoCoverage",
                        "location": {
                            "start": {"line": 1, "column": 34},
                            "end": {"line": 1, "column": 39},
                        },
                    },
                    {"mutatorName": "X", "replacement": "", "status": "Ignored", "location": {}},
                ],
            }
        },
        "testFiles": {"test/x.test.ts": {"tests": [{"id": "0", "name": "x is positive"}]}},
    }
    found = mutation.parse_stryker_report(report, "frontend")
    assert [m["status"] for m in found] == ["killed", "no-coverage"]
    assert found[0]["killed_by"] == "frontend/test/x.test.ts::x is positive"
    assert found[0]["mutation"] == "`a > 0` becomes `a >= 0`"
    assert found[0]["file"] == "frontend/lib/x.ts"
    assert mutation.counts_of(found)["no_coverage"] == 1
