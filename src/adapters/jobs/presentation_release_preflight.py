# SPDX-License-Identifier: MIT-0
"""Refuse frontend publication against incompatible deployed Presentation declarations."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import boto3
import yaml
from aws_lambda_powertools import Logger
from botocore.exceptions import BotoCoreError, ClientError
from requests import RequestException

from adapters.config import TracingSettings
from adapters.outbound.cloudformation_stack import stack_resource_summaries
from adapters.outbound.https import HttpsSession, require_https
from adapters.outbound.telemetry.bootstrap import configure_tracing, flush_traces
from adapters.outbound.telemetry.reporting import (
    LogSeverity,
    OperationLog,
    Report,
    flush_metrics,
    report_error,
    report_operation,
)
from adapters.outbound.telemetry.tracing import (
    PRESENTATION_RELEASE_PREFLIGHT,
    span,
    trace_boundary,
)
from domain.shared_kernel.errors import ErrorCode, ValidationError

ROOT = Path(__file__).resolve().parents[3]
MAX_ARCHIVE_BYTES = 50_000_000
MAX_ENVIRONMENT_LENGTH = 32
_MISSING = "<missing>"
_HTTP = HttpsSession()
LOGGER = Logger(service="adlytics-presentation-release-preflight", level="DEBUG")
_OPERATION = PRESENTATION_RELEASE_PREFLIGHT
_COMPONENT = "presentation_release_preflight.operator"
_HEARTBEAT_INTERVAL_SECONDS = 30.0
COMPATIBILITY_REFUSED = ErrorCode("PRESENTATION_PREFLIGHT", 1)


@dataclass(frozen=True, slots=True)
class _Progress:
    operation_id: str
    execution_id: str
    plan_digest: str | None
    stage: str
    completed: int
    total: int


class _Heartbeat:
    """Repeat only the last completed preflight progress snapshot."""

    def __init__(
        self,
        reporter: Callable[[_Progress], None],
        initial: _Progress,
        *,
        interval_seconds: float = _HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self._reporter = reporter
        self._progress = initial
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def update(self, progress: _Progress) -> None:
        if progress.completed < self._progress.completed:
            raise ValueError("preflight progress cannot move backwards.")
        self._progress = progress

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run,
            name="presentation-release-preflight-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            self._reporter(self._progress)


@dataclass(frozen=True, slots=True)
class _CandidatePlan:
    declarations: dict[str, dict[str, object]]
    digest: str


@dataclass(frozen=True, slots=True)
class DeclarationPath:
    """One declaration generation that must remain browser-compatible during migration."""

    generation: str
    candidate: Path
    deployed: str
    document_fields: tuple[str, ...]
    page_fields: tuple[str, ...]


_DECLARATIONS = (
    DeclarationPath(
        generation="v1",
        candidate=ROOT / "presentation/pages/v1/pages.yaml",
        deployed="presentation/pages/v1/pages.yaml",
        document_fields=("version", "context_fields"),
        page_fields=(
            "actions",
            "artifact",
            "boundary_required",
            "display_name",
            "grain",
            "id",
            "metrics",
            "mode",
            "preferred_window_duration_days",
            "route",
            "sections",
        ),
    ),
    DeclarationPath(
        generation="v2",
        candidate=ROOT / "presentation/pages/v2/pages.yaml",
        deployed="presentation/pages/v2/pages.yaml",
        document_fields=("version", "presentation_result_version", "temporal_control_version"),
        page_fields=(
            "actions",
            "artifacts",
            "boundary_required",
            "display_name",
            "id",
            "mode",
            "primary_job",
            "purpose",
            "route",
            "sections",
            "temporal_policy",
            "visuals",
        ),
    ),
)


class CompatibilityInspectionError(RuntimeError):
    """The deployed Presentation package could not be inspected safely."""


@dataclass(frozen=True, slots=True)
class CompatibilityFinding:
    """One candidate field that the deployed Presentation package does not satisfy."""

    path: str
    candidate: object
    deployed: object


def _nondefault_profile(value: str) -> str:
    if not value.strip() or value == "default":
        raise argparse.ArgumentTypeError("--profile must name an explicit nondefault AWS profile")
    return value


def _environment(value: str) -> str:
    if not value.strip() or len(value) > MAX_ENVIRONMENT_LENGTH:
        raise argparse.ArgumentTypeError("--env must be a nonblank deployment name")
    return value


def _account(value: str) -> str:
    normalized = value.strip()
    if re.fullmatch(r"\d{12}", normalized) is None:
        raise argparse.ArgumentTypeError("--expected-account must be a 12-digit AWS account id")
    return normalized


def _non_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("value must not be blank")
    return value.strip()


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_nondefault_profile)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--env", required=True, type=_environment)
    parser.add_argument("--operation-id", required=True, type=_non_blank)
    parser.add_argument("--stack", required=True, type=_non_blank)
    parser.add_argument("--trace", action="store_true")
    parsed = parser.parse_args(argv)
    expected_stack = f"Adlytics-Api-{parsed.env}"
    if parsed.stack != expected_stack:
        parser.error(f"--stack must be exactly {expected_stack}")
    return parsed


def _document(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CompatibilityInspectionError(f"{label} is not an object.")
    return cast("dict[str, object]", value)


def _page_map(document: dict[str, object], label: str) -> dict[str, dict[str, object]]:
    raw_pages = document.get("pages")
    if not isinstance(raw_pages, list):
        raise CompatibilityInspectionError(f"{label} has no page list.")
    pages: dict[str, dict[str, object]] = {}
    for raw_page in cast("list[object]", raw_pages):
        page = _document(raw_page, f"{label} page")
        page_id = page.get("id")
        if not isinstance(page_id, str) or not page_id.strip() or page_id in pages:
            raise CompatibilityInspectionError(f"{label} page identity is invalid.")
        pages[page_id] = page
    if not pages:
        raise CompatibilityInspectionError(f"{label} page list is empty.")
    return pages


def compatibility_findings(
    candidate: dict[str, object],
    deployed: dict[str, object],
    declaration: DeclarationPath,
) -> tuple[CompatibilityFinding, ...]:
    """Compare every declaration field the generation's browser binding can consume."""
    findings: list[CompatibilityFinding] = []
    prefix = declaration.generation
    for field in declaration.document_fields:
        expected = candidate.get(field, _MISSING)
        actual = deployed.get(field, _MISSING)
        if expected != actual:
            findings.append(CompatibilityFinding(f"{prefix}.{field}", expected, actual))

    candidate_pages = _page_map(candidate, f"candidate {declaration.generation} declaration")
    deployed_pages = _page_map(deployed, f"deployed {declaration.generation} declaration")
    for page_id, expected_page in candidate_pages.items():
        actual_page = deployed_pages.get(page_id)
        if actual_page is None:
            findings.append(CompatibilityFinding(f"{prefix}.pages.{page_id}", "present", _MISSING))
            continue
        for field in declaration.page_fields:
            expected = expected_page.get(field, _MISSING)
            actual = actual_page.get(field, _MISSING)
            if expected != actual:
                findings.append(
                    CompatibilityFinding(
                        f"{prefix}.pages.{page_id}.{field}",
                        expected,
                        actual,
                    )
                )
    return tuple(findings)


