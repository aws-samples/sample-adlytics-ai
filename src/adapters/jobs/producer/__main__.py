# SPDX-License-Identifier: MIT-0
"""Describe AMC packs and validate or plan source deliveries without AWS access."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

from adapters.outbound.commerce_identity import load_commerce_identity_bridge
from adapters.producer.landing import write_governed_manifest
from adapters.producer.packs import pack_as_dict, pack_by_id
from adapters.producer.registrations import registration_from_json, write_registration
from adapters.producer.results import parse_csv_result
from domain.producer.models import (
    GovernedQueryPack,
    PeriodGrain,
    ScheduleKind,
    SourceRegistration,
)
from domain.producer.parameters import CommerceIdentityRequest, resolve_commerce_identities
from domain.producer.planning import (
    PlanLandingRequest,
    PlanManifestRequest,
    plan_landing,
    plan_manifest,
)
from domain.producer.validation import (
    ConformanceReport,
    validate_registration,
    validate_result,
    validate_result_window,
)
from domain.semantics.identity import SourceIdentifier


def _repo_root() -> Path:
    """Find the repository containing governed contracts from this installed module."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "contracts").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    raise ValueError("could not locate repository root from producer command.")


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not an ISO date.") from error


def _moment(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"{value!r} is not an ISO datetime.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("datetime must include an explicit UTC offset.")
    return parsed


def _nonblank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank.")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    describe = commands.add_parser("pack-describe", help="render one installed governed pack")
    describe.add_argument("pack_id", type=_nonblank)

    result = commands.add_parser("validate-result", help="validate one local AMC CSV")
    result.add_argument("pack_id", type=_nonblank)
    result.add_argument("result", type=Path)
    result.add_argument("--registration", type=Path)

    plan = commands.add_parser("plan-landing", help="render keys and registration after upload")
    plan.add_argument("pack_id", type=_nonblank)
    plan.add_argument("--execution-id", required=True, type=_nonblank)
    plan.add_argument("--workflow-id", required=True, type=_nonblank)
    plan.add_argument("--schedule-kind", required=True, choices=tuple(ScheduleKind))
    plan.add_argument("--schedule-id", type=_nonblank)
    plan.add_argument("--source-scope-namespace", required=True, type=_nonblank)
    plan.add_argument("--source-scope-value", required=True, type=_nonblank)
    plan.add_argument("--marketplace", required=True, type=_nonblank)
    plan.add_argument("--period-start", required=True, type=_day)
    plan.add_argument("--period-end", required=True, type=_day)
    plan.add_argument("--time-window-start", required=True, type=_moment)
    plan.add_argument("--time-window-end", required=True, type=_moment)
    plan.add_argument("--source-watermark", required=True, type=_day)
    plan.add_argument("--retrieved-at", required=True, type=_moment)
    plan.add_argument("--object-version-id", required=True, type=_nonblank)
    plan.add_argument(
        "--period-grain",
        choices=tuple(PeriodGrain),
        help="the declared period grain; required for a periodic pack, refused for a daily one",
    )
    plan.add_argument(
        "--variant",
        type=_nonblank,
        help="a declared pack variant to run instead of the base pack (plan D9)",
    )

    registration = commands.add_parser(
        "validate-registration", help="validate one prepared registration"
    )
    registration.add_argument("registration", type=Path)

    manifest = commands.add_parser(
        "render-manifest", help="render trusted direct-manifest mode from local bytes"
    )
    manifest.add_argument("registration", type=Path)
    manifest.add_argument("result", type=Path)
    manifest.add_argument("--destination-version-id", required=True, type=_nonblank)
    return parser


def _show_report(report: ConformanceReport) -> int:
    if report.valid:
        print("  conformance: OK")
        return 0
    print(f"  conformance: {len(report.findings)} finding(s)", file=sys.stderr)
    for finding in report.findings:
        print(f"    [{finding.code}] {finding.detail}", file=sys.stderr)
    return 1


def _describe(root: Path, pack_id: str) -> int:
    pack = pack_by_id(root, pack_id)
    print(json.dumps(pack_as_dict(pack), indent=2, sort_keys=True))
    return 0


def _validate_result(root: Path, args: argparse.Namespace) -> int:
    pack = pack_by_id(root, str(args.pack_id))
    summary, _evidence = parse_csv_result(Path(args.result).read_bytes())
    report = validate_result(pack, summary)
    if args.registration is not None:
        registration = registration_from_json(Path(args.registration).read_text())
        bridge = load_commerce_identity_bridge(root)
        report = report.combined(validate_registration(pack, registration, bridge))
        report = report.combined(validate_result_window(registration, summary))
    print(f"RESULT CONFORMANCE — {pack.pack_id}")
    print(f"  rows: {summary.row_count}")
    print(f"  columns: {len(summary.columns)}")
    return _show_report(report)


def _plan(root: Path, args: argparse.Namespace) -> int:
    pack = pack_by_id(root, str(args.pack_id))
    if args.variant is not None:
        pack = pack.variant_binding(str(args.variant))
    bridge = load_commerce_identity_bridge(root)
    # The adapter resolves the governed values; the domain planner only copies them (ADR-110).
    identities = resolve_commerce_identities(
        bridge,
        CommerceIdentityRequest(
            pack=pack,
            marketplace=str(args.marketplace),
            period=(args.period_start, args.period_end),
        ),
    )
    registration = plan_landing(
        PlanLandingRequest(
            pack=pack,
            execution_id=str(args.execution_id),
            workflow_id=str(args.workflow_id),
            schedule_kind=ScheduleKind(str(args.schedule_kind)),
            schedule_id=None if args.schedule_id is None else str(args.schedule_id),
            source_scope=SourceIdentifier(
                str(args.source_scope_namespace),
                str(args.source_scope_value),
            ),
            marketplace=str(args.marketplace),
            period_start=args.period_start,
            period_end=args.period_end,
            time_window_start=args.time_window_start,
            time_window_end=args.time_window_end,
            source_watermark=args.source_watermark,
            retrieved_at=args.retrieved_at,
            object_version_id=str(args.object_version_id),
            period_grain=None if args.period_grain is None else PeriodGrain(args.period_grain),
            commerce_identities=identities,
        )
    )
    report = validate_registration(pack, registration, bridge)
    if not report.valid:
        return _show_report(report)
    print(write_registration(registration), end="")
    return 0


def _registered_binding(root: Path, registration: SourceRegistration) -> GovernedQueryPack:
    """Resolve a registration's pack version to its current pack or declared variant."""
    pack = pack_by_id(root, registration.query_pack.pack_id)
    return pack.binding_for(registration.query_pack.version) or pack


def _validate_registration(root: Path, path: Path) -> int:
    registration = registration_from_json(path.read_text())
    pack = _registered_binding(root, registration)
    print(f"REGISTRATION CONFORMANCE — {pack.pack_id}")
    bridge = load_commerce_identity_bridge(root)
    return _show_report(validate_registration(pack, registration, bridge))


def _render_manifest(root: Path, args: argparse.Namespace) -> int:
    registration = registration_from_json(Path(args.registration).read_text())
    pack = _registered_binding(root, registration)
    summary, evidence = parse_csv_result(Path(args.result).read_bytes())
    report = validate_registration(pack, registration, load_commerce_identity_bridge(root))
    report = report.combined(validate_result(pack, summary))
    report = report.combined(validate_result_window(registration, summary))
    if not report.valid:
        return _show_report(report)
    planned = plan_manifest(
        PlanManifestRequest(
            registration=registration,
            pack=pack,
            evidence=evidence,
            destination_version_id=str(args.destination_version_id),
        )
    )
    print(write_governed_manifest(planned), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Dispatch one local producer command and render safe validation failures."""
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = _repo_root()
    try:
        if args.command == "pack-describe":
            return _describe(root, str(args.pack_id))
        if args.command == "validate-result":
            return _validate_result(root, args)
        if args.command == "plan-landing":
            return _plan(root, args)
        if args.command == "validate-registration":
            return _validate_registration(root, Path(args.registration))
        if args.command == "render-manifest":
            return _render_manifest(root, args)
        raise ValueError(f"unknown producer command {args.command!r}.")
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"producer command refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
