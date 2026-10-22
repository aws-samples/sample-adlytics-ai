# SPDX-License-Identifier: MIT-0
"""V2-only command-line parsing and refusal before any AWS boundary."""

from __future__ import annotations

import argparse
import signal
import threading
from types import ModuleType, SimpleNamespace
from typing import NoReturn, Protocol

import pytest

from adapters.jobs.ingest_delivery import __main__ as ingest_cli
from adapters.jobs.ingest_delivery.landing import ManifestInput
from adapters.jobs.land_fixtures import __main__ as land_cli
from adapters.jobs.reprocess_semantic_v2 import __main__ as reprocess_cli
from adapters.jobs.reprocess_semantic_v2.job import ReprocessCancelledError, ReprocessProgress
from adapters.outbound.telemetry.reporting import OperationLog
from domain.operations.execution_graph import AuthorityMode

pytestmark = pytest.mark.unit


class _CliModule(Protocol):
    sys: ModuleType
    boto3: ModuleType

    def main(self, argv: tuple[str, ...] | None = None) -> int: ...


def _unexpected_aws_access(*_args: object, **_kwargs: object) -> NoReturn:
    pytest.fail("the CLI crossed the AWS boundary before refusing unsafe arguments")


_INGEST_ARGUMENTS = (
    "--profile",
    "explicit-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
    "--operation-id",
    "ingest-unit",
    "--stack",
    "Adlytics-DataV2-dev",
    "--bucket",
    "explicit-versioned-raw",
    "--graph-store-stack",
    "Adlytics-ControlPlaneAuthorityStore-dev",
)
_LAND_ARGUMENTS = (
    "--profile",
    "explicit-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
    "--operation-id",
    "land-unit",
    "--stack",
    "Adlytics-DataV2-dev",
    "--bucket",
    "explicit-versioned-raw",
)


def test_ingest_parses_only_the_v2_inputs() -> None:
    arguments = ingest_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
        (
            *_INGEST_ARGUMENTS,
            "--key",
            "prefix/manifest.json",
            "--version-id",
            "manifest-version-001",
            "--dry-run",
        )
    )
    assert arguments.profile == "explicit-profile"
    assert arguments.region == "us-east-1"
    assert arguments.expected_account == "000000000000"
    assert arguments.operation_id == "ingest-unit"
    assert arguments.stack == "Adlytics-DataV2-dev"
    assert arguments.bucket == "explicit-versioned-raw"
    assert arguments.manifest == ManifestInput(
        "prefix/manifest.json",
        "manifest-version-001",
    )
    assert arguments.predecessor_execution_id is None
    assert arguments.dry_run is True


def test_ingest_retry_parses_one_explicit_predecessor() -> None:
    arguments = ingest_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
        (
            *_INGEST_ARGUMENTS,
            "--key",
            "prefix/manifest.json",
            "--version-id",
            "manifest-version-001",
            "--predecessor-execution-id",
            "execution-prior",
        )
    )
    assert arguments.predecessor_execution_id == "execution-prior"


@pytest.mark.parametrize(
    "arguments",
    (
        ("--predecessor-execution-id", "execution-prior"),
        (
            "--key",
            "prefix/manifest.json",
            "--version-id",
            "manifest-version-001",
            "--predecessor-execution-id",
            "execution-prior",
            "--dry-run",
        ),
    ),
)
def test_ingest_retry_requires_one_mutating_explicit_delivery(
    arguments: tuple[str, ...],
) -> None:
    with pytest.raises(SystemExit) as refusal:
        ingest_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
            (*_INGEST_ARGUMENTS, *arguments)
        )
    assert refusal.value.code == 2


@pytest.mark.parametrize(
    "orphan",
    (
        ("--key", "prefix/manifest.json"),
        ("--version-id", "manifest-version-001"),
    ),
)
def test_ingest_requires_key_and_version_together(orphan: tuple[str, str]) -> None:
    with pytest.raises(SystemExit) as refusal:
        ingest_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
            (*_INGEST_ARGUMENTS, *orphan)
        )
    assert refusal.value.code == 2


def test_fixture_landing_requires_an_explicit_profile_for_v2_inputs() -> None:
    arguments = land_cli._parse_args(  # pyright: ignore[reportPrivateUsage]
        (*_LAND_ARGUMENTS, "--dry-run")
    )
    assert arguments.profile == "explicit-profile"
    assert arguments.environment == "dev"
    assert arguments.region == "us-east-1"
    assert arguments.expected_account == "000000000000"
    assert arguments.dry_run is True