def all_compatibility_findings(
    candidate: dict[str, dict[str, object]],
    deployed: dict[str, dict[str, object]],
) -> tuple[CompatibilityFinding, ...]:
    """Compare every migration declaration required by the candidate frontend."""
    findings: list[CompatibilityFinding] = []
    for declaration in _DECLARATIONS:
        expected = candidate.get(declaration.generation)
        actual = deployed.get(declaration.generation)
        if expected is None:
            raise CompatibilityInspectionError(
                f"candidate {declaration.generation} declaration is missing."
            )
        if actual is None:
            findings.append(
                CompatibilityFinding(
                    declaration.generation,
                    "present",
                    _MISSING,
                )
            )
            continue
        findings.extend(compatibility_findings(expected, actual, declaration))
    return tuple(findings)


def _load_declaration(raw: bytes, label: str) -> dict[str, object]:
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise CompatibilityInspectionError(f"{label} is not valid YAML.") from exc
    return _document(parsed, label)


def _candidate_plan() -> _CandidatePlan:
    digest = hashlib.sha256()
    declarations: dict[str, dict[str, object]] = {}
    for declaration in _DECLARATIONS:
        raw = declaration.candidate.read_bytes()
        generation = declaration.generation.encode()
        digest.update(len(generation).to_bytes(8, "big"))
        digest.update(generation)
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        declarations[declaration.generation] = _load_declaration(
            raw,
            f"candidate {declaration.generation} declaration",
        )
    return _CandidatePlan(declarations, f"sha256:{digest.hexdigest()}")


