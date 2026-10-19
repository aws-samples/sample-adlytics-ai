# SPDX-License-Identifier: MIT-0
"""Build and compare the Python dependency layer from two unequal absolute roots."""

from __future__ import annotations

import argparse
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from lambda_layer_packaging import (
    PackagingError,
    compare_trees,
    scan_for_absolute_paths,
    validate_layer,
    verify_imports,
)


def _run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PackagingError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout.strip()


def _require_committed_snapshot(checkout: Path) -> str:
    for arguments in (
        ["diff", "--quiet", "HEAD", "--"],
        ["diff", "--cached", "--quiet", "HEAD", "--"],
    ):
        result = subprocess.run(
            ["git", "-C", str(checkout), *arguments],
            check=False,
        )
        if result.returncode != 0:
            raise PackagingError(
                "the reproducibility gate requires a clean tracked tree so both roots come from HEAD"
            )
    return _run(["git", "-C", str(checkout), "rev-parse", "HEAD"], cwd=checkout)


def _export_head(checkout: Path, destination: Path) -> None:
    destination.mkdir()
    archive = subprocess.Popen(
        ["git", "-C", str(checkout), "archive", "HEAD"],
        stdout=subprocess.PIPE,
    )
    if archive.stdout is None:
        archive.kill()
        raise PackagingError("git archive did not expose its output stream")
    extractor = subprocess.run(
        ["tar", "-x", "-C", str(destination)],
        stdin=archive.stdout,
        check=False,
        capture_output=True,
        text=True,
    )
    archive.stdout.close()
    archive_status = archive.wait()
    if archive_status != 0 or extractor.returncode != 0:
        raise PackagingError(
            f"HEAD export failed: git={archive_status}, tar={extractor.returncode}: "
            f"{extractor.stderr.strip()}"
        )


def _attach_toolchains(checkout: Path, exported_root: Path) -> None:
    python_environment = checkout / ".venv"
    node_modules = checkout / "infra" / "node_modules"
    if not (python_environment / "bin" / "python").is_file():
        raise PackagingError("project .venv is required; run make install before this gate")
    if not node_modules.is_dir():
        raise PackagingError("infra/node_modules is required; run npm ci in infra before this gate")
    (exported_root / ".venv").symlink_to(python_environment.resolve(), target_is_directory=True)
    (exported_root / "infra" / "node_modules").symlink_to(
        node_modules.resolve(), target_is_directory=True
    )


def _forbidden_machine_roots(checkout: Path) -> tuple[Path, ...]:
    python_environment = (checkout / ".venv").resolve(strict=True)
    python_installation = (python_environment / "bin" / "python").resolve(strict=True).parents[1]
    node_modules = (checkout / "infra" / "node_modules").resolve(strict=True)
    uv = shutil.which("uv")
    if uv is None:
        raise PackagingError("uv executable is required for the layer build")
    candidates = (
        checkout.resolve(strict=True),
        python_environment,
        python_installation,
        node_modules,
        Path(uv).resolve(strict=True).parent,
        Path.home().resolve(strict=True),
    )
    return tuple(dict.fromkeys(candidates))


def _build(exported_root: Path, environment: dict[str, str]) -> Path:
    output = exported_root / "build"
    command = ["bash", str(exported_root / "scripts" / "build-lambdas.sh"), str(output)]
    print(f"BUILD_ROOT={exported_root}")
    result = subprocess.run(command, cwd=exported_root, env=environment, check=False)
    if result.returncode != 0:
        raise PackagingError(
            f"layer build failed with exit code {result.returncode}: {exported_root}"
        )
    return output / "layer"


def _cdk_asset_hash(exported_root: Path, layer: Path, environment: dict[str, str]) -> str:
    command = [
        str(exported_root / "infra" / "node_modules" / ".bin" / "ts-node"),
        str(exported_root / "infra" / "scripts" / "lambda-layer-asset-hash.ts"),
        str(layer),
    ]
    output = _run(command, cwd=exported_root / "infra", environment=environment)
    lines = output.splitlines()
    if len(lines) != 1 or len(lines[0]) != 64:
        raise PackagingError(f"CDK asset helper returned an unexpected hash: {output!r}")
    return lines[0]


