# SPDX-License-Identifier: MIT-0
"""The maintainer-tool pins are read from the one CONTRIBUTING.md table and enforced exactly.

`scripts/tool_versions.py` parses the "Maintainer tools" table the publication gate checks the
installed gitleaks against and the release gate builds its ASH command from. A failure here means
a gate could run an unpinned scanner and call its pass proof, or silently accept a table that no
longer names the tool.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from gate_script_support import SCRIPTS

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def versions() -> ModuleType:
    """Load the script as a module so its functions are called directly."""
    spec = importlib.util.spec_from_file_location("tool_versions", SCRIPTS / "tool_versions.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _contributing(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "CONTRIBUTING.md"
    path.write_text(f"# Contributing\n\n### Maintainer tools\n\nIntro.\n\n{body}\n### Next\n")
    return path


# The table owns the versions; these tests check its shape and never restate a pin, so a bump
# edits only CONTRIBUTING.md.
@pytest.mark.parametrize("tool", ["gitleaks", "ASH (automated-security-helper)"])
def test_the_real_table_pins_each_scanner_a_gate_reads(versions: ModuleType, tool: str) -> None:
    assert versions.VERSION.fullmatch(versions.pinned(tool))


def test_the_real_table_lists_an_unpinned_container_runtime(versions: ModuleType) -> None:
    assert versions.table()["finch or Docker"] == versions.UNPINNED


def test_an_unpinned_row_is_never_returned_as_a_pin(versions: ModuleType, tmp_path: Path) -> None:
    path = _contributing(
        tmp_path, "| Tool | Version | Used by |\n|---|---|---|\n| runtime | any current | gate |\n"
    )

    assert versions.table(path) == {"runtime": "any current"}
    with pytest.raises(versions.ToolVersionError, match="not version-pinned"):
        versions.pinned("runtime", path)


def test_a_different_installed_version_is_reported(versions: ModuleType, tmp_path: Path) -> None:
    path = _contributing(
        tmp_path, "| Tool | Version | Used by |\n|---|---|---|\n| gitleaks | 1.2.3 | gate |\n"
    )

    assert versions.mismatch("gitleaks", "1.2.3", path) is None
    assert versions.mismatch("gitleaks", "1.3.0", path) == (
        "gitleaks 1.3.0 is installed; CONTRIBUTING.md Maintainer tools pins 1.2.3"
    )
    assert "is not installed" in versions.mismatch("gitleaks", None, path)


def test_an_absent_table_or_row_fails_loudly(versions: ModuleType, tmp_path: Path) -> None:
    no_section = tmp_path / "none.md"
    no_section.write_text("# Contributing\n")
    with pytest.raises(versions.ToolVersionError, match="no '### Maintainer tools' section"):
        versions.pinned("gitleaks", no_section)

    other_tool = _contributing(
        tmp_path, "| Tool | Version | Used by |\n|---|---|---|\n| other | 1.2.3 | gate |\n"
    )
    with pytest.raises(versions.ToolVersionError, match="no Maintainer tools row for gitleaks"):
        versions.pinned("gitleaks", other_tool)


def test_gitleaks_absent_from_path_reads_as_not_installed(
    versions: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_on_path(_name: str) -> None:
        return None

    monkeypatch.setattr(versions.shutil, "which", not_on_path)

    def no_process(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no process may start when gitleaks is absent")

    monkeypatch.setattr(versions.subprocess, "run", no_process)

    assert versions.installed_gitleaks() is None


def test_a_malformed_version_fails_loudly(versions: ModuleType, tmp_path: Path) -> None:
    path = _contributing(
        tmp_path, "| Tool | Version | Used by |\n|---|---|---|\n| gitleaks | latest | gate |\n"
    )

    with pytest.raises(versions.ToolVersionError, match="malformed"):
        versions.pinned("gitleaks", path)