def _archive_declarations(archive_bytes: bytes) -> dict[str, dict[str, object]]:
    try:
        with zipfile.ZipFile(BytesIO(archive_bytes)) as archive:
            return {
                declaration.generation: _load_declaration(
                    archive.read(declaration.deployed),
                    f"deployed {declaration.generation} declaration",
                )
                for declaration in _DECLARATIONS
            }
    except (KeyError, zipfile.BadZipFile) as exc:
        raise CompatibilityInspectionError(
            "Presentation Lambda does not contain both governed page declarations."
        ) from exc


def _presentation_function_name(session: boto3.Session, stack_name: str) -> str:
    client: Any = session.client("cloudformation")
    # ApiStack holds more than DescribeStackResources' 100, so the Presentation function can fall
    # outside one page; the helper reads every page.
    resources = stack_resource_summaries(client, stack_name)
    matches = [
        item.get("PhysicalResourceId")
        for item in resources
        if item.get("ResourceType") == "AWS::Lambda::Function"
        and str(item.get("LogicalResourceId", "")).startswith("PresentationFunction")
    ]
    names = [name for name in matches if isinstance(name, str) and name]
    if len(names) != 1:
        raise CompatibilityInspectionError(
            "ApiStack does not identify exactly one Presentation Lambda."
        )
    return names[0]


def _deployed_declarations(
    session: boto3.Session,
    stack_name: str,
) -> dict[str, dict[str, object]]:
    function_name = _presentation_function_name(session, stack_name)
    client: Any = session.client("lambda")
    response = client.get_function(FunctionName=function_name)
    code = response.get("Code")
    if not isinstance(code, dict):
        raise CompatibilityInspectionError("Presentation Lambda has no readable code location.")
    location = cast("dict[str, object]", code).get("Location")
    if not isinstance(location, str):
        raise CompatibilityInspectionError("Presentation Lambda has no readable code location.")
    with _HTTP.get(require_https(location), timeout=30) as download:
        download.raise_for_status()
        archive_bytes = download.content
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise CompatibilityInspectionError(
            "Presentation Lambda archive exceeds the inspection limit."
        )
    return _archive_declarations(archive_bytes)