@pytest.mark.parametrize("cli", (ingest_cli, land_cli))
def test_operator_jobs_refuse_a_missing_profile(cli: _CliModule) -> None:
    with pytest.raises(SystemExit) as refusal:
        cli._parse_args(("--dry-run",))  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
    assert refusal.value.code == 2


def test_operator_jobs_refuse_the_default_profile() -> None:
    for parser in (ingest_cli._profile, land_cli._profile, reprocess_cli._profile):  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(argparse.ArgumentTypeError, match="nondefault AWS profile"):
            parser("default")


@pytest.mark.parametrize("cli", [ingest_cli, land_cli])
@pytest.mark.parametrize("retired", ["--v1", "--v2"])
def test_retired_plane_options_are_refused(cli: _CliModule, retired: str) -> None:
    with pytest.raises(SystemExit) as refusal:
        cli._parse_args((retired,))  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue]
    assert refusal.value.code == 2


@pytest.mark.parametrize(
    ("cli", "argv"),
    (
        (ingest_cli, ["job", *_INGEST_ARGUMENTS[:-10], "--env", "prod", *_INGEST_ARGUMENTS[-8:]]),
        (land_cli, ["job", *_LAND_ARGUMENTS[:-10], "--env", "prod", *_LAND_ARGUMENTS[-8:]]),
    ),
)
def test_non_dev_write_is_refused_before_aws_access(
    monkeypatch: pytest.MonkeyPatch,
    cli: _CliModule,
    argv: list[str],
) -> None:
    monkeypatch.setattr(cli.sys, "argv", argv)
    monkeypatch.setattr(cli.boto3, "client", _unexpected_aws_access)
    monkeypatch.setattr(cli.boto3, "Session", _unexpected_aws_access)

    with pytest.raises((SystemExit, land_cli.NotDevError)):
        cli.main()


_REPROCESS_ARGUMENTS = (
    "--profile",
    "explicit-profile",
    "--region",
    "us-east-1",
    "--expected-account",
    "000000000000",
    "--env",
    "dev",
    "--stack",
    "Adlytics-Foundation-dev",
    "--bucket",
    "explicit-versioned-raw",
    "--window-start",
    "2026-08-01",
    "--window-end",
    "2026-08-01",
    "--operation-id",
    "reprocess-unit",
    "--contract",
    "vendor-sales-by-asin",
)


@pytest.mark.parametrize(
    "unsafe",
    (
        ("--apply",),
        ("--expected-plan-digest", "sha256:reviewed"),
    ),
)
def test_reprocess_expected_digest_and_apply_are_required_together_before_aws(
    monkeypatch: pytest.MonkeyPatch,
    unsafe: tuple[str, ...],
) -> None:
    monkeypatch.setattr(reprocess_cli.boto3, "Session", _unexpected_aws_access)

    with pytest.raises(SystemExit) as refusal:
        reprocess_cli.main([*_REPROCESS_ARGUMENTS, *unsafe])

    assert refusal.value.code == 2


class _CallerIdentity:
    def __init__(self, account: str) -> None:
        self._account = account

    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": self._account, "Arn": "arn:aws:iam::000000000000:role/fictional"}


class _BoundarySession:
    def __init__(self) -> None:
        self._session = object()
        self.clients: list[str] = []

    def client(self, service: str) -> object:
        self.clients.append(service)
        if service == "sts":
            return _CallerIdentity("000000000000")
        return object()


class _OperatorSession:
    def __init__(self, account: str) -> None:
        self._account = account
        self.botocore_session = object()
        self._session = self.botocore_session
        self.clients: list[str] = []

    def client(self, service: str) -> _CallerIdentity:
        self.clients.append(service)
        if service != "sts":
            pytest.fail(f"account verification did not precede {service} client construction")
        return _CallerIdentity(self._account)


