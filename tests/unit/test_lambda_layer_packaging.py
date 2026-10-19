# SPDX-License-Identifier: MIT-0
"""Focused tests for deterministic Python dependency-layer packaging."""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import py_compile
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.lambda_layer_packaging import (  # noqa: E402
    PackagingError,
    compare_trees,
    prune_console_scripts,
    scan_for_absolute_paths,
    shipped_top_level_modules,
    validate_pyc_files,
    validate_records,
)

sys.path.remove(str(ROOT))


def _digest(content: bytes) -> str:
    value = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
    return f"sha256={value.decode()}"


def _layer_with_script(tmp_path: Path, *, newline: str = "\n") -> tuple[Path, Path, Path]:
    root = tmp_path / "python"
    script = root / "bin" / "tool"
    script.parent.mkdir(parents=True)
    script.write_bytes(b"#!/absolute/checkout/.venv/bin/python\nprint('ok')\n")
    record = root / "example-1.0.dist-info" / "RECORD"
    record.parent.mkdir()
    rows = [
        ["example/__init__.py", "sha256=retained", "8"],
        ["../bin/tool", _digest(script.read_bytes()), str(script.stat().st_size)],
        ["example-1.0.dist-info/RECORD", "", ""],
    ]
    with record.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator=newline)
        writer.writerows(rows)
    return root, script, record


@pytest.mark.unit
def test_prune_console_scripts_removes_exact_owner_and_preserves_retained_bytes(
    tmp_path: Path,
) -> None:
    root, _, record = _layer_with_script(tmp_path, newline="\r\n")
    before = record.read_bytes().splitlines(keepends=True)

    assert prune_console_scripts(root) == ("tool",)

    assert not (root / "bin").exists()
    assert record.read_bytes() == before[0] + before[2]
    assert b"\r\n" in record.read_bytes()


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [(1, "sha256=stale"), (2, "1")])
def test_prune_console_scripts_refuses_stale_record(tmp_path: Path, field: int, value: str) -> None:
    root, _, record = _layer_with_script(tmp_path)
    rows = list(csv.reader(record.read_text().splitlines()))
    rows[1][field] = value
    with record.open("w", newline="") as stream:
        csv.writer(stream, lineterminator="\n").writerows(rows)

    with pytest.raises(PackagingError, match="stale RECORD"):
        prune_console_scripts(root)


@pytest.mark.unit
def test_prune_console_scripts_refuses_missing_or_duplicate_owner(tmp_path: Path) -> None:
    root, _, record = _layer_with_script(tmp_path)
    retained, owner, self_row = record.read_bytes().splitlines(keepends=True)
    record.write_bytes(retained + self_row)
    with pytest.raises(PackagingError, match="0 owning"):
        prune_console_scripts(root)

    record.write_bytes(retained + owner + owner + self_row)
    with pytest.raises(PackagingError, match="2 owning"):
        prune_console_scripts(root)


@pytest.mark.unit
def test_prune_console_scripts_refuses_unexpected_bin_entry(tmp_path: Path) -> None:
    root, _, _ = _layer_with_script(tmp_path)
    (root / "bin" / "nested").mkdir()
    with pytest.raises(PackagingError, match="unexpected console-script entry"):
        prune_console_scripts(root)


@pytest.mark.unit
def test_compare_trees_detects_path_mode_and_content_changes(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "same").write_text("bytes")
    (second / "same").write_text("bytes")
    assert len(compare_trees(first, second)) == 64

    (second / "same").chmod(0o755)
    with pytest.raises(PackagingError, match="inventory mismatch"):
        compare_trees(first, second)
    (second / "same").chmod(0o644)
    (second / "same").write_text("other")
    with pytest.raises(PackagingError, match="inventory mismatch"):
        compare_trees(first, second)
    (second / "same").unlink()
    (second / "renamed").write_text("bytes")
    with pytest.raises(PackagingError, match="inventory mismatch"):
        compare_trees(first, second)


@pytest.mark.unit
def test_pyc_validation_rejects_noncanonical_filename(tmp_path: Path) -> None:
    source = tmp_path / "module.py"
    source.write_text("VALUE = 1\n")
    pyc = tmp_path / "python" / "__pycache__" / "module.pyc"
    pyc.parent.mkdir(parents=True)
    py_compile.compile(
        str(source),
        cfile=str(pyc),
        dfile="/absolute/checkout/module.py",
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    with pytest.raises(PackagingError, match="noncanonical co_filename"):
        validate_pyc_files(tmp_path / "python")


@pytest.mark.unit
def test_absolute_path_scan_negative_control_catches_injected_pyc(tmp_path: Path) -> None:
    forbidden = tmp_path / "deliberately-long-build-root"
    source = tmp_path / "fixture.py"
    source.write_text("VALUE = 1\n")
    pyc = tmp_path / "asset" / "python" / "fixture.pyc"
    pyc.parent.mkdir(parents=True)
    py_compile.compile(
        str(source),
        cfile=str(pyc),
        dfile=str(forbidden / "fixture.py"),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    assert pyc.read_bytes().startswith(importlib.util.MAGIC_NUMBER)
    with pytest.raises(PackagingError, match="absolute build root found"):
        scan_for_absolute_paths(tmp_path / "asset", [forbidden])


@pytest.mark.unit
def test_record_validation_rejects_stale_retained_file(tmp_path: Path) -> None:
    root = tmp_path / "python"
    package = root / "example" / "__init__.py"
    package.parent.mkdir(parents=True)
    package.write_text("VALUE=1\n")
    record = root / "example-1.0.dist-info" / "RECORD"
    record.parent.mkdir()
    record.write_text(
        f"example/__init__.py,{_digest(package.read_bytes())},{package.stat().st_size}\n"
        "example-1.0.dist-info/RECORD,,\n"
    )
    assert validate_records(root) == (1, 2)
    package.write_text("VALUE=2\n")
    with pytest.raises(PackagingError, match="stale"):
        validate_records(root)


@pytest.mark.unit
def test_record_validation_rejects_every_out_of_layer_row(tmp_path: Path) -> None:
    root = tmp_path / "python"
    record = root / "example-1.0.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    record.write_text(
        "../unexpected-data.txt,sha256=not-validated,17\nexample-1.0.dist-info/RECORD,,\n"
    )

    with pytest.raises(PackagingError, match="escapes the installed layer"):
        validate_records(root)


@pytest.mark.unit
def test_import_discovery_includes_standalone_linux_extension(tmp_path: Path) -> None:
    root = tmp_path / "python"
    extension = root / "_example.cpython-314-aarch64-linux-gnu.so"
    extension.parent.mkdir(parents=True)
    extension.write_bytes(b"fixture")
    metadata = root / "example-1.0.dist-info" / "METADATA"
    metadata.parent.mkdir()
    metadata.write_text("Name: example\nVersion: 1.0\n\n")
    record = metadata.with_name("RECORD")
    record.write_text(
        f"{extension.name},{_digest(extension.read_bytes())},{extension.stat().st_size}\n"
        "example-1.0.dist-info/METADATA,sha256=fixture,27\n"
        "example-1.0.dist-info/RECORD,,\n"
    )

    assert shipped_top_level_modules(root) == ("_example",)
