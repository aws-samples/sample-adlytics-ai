# SPDX-License-Identifier: MIT-0
"""Fail-closed validation and normalization for the Python dependency layer."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import importlib.util
import json
import marshal
import os
import stat
import struct
import subprocess
import sys
import types
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path


class PackagingError(RuntimeError):
    """Raised when installed layer content violates its packaging contract."""


@dataclass(frozen=True)
class TreeEntry:
    """One stable, byte-bearing layer inventory entry."""

    path: str
    type: str
    mode: str
    size: int | None = None
    sha256: str | None = None
    target: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _record_digest(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).digest()
    return "sha256=" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _record_lines(path: Path) -> list[tuple[bytes, list[str]]]:
    content = path.read_bytes()
    lines = content.splitlines(keepends=True)
    if b"".join(lines) != content:
        raise PackagingError(f"cannot preserve RECORD line boundaries: {path}")
    parsed: list[tuple[bytes, list[str]]] = []
    for line in lines:
        try:
            text = line.decode("utf-8")
            rows = list(csv.reader([text]))
        except (UnicodeDecodeError, csv.Error) as error:
            raise PackagingError(f"invalid RECORD row in {path}: {error}") from error
        if len(rows) != 1 or len(rows[0]) != 3:
            raise PackagingError(f"invalid RECORD row in {path}: {text.rstrip()!r}")
        parsed.append((line, rows[0]))
    return parsed


def _resolved_record_path(layer_python: Path, value: str) -> Path:
    return Path(os.path.normpath(layer_python / value))


def prune_console_scripts(layer_python: Path) -> tuple[str, ...]:
    """Remove generated launchers after validating their owning RECORD rows."""
    root = layer_python.resolve(strict=True)
    bin_dir = root / "bin"
    if not bin_dir.exists():
        return ()
    if not bin_dir.is_dir() or bin_dir.is_symlink():
        raise PackagingError(f"expected a regular console-script directory: {bin_dir}")

    scripts: list[Path] = []
    for entry in sorted(bin_dir.iterdir(), key=lambda item: item.name):
        if entry.is_symlink() or not entry.is_file():
            raise PackagingError(f"unexpected console-script entry: {entry}")
        scripts.append(entry)
    if not scripts:
        bin_dir.rmdir()
        return ()

    records = sorted(root.glob("*.dist-info/RECORD"))
    if not records:
        raise PackagingError("console scripts exist but no wheel RECORD files were installed")

    parsed_records = {record: _record_lines(record) for record in records}
    removals: dict[Path, set[int]] = {record: set() for record in records}
    for script in scripts:
        owners: list[tuple[Path, int, list[str]]] = []
        for record, lines in parsed_records.items():
            for index, (_, row) in enumerate(lines):
                resolved = _resolved_record_path(root, row[0])
                if resolved == script:
                    owners.append((record, index, row))
                elif Path(row[0]).name == script.name and "bin" in Path(row[0]).parts:
                    # Some target installers retain the wheel scheme's ../../../bin path even
                    # after placing the launcher under the target root. Match that representation
                    # only when its normalized destination is outside this isolated target.
                    try:
                        resolved.relative_to(root)
                    except ValueError:
                        owners.append((record, index, row))
        if len(owners) != 1:
            raise PackagingError(
                f"console script {script.name!r} has {len(owners)} owning RECORD rows; expected 1"
            )
        record, index, row = owners[0]
        expected_hash = _record_digest(script)
        expected_size = str(script.stat().st_size)
        if row[1] != expected_hash or row[2] != expected_size:
            raise PackagingError(
                f"stale RECORD row for {script.name}: got hash={row[1]!r} size={row[2]!r}, "
                f"expected hash={expected_hash!r} size={expected_size!r}"
            )
        removals[record].add(index)

    for record, indexes in removals.items():
        if not indexes:
            continue
        retained = [
            raw for index, (raw, _) in enumerate(parsed_records[record]) if index not in indexes
        ]
        record.write_bytes(b"".join(retained))

    names = tuple(script.name for script in scripts)
    for script in scripts:
        script.unlink()
    bin_dir.rmdir()

    for record in records:
        for _, row in _record_lines(record):
            if "bin" in Path(row[0]).parts:
                raise PackagingError(
                    f"RECORD still references removed console-script tree: {record}"
                )
    return names


def tree_inventory(root: Path) -> tuple[TreeEntry, ...]:
    """Return a complete, sorted inventory of paths, modes, sizes, and bytes."""
    absolute_root = root.resolve(strict=True)
    entries: list[TreeEntry] = []
    for path in sorted(
        absolute_root.rglob("*"), key=lambda item: item.relative_to(absolute_root).as_posix()
    ):
        relative = path.relative_to(absolute_root).as_posix()
        metadata = path.lstat()
        mode = f"{stat.S_IMODE(metadata.st_mode):04o}"
        if stat.S_ISREG(metadata.st_mode):
            entries.append(TreeEntry(relative, "file", mode, metadata.st_size, _sha256(path)))
        elif stat.S_ISDIR(metadata.st_mode):
            entries.append(TreeEntry(relative, "directory", mode))
        elif stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(path)
            entries.append(
                TreeEntry(
                    relative,
                    "symlink",
                    mode,
                    len(os.fsencode(target)),
                    hashlib.sha256(os.fsencode(target)).hexdigest(),
                    target,
                )
            )
        else:
            raise PackagingError(f"unsupported filesystem entry in layer: {path}")
    return tuple(entries)


def inventory_digest(entries: Sequence[TreeEntry]) -> str:
    """Hash the complete normalized inventory for concise evidence."""
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            json.dumps(asdict(entry), sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def compare_trees(first_root: Path, second_root: Path) -> str:
    """Require matching inventories and direct byte equality for every file."""
    first = tree_inventory(first_root)
    second = tree_inventory(second_root)
    if first != second:
        limit = max(len(first), len(second))
        for index in range(limit):
            left = first[index] if index < len(first) else None
            right = second[index] if index < len(second) else None
            if left != right:
                raise PackagingError(f"layer inventory mismatch: {left!r} != {right!r}")
        raise PackagingError("layer inventories differ")

    for entry in first:
        if entry.type != "file":
            continue
        left_path = first_root / entry.path
        right_path = second_root / entry.path
        with left_path.open("rb") as left, right_path.open("rb") as right:
            while True:
                left_chunk = left.read(1024 * 1024)
                right_chunk = right.read(1024 * 1024)
                if left_chunk != right_chunk:
                    raise PackagingError(f"layer file bytes differ: {entry.path}")
                if not left_chunk:
                    break
    return inventory_digest(first)


def _code_objects(code: types.CodeType) -> Iterable[types.CodeType]:
    yield code
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _code_objects(constant)


def validate_pyc_files(layer_python: Path) -> int:
    """Require unchecked-hash bytecode with deployed, canonical code filenames."""
    count = 0
    for path in sorted(layer_python.rglob("*.pyc")):
        content = path.read_bytes()
        if len(content) < 16:
            raise PackagingError(f"truncated pyc header: {path}")
        if content[:4] != importlib.util.MAGIC_NUMBER:
            raise PackagingError(f"pyc magic does not match the project interpreter: {path}")
        flags = struct.unpack("<I", content[4:8])[0]
        if flags != 1:
            raise PackagingError(f"pyc is not unchecked-hash mode (flags={flags}): {path}")
        try:
            root_code = marshal.loads(content[16:])
        except (EOFError, ValueError, TypeError) as error:
            raise PackagingError(f"cannot decode pyc {path}: {error}") from error
        if not isinstance(root_code, types.CodeType):
            raise PackagingError(f"pyc does not contain a code object: {path}")
        for code in _code_objects(root_code):
            filename = code.co_filename
            if filename != "/opt/python" and not filename.startswith("/opt/python/"):
                raise PackagingError(f"noncanonical co_filename {filename!r} in {path}")
        count += 1
    if count == 0:
        raise PackagingError("layer contains no precompiled bytecode")
    return count


def validate_records(layer_python: Path) -> tuple[int, int]:
    """Validate every retained RECORD row against a byte inside the layer."""
    root = layer_python.resolve(strict=True)
    records = sorted(root.glob("*.dist-info/RECORD"))
    row_count = 0
    for record in records:
        for _, row in _record_lines(record):
            row_count += 1
            candidate = _resolved_record_path(root, row[0])
            try:
                candidate.relative_to(root)
            except ValueError as error:
                raise PackagingError(
                    f"RECORD path escapes the installed layer: {record}: {row[0]!r}"
                ) from error
            if candidate == record:
                if row[1] != "" or row[2] != "":
                    raise PackagingError(f"RECORD self-row must have empty hash and size: {record}")
                continue
            if not candidate.is_file() or candidate.is_symlink():
                raise PackagingError(f"RECORD path is missing or not a regular file: {candidate}")
            if row[1] != _record_digest(candidate) or row[2] != str(candidate.stat().st_size):
                raise PackagingError(f"RECORD hash or size is stale: {candidate}")
    if not records:
        raise PackagingError("layer contains no wheel RECORD files")
    return len(records), row_count


def scan_for_absolute_paths(asset_root: Path, forbidden_roots: Sequence[Path]) -> int:
    """Scan every regular asset file as bytes for forbidden absolute roots."""
    lexical_and_resolved = (
        value for root in forbidden_roots for value in (str(root.absolute()), str(root.resolve()))
    )
    needles = tuple(value.encode("utf-8") for value in dict.fromkeys(lexical_and_resolved))
    if not needles:
        raise PackagingError("absolute-path scan requires at least one forbidden root")
    scanned = 0
    for path in sorted(asset_root.rglob("*")):
        if path.is_symlink():
            target = os.fsencode(os.readlink(path))
            if any(needle in target for needle in needles):
                raise PackagingError(f"absolute build root found in symlink target: {path}")
            continue
        if not path.is_file():
            continue
        content = path.read_bytes()
        for needle in needles:
            if needle in content:
                raise PackagingError(f"absolute build root found in asset file: {path}")
        scanned += 1
    if scanned == 0:
        raise PackagingError("absolute-path scan inspected no asset files")
    return scanned


def _top_level_module(relative: Path) -> str | None:
    if not relative.parts:
        return None
    first = relative.parts[0]
    if first.endswith(".dist-info") or first == "bin":
        return None
    if len(relative.parts) > 1:
        return first if first.isidentifier() else None
    if first.endswith(".py"):
        module = first.removesuffix(".py")
    elif first.endswith(".so"):
        # Linux extension modules use names such as module.cpython-314-aarch64-linux-gnu.so.
        module = first.split(".", 1)[0]
    else:
        return None
    return module if module.isidentifier() else None


def _shipped_top_level_owners(
    layer_python: Path,
) -> dict[str, tuple[tuple[str, str], ...]]:
    root = layer_python.resolve(strict=True)
    owners: dict[str, set[tuple[str, str]]] = {}
    for record in sorted(root.glob("*.dist-info/RECORD")):
        metadata = record.with_name("METADATA")
        if not metadata.is_file():
            raise PackagingError(f"installed distribution has no METADATA: {record.parent}")
        fields: dict[str, str] = {}
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if not line:
                break
            key, separator, value = line.partition(":")
            if separator and key in {"Name", "Version"}:
                fields[key] = value.strip()
        distribution = fields.get("Name")
        version = fields.get("Version")
        if distribution is None or version is None:
            raise PackagingError(f"installed distribution has incomplete METADATA: {metadata}")

        for _, row in _record_lines(record):
            candidate = _resolved_record_path(root, row[0])
            try:
                relative = candidate.relative_to(root)
            except ValueError as error:
                raise PackagingError(
                    f"RECORD path escapes the installed layer: {record}: {row[0]!r}"
                ) from error
            module = _top_level_module(relative)
            if module is not None:
                owners.setdefault(module, set()).add((distribution, version))
    if not owners:
        raise PackagingError("no shipped top-level import packages were discovered")
    return {module: tuple(sorted(values)) for module, values in sorted(owners.items())}


def shipped_top_level_modules(layer_python: Path) -> tuple[str, ...]:
    """Discover source, package, and native-extension import roots."""
    return tuple(_shipped_top_level_owners(layer_python))


TARGET_IMPORT_IMAGE = (
    "public.ecr.aws/docker/library/python@"
    "sha256:7bec7ddcddeff7975d6ba9b4be7dd6f6b2f55e7491539145e2978f7f97ce9144"
)


def verify_imports(layer_python: Path, image: str = TARGET_IMPORT_IMAGE) -> tuple[str, ...]:
    """Import every shipped root from the layer in Linux AArch64 Python 3.14."""
    modules = tuple(_shipped_top_level_owners(layer_python))
    program = """