def _stage_verified_build(exported_root: Path, checkout: Path) -> tuple[Path, str]:
    source = exported_root / "build"
    destination = checkout / "build"
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise PackagingError(f"checkout build path is not a regular directory: {destination}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, symlinks=True)
    return destination, compare_trees(source, destination)


def _negative_control(parent: Path) -> None:
    forbidden = parent / "negative-control-deliberately-injected-absolute-root"
    fixture = parent / "negative-control-fixture"
    python_root = fixture / "python"
    python_root.mkdir(parents=True)
    source = fixture / "fixture.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    py_compile.compile(
        str(source),
        cfile=str(python_root / "fixture.pyc"),
        dfile=str(forbidden / "fixture.py"),
        doraise=True,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    try:
        scan_for_absolute_paths(fixture, [forbidden])
    except PackagingError:
        print(f"NEGATIVE_CONTROL=DETECTED injected_root={forbidden}")
        return
    raise PackagingError("negative control passed unexpectedly; absolute-path detection is broken")


def run_gate(checkout: Path) -> None:
    """Run the complete paired-build gate and always remove its temporary roots."""
    checkout = checkout.resolve(strict=True)
    commit = _require_committed_snapshot(checkout)
    parent = Path(tempfile.mkdtemp(prefix="adlytics-layer-repro-gate-"))
    first_root = parent / "root-a"
    second_root = parent / "root-b-with-a-deliberately-and-visibly-different-absolute-path-length"
    environment = dict(os.environ)
    for name in tuple(environment):
        if name.startswith("ADLYTICS_") or name.startswith("AWS_"):
            environment.pop(name)
    environment.update(
        {
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "AWS_EC2_METADATA_DISABLED": "true",
        }
    )

    try:
        for root in (first_root, second_root):
            _export_head(checkout, root)
            _attach_toolchains(checkout, root)
        first_layer = _build(first_root, environment)
        second_layer = _build(second_root, environment)

        first_python = first_layer / "python"
        second_python = second_layer / "python"
        first_pycs, first_records, first_record_rows = validate_layer(first_python)
        second_pycs, second_records, second_record_rows = validate_layer(second_python)
        inventory_hash = compare_trees(first_layer, second_layer)
        forbidden_roots = (first_root, second_root, *_forbidden_machine_roots(checkout))
        first_scanned = scan_for_absolute_paths(first_layer, forbidden_roots)
        second_scanned = scan_for_absolute_paths(second_layer, forbidden_roots)
        imports = verify_imports(first_python)
        second_imports = verify_imports(second_python)
        if imports != second_imports:
            raise PackagingError("the two layers expose different top-level import packages")

        first_asset_hash = _cdk_asset_hash(first_root, first_layer, environment)
        second_asset_hash = _cdk_asset_hash(second_root, second_layer, environment)
        if first_asset_hash != second_asset_hash:
            raise PackagingError(
                f"CDK asset hashes differ: {first_asset_hash} != {second_asset_hash}"
            )
        _negative_control(parent)
        staged_build, staged_build_hash = _stage_verified_build(first_root, checkout)

        print(f"COMMIT={commit}")
        print(f"FIRST_ROOT={first_root}")
        print(f"SECOND_ROOT={second_root}")
        print(f"FIRST_INVENTORY_SHA256={inventory_hash}")
        print(f"SECOND_INVENTORY_SHA256={inventory_hash}")
        print(f"FIRST_CDK_ASSET_HASH={first_asset_hash}")
        print(f"SECOND_CDK_ASSET_HASH={second_asset_hash}")
        print(
            "ABSOLUTE_PATH_SCAN=CLEAN "
            f"first_files={first_scanned} second_files={second_scanned} "
            f"forbidden_roots={','.join(str(root) for root in forbidden_roots)}"
        )
        print(
            "BYTECODE_AND_RECORDS=VALID "
            f"first_pycs={first_pycs} second_pycs={second_pycs} "
            f"first_records={first_records} second_records={second_records} "
            f"first_record_rows={first_record_rows} second_record_rows={second_record_rows}"
        )
        print(f"CONSOLE_SCRIPTS=EXCLUDED imports={len(imports)}")
        print(
            "RUNTIME_IMPORTS=PASS environment=linux/aarch64/python3.14.6 "
            f"direct_layer={len(imports)} modules={','.join(imports)}"
        )
        print(f"STAGED_BUILD={staged_build} inventory_sha256={staged_build_hash}")
        print("LAYER_REPRODUCIBLE")
    finally:
        shutil.rmtree(parent, ignore_errors=False)
        print(f"CLEANUP=REMOVED {parent}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="clean checkout whose committed HEAD is built twice",
    )
    return parser


def main() -> int:
    """CLI entry point."""
    arguments = _parser().parse_args()
    try:
        run_gate(arguments.checkout)
    except (OSError, PackagingError, py_compile.PyCompileError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
