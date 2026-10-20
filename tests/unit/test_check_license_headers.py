# SPDX-License-Identifier: MIT-0
"""The license-header gate skips the operator's local tool directories named in git's exclude file.

`scripts/check-license-headers.py` names no local tool in tracked content; it reads every bare
`.<name>/` pattern from `<git-common-dir>/info/exclude` instead. A failure here means a local
tool's state directory fails the gate on the operator's machine, or an ordinary source file is
skipped and published without its license header.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from gate_script_support import SCRIPTS, new_repo

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def headers() -> ModuleType:
    """Load the hyphenated script as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_license_headers", SCRIPTS / "check-license-headers.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_only_bare_dot_directory_lines_are_read(headers: ModuleType, tmp_path: Path) -> None:
    exclude = tmp_path / "exclude"
    exclude.write_text("# comment\n.toolstate/\n.toolfile\n*.log\n/anchored/\nplain/\n.a.b/\n")

    assert headers.excluded_names_from(exclude) == frozenset({".toolstate", ".a.b"})
    assert headers.excluded_names_from(tmp_path / "absent") == frozenset()


def test_an_excluded_tool_directory_is_skipped_and_source_is_not(
    headers: ModuleType, tmp_path: Path
) -> None:
    repo = new_repo(tmp_path)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(".toolstate/\n")
    (repo / "scripts" / ".toolstate").mkdir(parents=True)
    (repo / "scripts" / ".toolstate" / "report.py").write_text("print('no header')\n")
    (repo / "scripts" / "tool.py").write_text("print('no header')\n")

    candidates = [p.relative_to(repo).as_posix() for p in headers.candidate_paths(repo)]

    assert candidates == ["scripts/tool.py"]


def test_without_a_repository_no_extra_names_are_excluded(
    headers: ModuleType, tmp_path: Path
) -> None:
    assert headers.local_excluded_directory_names(tmp_path) == frozenset()


def test_without_git_on_path_no_extra_names_are_excluded(
    headers: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = new_repo(tmp_path)
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "exclude").write_text(".toolstate/\n")

    def not_on_path(_name: str) -> None:
        return None

    monkeypatch.setattr(headers.shutil, "which", not_on_path)

    def no_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no process may start when git is absent")

    monkeypatch.setattr(headers.subprocess, "run", no_process)

    assert headers.local_excluded_directory_names(repo) == frozenset()