def test_reprocess_account_mismatch_refuses_before_non_sts_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _OperatorSession("111111111111")
    errors: list[BaseException] = []
    flushed: list[str] = []
    constructed: list[dict[str, object]] = []
    traced_with: list[object] = []

    def session_factory(**kwargs: object) -> _OperatorSession:
        constructed.append(kwargs)
        return session

    def configure(_settings: object, *, session: object) -> bool:
        traced_with.append(session)
        return False

    def record_error(error: BaseException, _report: object) -> None:
        errors.append(error)

    def ignore_operation(_event: object, _logger: object) -> None:
        return None

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(reprocess_cli.boto3, "Session", session_factory)
    monkeypatch.setattr(reprocess_cli, "configure_tracing", configure)
    monkeypatch.setattr(reprocess_cli, "report_error", record_error)
    monkeypatch.setattr(reprocess_cli, "report_operation", ignore_operation)
    monkeypatch.setattr(reprocess_cli, "flush_metrics", flush_metrics)
    monkeypatch.setattr(reprocess_cli, "flush_traces", flush_traces)

    result = reprocess_cli.main(list(_REPROCESS_ARGUMENTS))

    assert result == 1
    assert constructed == [{"profile_name": "explicit-profile", "region_name": "us-east-1"}]
    # Bootstrap precedes the STS client so the identity check itself is instrumented; the
    # exporter is bound to the operator's own session, so a refusal never crosses accounts.
    assert traced_with == [session.botocore_session]
    assert session.clients == ["sts"]
    assert len(errors) == 1
    assert "111111111111" not in str(errors[0])
    assert flushed == ["metrics", "traces"]


@pytest.mark.parametrize(
    ("cli", "arguments"),
    # A full-plan landing holds historical days and is refused before any AWS call (S1 hard
    # deny), so the landing case selects the non-historical trap class.
    ((ingest_cli, _INGEST_ARGUMENTS), (land_cli, (*_LAND_ARGUMENTS, "--classes", "trap"))),
)
def test_operator_account_mismatch_refuses_before_non_sts_clients(
    monkeypatch: pytest.MonkeyPatch,
    cli: _CliModule,
    arguments: tuple[str, ...],
) -> None:
    session = _OperatorSession("111111111111")
    flushed: list[str] = []
    traced_with: list[object] = []

    def session_factory(**_kwargs: object) -> _OperatorSession:
        return session

    def configure(_settings: object, *, session: object) -> bool:
        traced_with.append(session)
        return False

    def ignore_error(_error: BaseException, _report: object) -> None:
        return None

    def ignore_operation(_event: object, _logger: object) -> None:
        return None

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(cli.boto3, "Session", session_factory)
    monkeypatch.setattr(cli, "configure_tracing", configure)
    monkeypatch.setattr(cli, "report_error", ignore_error)
    monkeypatch.setattr(cli, "report_operation", ignore_operation)
    monkeypatch.setattr(cli, "flush_metrics", flush_metrics)
    monkeypatch.setattr(cli, "flush_traces", flush_traces)

    result = cli.main(arguments)

    assert result == 1
    assert traced_with == [session.botocore_session]
    assert session.clients == ["sts"]
    assert flushed == ["metrics", "traces"]