def _safe_value(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _details(progress: _Progress, **extra: object) -> dict[str, object]:
    details: dict[str, object] = {
        "operation_id": progress.operation_id,
        "execution_id": progress.execution_id,
        "predecessor_execution_id": "none",
        "plan_digest": progress.plan_digest or "unbound",
        "stage": progress.stage,
        "completed": progress.completed,
        "total": progress.total,
        **extra,
    }
    return details


def _lifecycle(  # noqa: PLR0913
    progress: _Progress,
    *,
    phase: str,
    outcome: str,
    severity: LogSeverity,
    **extra: object,
) -> None:
    report_operation(
        OperationLog(
            operation=_OPERATION,
            component=_COMPONENT,
            phase=phase,
            outcome=outcome,
            severity=severity,
            details=_details(progress, **extra),
        ),
        LOGGER,
    )


@trace_boundary(PRESENTATION_RELEASE_PREFLIGHT)
def main(argv: Sequence[str] | None = None) -> int:  # noqa: PLR0915
    """Compare candidate declarations with one explicitly named deployed package."""
    args = _parse_args(argv)
    execution_id = uuid4().hex
    total = len(_DECLARATIONS)
    progress = _Progress(args.operation_id, execution_id, None, "start", 0, total)
    heartbeat: _Heartbeat | None = None
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    try:
        tracing_active = configure_tracing(
            TracingSettings(
                enabled=args.trace,
                aws_region=args.region,
                environment=args.env,
                sample_ratio=1.0,
            ),
            session=session._session,
        )
        with span(
            PRESENTATION_RELEASE_PREFLIGHT,
            operation_id=args.operation_id,
            execution_id=execution_id,
            predecessor_execution_id="none",
            plan_digest="unbound",
            stage="start",
            completed=0,
            total=total,
        ) as root_span:
            _lifecycle(
                progress,
                phase="start",
                outcome="started",
                severity=LogSeverity.DEBUG,
                tracing=tracing_active,
            )

            def report_heartbeat(snapshot: _Progress) -> None:
                _lifecycle(
                    snapshot,
                    phase="heartbeat",
                    outcome="unchanged",
                    severity=LogSeverity.DEBUG,
                )

            heartbeat = _Heartbeat(report_heartbeat, progress)
            heartbeat.start()
            identity = session.client("sts").get_caller_identity()
            if str(identity.get("Account", "")) != args.expected_account:
                raise PermissionError(
                    "caller account does not match the explicitly expected account"
                )

            plan = _candidate_plan()
            progress = _Progress(
                args.operation_id,
                execution_id,
                plan.digest,
                "plan",
                0,
                total,
            )
            heartbeat.update(progress)
            root_span.record(plan_digest=plan.digest, stage="plan")
            _lifecycle(
                progress,
                phase="progress",
                outcome="completed",
                severity=LogSeverity.INFO,
            )
            deployed = _deployed_declarations(session, args.stack)
            findings = all_compatibility_findings(plan.declarations, deployed)
            progress = _Progress(
                args.operation_id,
                execution_id,
                plan.digest,
                "attest",
                total,
                total,
            )
            heartbeat.update(progress)
            heartbeat.stop()
            _lifecycle(
                progress,
                phase="progress",
                outcome="completed",
                severity=LogSeverity.INFO,
            )
            if findings:
                refusal = ValidationError(
                    COMPATIBILITY_REFUSED,
                    "Candidate and deployed Presentation declarations are incompatible.",
                )
                report_error(
                    refusal,
                    Report(
                        operation=_OPERATION,
                        correlation_id=execution_id,
                        logger=LOGGER,
                    ),
                )
                root_span.record(
                    stage="attest",
                    completed=total,
                    accepted=False,
                    declaration_count=total,
                    finding_count=len(findings),
                    outcome="failed",
                )
                _lifecycle(
                    progress,
                    phase="terminal",
                    outcome="failed",
                    severity=LogSeverity.WARNING,
                    accepted=False,
                    declaration_count=total,
                    finding_count=len(findings),
                )
                sys.stderr.write(
                    "ERROR: candidate frontend requires a different Presentation contract. "
                    "Build Lambda artifacts and deploy ApiStack before publishing frontend.\n"
                )
                for finding in findings:
                    sys.stderr.write(
                        f"  - {finding.path}: candidate={_safe_value(finding.candidate)} "
                        f"deployed={_safe_value(finding.deployed)}\n"
                    )
                return 1

            page_count = sum(
                len(_page_map(plan.declarations[item.generation], item.generation))
                for item in _DECLARATIONS
            )
            root_span.record(
                stage="attest",
                completed=total,
                accepted=True,
                declaration_count=total,
                finding_count=0,
                outcome="completed",
            )
            _lifecycle(
                progress,
                phase="terminal",
                outcome="completed",
                severity=LogSeverity.INFO,
                accepted=True,
                declaration_count=total,
                finding_count=0,
            )
            sys.stdout.write(
                f"presentation compatibility accepted: declarations={total}, pages={page_count}\n"
            )
            return 0
    except KeyboardInterrupt:
        _lifecycle(
            progress,
            phase="terminal",
            outcome="cancelled",
            severity=LogSeverity.WARNING,
        )
        return 130
    except (
        BotoCoreError,
        ClientError,
        CompatibilityInspectionError,
        OSError,
        PermissionError,
        RequestException,
        ValueError,
    ) as error:
        report_error(
            error,
            Report(
                operation=_OPERATION,
                correlation_id=execution_id,
                logger=LOGGER,
            ),
        )
        _lifecycle(
            progress,
            phase="terminal",
            outcome="failed",
            severity=LogSeverity.WARNING,
        )
        sys.stderr.write(
            "ERROR: could not attest deployed Presentation compatibility "
            f"({type(error).__name__}).\n"
        )
        return 2
    finally:
        if heartbeat is not None:
            heartbeat.stop()
        try:
            flush_metrics()
        finally:
            flush_traces()


if __name__ == "__main__":
    raise SystemExit(main())