import importlib
import json
import platform
import sys

assert sys.version_info[:3] == (3, 14, 6), sys.version
assert sys.platform == "linux", sys.platform
assert platform.machine() == "aarch64", platform.machine()
sys.path.insert(0, "/opt/python")
modules = json.loads(sys.argv[1])
for name in modules:
    module = importlib.import_module(name)
    locations = []
    if getattr(module, "__file__", None):
        locations.append(module.__file__)
    if getattr(module, "__path__", None):
        locations.extend(str(value) for value in module.__path__)
    if not locations or any(
        value != "/opt/python" and not value.startswith("/opt/python/")
        for value in locations
    ):
        raise RuntimeError(f"{name} loaded outside the shipped layer: {locations}")
print(json.dumps({"imported": len(modules)}))
"""
    container_name = (
        "adlytics-layer-import-"
        + hashlib.sha256(str(layer_python.resolve(strict=True)).encode("utf-8")).hexdigest()[:12]
    )
    command = [
        "perl",
        "-e",
        "alarm 300; exec @ARGV",
        "docker",
        "run",
        "--name",
        container_name,
        "--pull=never",
        "--network=none",
        "--platform",
        "linux/arm64",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit=64",
        "--mount",
        f"type=bind,src={layer_python.resolve(strict=True)},dst=/opt/python,readonly",
        "--entrypoint",
        "python",
        image,
        "-I",
        "-c",
        program,
        json.dumps(modules),
    ]
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    cleanup = subprocess.run(
        ["docker", "rm", "-f", container_name],
        check=False,
        capture_output=True,
        text=True,
    )
    if cleanup.returncode != 0 and "No such container" not in cleanup.stderr:
        raise PackagingError(
            f"could not remove target import container {container_name}: {cleanup.stderr.strip()}"
        )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "no diagnostic"
        raise PackagingError(f"target layer import verification failed: {detail}")
    return modules


def validate_layer(layer_python: Path) -> tuple[int, int, int]:
    """Run host-independent structural checks over one completed layer."""
    if (layer_python / "bin").exists():
        raise PackagingError(f"generated console-script tree survived: {layer_python / 'bin'}")
    pycs = validate_pyc_files(layer_python)
    records, record_rows = validate_records(layer_python)
    return pycs, records, record_rows


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prune = subparsers.add_parser("prune-console-scripts")
    prune.add_argument("layer_python", type=Path)
    validate = subparsers.add_parser("validate-layer")
    validate.add_argument("layer_python", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the requested packaging operation."""
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "prune-console-scripts":
            removed = prune_console_scripts(arguments.layer_python)
            print(f"removed {len(removed)} generated console scripts: {', '.join(removed)}")
        else:
            pycs, records, record_rows = validate_layer(arguments.layer_python)
            print(
                f"validated {pycs} pyc files, {records} RECORD files, "
                f"and {record_rows} retained RECORD rows"
            )
    except PackagingError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