def test_ingest_cancellation_preserves_durable_progress_and_recovery_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _BoundarySession()
    flushed: list[str] = []
    terminal: list[dict[str, object]] = []

    def session_factory(**_kwargs: object) -> _BoundarySession:
        return session

    def fixed_execution_id() -> SimpleNamespace:
        return SimpleNamespace(hex="current-ingest-execution")

    def configure(_settings: object, *, session: object) -> bool:
        del session
        return False

    def ignore(*_args: object, **_kwargs: object) -> None:
        return None

    def semantic_resources(*_args: object) -> dict[str, str]:
        return {
            "bucket": "semantic",
            "bake_workgroup": "bake",
            "source_database": "source",
            "candidate_database": "candidate",
            "current_database": "current",
            "staging_database": "staging",
            "attempts": "attempts",
            "leases": "leases",
            "publications": "publications",
        }

    def selected_delivery(*_args: object) -> object:
        return object()

    def writes_dimension(*_args: object) -> bool:
        return False

    def empty_items(_root: object) -> tuple[()]:
        return ()

    def cancel_after_durable_delivery(
        batch: ingest_cli._V2Batch,  # pyright: ignore[reportPrivateUsage]
    ) -> None:
        batch.progress(
            ingest_cli._Progress(  # pyright: ignore[reportPrivateUsage]
                operation_id=batch.operation_id,
                execution_id=batch.execution_id,
                predecessor_execution_id=batch.predecessor_execution_id,
                plan_digest=batch.plan_digest,
                stage="apply",
                completed=1,
                total=1,
            )
        )
        raise ingest_cli.IngestCancelledError

    def capture_operation(event: OperationLog, _logger: object) -> None:
        if event.phase == "terminal":
            terminal.append(dict(event.details))

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(ingest_cli.boto3, "Session", session_factory)
    monkeypatch.setattr(ingest_cli, "uuid4", fixed_execution_id)
    monkeypatch.setattr(ingest_cli, "configure_tracing", configure)
    monkeypatch.setattr(ingest_cli, "verify_named_raw_bucket", ignore)
    monkeypatch.setattr(ingest_cli, "resolve_named_semantic_resources", semantic_resources)
    monkeypatch.setattr(ingest_cli, "load_registry", selected_delivery)
    monkeypatch.setattr(ingest_cli, "load_contracts", empty_items)
    monkeypatch.setattr(ingest_cli, "load_query_packs", empty_items)

    class _Governed:
        def __init__(self, *_args: object) -> None:
            pass

        def fetch(self, *_args: object) -> object:
            return selected_delivery()

    def legacy_gate(*_args: object) -> object:
        return ingest_cli.OperatorGate(
            mode=lambda: AuthorityMode.LEGACY,
            admit=None,
            principal="arn:aws:iam::000000000000:user/operator",
            operation_id="ingest-unit",
        )

    monkeypatch.setattr(ingest_cli, "GovernedMaterializationPort", _Governed)
    monkeypatch.setattr(ingest_cli, "_operator_gate", legacy_gate)
    monkeypatch.setattr(ingest_cli, "_writes_a_dimension", writes_dimension)
    monkeypatch.setattr(ingest_cli, "_ingest_v2", cancel_after_durable_delivery)
    monkeypatch.setattr(ingest_cli, "report_operation", capture_operation)
    monkeypatch.setattr(ingest_cli, "flush_metrics", flush_metrics)
    monkeypatch.setattr(ingest_cli, "flush_traces", flush_traces)

    result = ingest_cli.main(
        (
            *_INGEST_ARGUMENTS,
            "--key",
            "prefix/manifest.json",
            "--version-id",
            "manifest-version-001",
        )
    )

    assert result == 130
    assert terminal[-1] | {"plan_digest": "ignored"} == {
        "operation_id": "ingest-unit",
        "execution_id": "current-ingest-execution",
        "predecessor_execution_id": "none",
        "plan_digest": "ignored",
        "stage": "apply",
        "completed": 1,
        "total": 1,
    }
    assert capsys.readouterr().out.endswith(
        "Next predecessor execution ID: current-ingest-execution\n"
    )
    assert flushed == ["metrics", "traces"]


def test_fixture_cancellation_preserves_durable_progress_and_recovery_lineage(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _BoundarySession()
    flushed: list[str] = []
    terminal: list[dict[str, object]] = []
    deliveries = (land_cli.plan_landings(land_cli.ROOT)[0],)

    def session_factory(**_kwargs: object) -> _BoundarySession:
        return session

    def fixed_execution_id() -> SimpleNamespace:
        return SimpleNamespace(hex="current-land-execution")

    def configure(_settings: object, *, session: object) -> bool:
        del session
        return False

    def ignore(*_args: object, **_kwargs: object) -> None:
        return None

    def selected_landings(_root: object, _selection: object = None) -> object:
        return deliveries

    def cancel_after_durable_delivery(
        request: land_cli.ApplyLandFixturesRequest,
    ) -> int:
        request.progress(
            land_cli.LandFixturesProgress(
                operation_id=request.operation_id,
                execution_id=request.execution_id,
                predecessor_execution_id=request.predecessor_execution_id,
                plan_digest=request.plan_digest,
                stage="apply",
                completed=1,
                total=1,
            )
        )
        raise land_cli.LandFixturesCancelledError

    def capture_operation(event: OperationLog, _logger: object) -> None:
        if event.phase == "terminal":
            terminal.append(dict(event.details))

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(land_cli.boto3, "Session", session_factory)
    monkeypatch.setattr(land_cli, "uuid4", fixed_execution_id)
    monkeypatch.setattr(land_cli, "configure_tracing", configure)
    monkeypatch.setattr(land_cli, "verify_named_raw_bucket", ignore)
    monkeypatch.setattr(land_cli, "plan_landings", selected_landings)
    monkeypatch.setattr(land_cli, "apply_immutable", cancel_after_durable_delivery)
    monkeypatch.setattr(land_cli, "report_operation", capture_operation)
    monkeypatch.setattr(land_cli, "flush_metrics", flush_metrics)
    monkeypatch.setattr(land_cli, "flush_traces", flush_traces)

    result = land_cli.main(_LAND_ARGUMENTS)

    assert result == 130
    assert terminal[-1] | {"plan_digest": "ignored"} == {
        "operation_id": "land-unit",
        "execution_id": "current-land-execution",
        "predecessor_execution_id": "none",
        "plan_digest": "ignored",
        "stage": "apply",
        "completed": 1,
        "total": 1,
    }
    assert capsys.readouterr().out.endswith(
        "Next predecessor execution ID: current-land-execution\n"
    )
    assert flushed == ["metrics", "traces"]


def test_reprocess_cancellation_reports_current_execution_as_predecessor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session = _OperatorSession("000000000000")
    lifecycle: list[tuple[str, str]] = []
    flushed: list[str] = []
    traced_with: list[object] = []

    def session_factory(**_kwargs: object) -> _OperatorSession:
        return session

    def fixed_execution_id() -> SimpleNamespace:
        return SimpleNamespace(hex="current-execution-id")

    def ignore_checkpoint(_event: object) -> None:
        return None

    def capture_lifecycle(  # noqa: PLR0913
        _operation: str,
        *,
        phase: str,
        outcome: str,
        severity: object,
        details: dict[str, object],
    ) -> None:
        del severity, details
        lifecycle.append((phase, outcome))

    def flush_metrics() -> None:
        flushed.append("metrics")

    def flush_traces() -> None:
        flushed.append("traces")

    monkeypatch.setattr(reprocess_cli.boto3, "Session", session_factory)
    monkeypatch.setattr(reprocess_cli, "uuid4", fixed_execution_id)
    monkeypatch.setattr(reprocess_cli, "_checkpoint", ignore_checkpoint)

    def cancel_after_bootstrap(_settings: object, *, session: object) -> bool:
        traced_with.append(session)
        raise ReprocessCancelledError

    monkeypatch.setattr(reprocess_cli, "configure_tracing", cancel_after_bootstrap)
    monkeypatch.setattr(reprocess_cli, "_lifecycle", capture_lifecycle)
    monkeypatch.setattr(reprocess_cli, "flush_metrics", flush_metrics)
    monkeypatch.setattr(reprocess_cli, "flush_traces", flush_traces)

    result = reprocess_cli.main(list(_REPROCESS_ARGUMENTS))

    assert result == 130
    assert traced_with == [session.botocore_session]
    assert session.clients == []
    assert lifecycle[-1] == ("terminal", "cancelled")
    assert capsys.readouterr().out == "Next predecessor execution ID: current-execution-id\n"
    assert flushed == ["metrics", "traces"]


def test_reprocess_heartbeat_repeats_progress_without_advancing_it() -> None:
    progress = ReprocessProgress(
        operation_id="operation",
        execution_id="execution",
        predecessor_execution_id=None,
        plan_digest="sha256:reviewed",
        stage="preflight",
        completed=2,
        total=4,
    )
    reported: list[ReprocessProgress] = []
    heartbeat = reprocess_cli.ReprocessHeartbeat(reported.append, progress, interval_seconds=0.01)

    heartbeat.emit_once()
    heartbeat.emit_once()

    assert reported == [progress, progress]
    assert [item.completed for item in reported] == [2, 2]


def test_reprocess_signal_interrupts_a_blocked_stage_and_sets_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installed: dict[signal.Signals, object] = {}
    cancelled = threading.Event()

    def default_handler(_selected: object) -> signal.Handlers:
        return signal.SIG_DFL

    def install_handler(selected: signal.Signals, handler: object) -> None:
        installed[selected] = handler

    monkeypatch.setattr(reprocess_cli.signal, "getsignal", default_handler)
    monkeypatch.setattr(reprocess_cli.signal, "signal", install_handler)

    reprocess_cli._install_cancellation_signals(cancelled)  # pyright: ignore[reportPrivateUsage]
    handler = installed[signal.SIGTERM]
    assert callable(handler)
    handler(signal.SIGTERM, None)
    assert cancelled.is_set()
    with pytest.raises(ReprocessCancelledError):
        reprocess_cli._checkpoint(cancelled)  # pyright: ignore[reportPrivateUsage]
