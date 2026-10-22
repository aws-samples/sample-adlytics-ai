# SPDX-License-Identifier: MIT-0
"""ADR-075 Stage-1 configuration ownership and generated tracing façade."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError

import pytest

from adapters import config
from adapters.outbound.telemetry import generated_spans, tracing
from domain.shared_kernel.errors import InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

_VALID_INGESTION_ENV = {
    "ADL_PROJECTION_HANDOFF_STATE": "QUEUED_QUIESCED",
    "ADL_STAGE1_MAX_UNITS": "15",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT": "10",
    "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN": "32",
    "ADL_STAGE1_MAX_GROUP_MEMBERS": "4",
    "ADL_STAGE1_MAX_POINTERS_PER_TABLE": "37",
    "ADL_STAGE1_MAX_OBJECT_REFS_PER_UNIT": "37",
    "ADL_STAGE1_MAX_ROWS_PER_UNIT": "50000",
    "ADL_STAGE1_MAX_ROWS_PER_DELIVERY": "20000",
    "ADL_STAGE1_ATHENA_MAX_STATEMENT_BYTES": "200000",
    "ADL_STAGE1_MAX_ENCODED_IDENTITY_BYTES": "128",
    "ADL_STAGE1_MAX_PARTITION_IDENTITY_BYTES": "256",
    "ADL_TABLE_WRITER_TIMEOUT_SECONDS": "900",
    "ADL_TABLE_WRITER_CLEANUP_RESERVE_MS": "60000",
    "ADL_TABLE_WRITER_CONCURRENCY": "8",
    "ADL_TABLE_WRITER_MAX_RECEIVE_COUNT": "3",
    "ADL_TABLE_LEASE_DURATION_SECONDS": "930",
    "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE": "10",
    "ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE": "50",
    "ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS": "60",
    "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
    "ADL_PROJECTION_HANDOFF_DRAIN_OBSERVATION_SECONDS": "300",
    "ADL_SEMANTIC_WRITE_QUEUE_URL": "https://sqs.test.invalid/semantic-write.fifo",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "fictional-publications",
    "ADL_SEMANTIC_LEASES_TABLE": "fictional-leases",
}
_VALID_LIFECYCLE_ENV = {
    "ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE": "10",
    "ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS": "3",
    "ADL_OPERATIONAL_STATUS_QUEUE_URL": "https://sqs.test.invalid/status.fifo",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "fictional-publications",
    "ADL_OPERATIONAL_STATUS_TABLE": "fictional-operational-status",
}
_CACHED_ACCESSORS = (
    config._ingestion_config,  # pyright: ignore[reportPrivateUsage] - cache isolation
    config._queued_ingestion_config,  # pyright: ignore[reportPrivateUsage] - cache isolation
    config.stage1_delivery_settings,
    config.table_writer_settings,
    config.table_attempt_reconciler_settings,
    config.plan_intent_reconciler_settings,
    config.provider_fence_recovery_settings,
    config.projection_handoff_settings,
    config.stage1_lifecycle_producer_settings,
    config.stage1_outbox_recovery_settings,
    config.operational_projector_settings,
)


@pytest.fixture(autouse=True)
def _clear_stage1_config_caches() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    for accessor in _CACHED_ACCESSORS:
        accessor.cache_clear()
    yield
    for accessor in _CACHED_ACCESSORS:
        accessor.cache_clear()


def _set_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_six_accessors_project_one_frozen_strict_ingestion_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, _VALID_INGESTION_ENV)

    delivery = config.stage1_delivery_settings()
    writer = config.table_writer_settings()
    attempt_reconciler = config.table_attempt_reconciler_settings()
    intent_reconciler = config.plan_intent_reconciler_settings()
    handoff = config.projection_handoff_settings()

    assert delivery.max_units == 15
    assert delivery.athena_max_statement_bytes == 200_000
    assert (delivery.max_rows_per_unit, delivery.max_rows_per_delivery) == (50_000, 20_000)
    assert writer.max_rows_per_unit == 50_000
    assert not hasattr(writer, "max_rows_per_delivery")
    assert not hasattr(delivery, "semantic_write_queue_url")
    assert writer.table_writer_timeout_seconds == 900
    assert writer.table_writer_cleanup_reserve_ms == 60_000
    assert writer.semantic_write_visibility_margin_seconds == 60
    assert attempt_reconciler == config.TableAttemptReconcilerSettings(
        10, 60, 900, "fictional-publications", "fictional-leases"
    )
    assert intent_reconciler == config.PlanIntentReconcilerSettings(
        50,
        60,
        900,
        15,
        "https://sqs.test.invalid/semantic-write.fifo",
        "fictional-publications",
    )
    assert config.provider_fence_recovery_settings() == config.ProviderFenceRecoverySettings(
        "fictional-publications"
    )
    assert handoff == config.ProjectionHandoffSettings(
        config.ProjectionHandoffState.QUEUED_QUIESCED,
        300,
    )
    with pytest.raises(FrozenInstanceError):
        delivery.max_units = 16  # type: ignore[misc]


def test_stage1_values_do_not_enter_broad_settings() -> None:
    governed_fields = {
        "projection_handoff_state",
        "max_units",
        "max_accepted_attempts_per_unit",
        "max_accepted_attempts_per_plan",
        "max_group_members",
        "max_pointers_per_table",
        "max_object_refs_per_unit",
        "max_rows_per_unit",
        "max_rows_per_delivery",
        "athena_max_statement_bytes",
        "max_encoded_identity_bytes",
        "table_writer_timeout_seconds",
        "table_writer_cleanup_reserve_ms",
        "table_writer_concurrency",
        "semantic_write_max_receive_count",
        "table_lease_duration_seconds",
        "semantic_write_visibility_margin_seconds",
        "table_attempt_reconcile_batch_size",
        "table_attempt_reconcile_base_backoff_seconds",
        "table_attempt_reconcile_max_backoff_seconds",
        "plan_intent_reconcile_batch_size",
        "plan_intent_reconcile_base_backoff_seconds",
        "plan_intent_reconcile_max_backoff_seconds",
        "drain_observation_interval_seconds",
        "semantic_write_queue_url",
    }

    assert governed_fields.isdisjoint(config.Settings.model_fields)


@pytest.mark.parametrize(
    ("name", "rejected"),
    [
        ("ADL_STAGE1_MAX_UNITS", "true"),
        ("ADL_STAGE1_MAX_UNITS", "1.5"),
        ("ADL_STAGE1_MAX_UNITS", "0"),
        ("ADL_STAGE1_MAX_UNITS", "-1"),
        ("ADL_STAGE1_MAX_UNITS", "01"),
    ],
)
def test_noninteger_boolean_nonpositive_and_noncanonical_values_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    rejected: str,
) -> None:
    values = {**_VALID_INGESTION_ENV, name: rejected}
    _set_environment(monkeypatch, values)

    with pytest.raises(InvariantError) as caught:
        config.stage1_delivery_settings()

    rendered = f"{caught.value} {caught.value.context} {caught.value.description}"
    assert name in rendered
    assert repr(rejected) not in rendered


def test_delivery_accessor_resolves_without_a_semantic_write_queue_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The delivery Lambda never sends a queue message, so its environment names no queue.

    A queue requirement in this accessor would fail every delivery container at import.
    """
    values = {
        name: value
        for name, value in _VALID_INGESTION_ENV.items()
        if name != "ADL_SEMANTIC_WRITE_QUEUE_URL"
    }
    _set_environment(monkeypatch, values)

    assert config.stage1_delivery_settings().table_lease_duration_seconds == 930
    with pytest.raises(InvariantError):
        config.table_writer_settings()


def test_missing_values_are_reported_together_without_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        name: value
        for name, value in _VALID_INGESTION_ENV.items()
        if name
        not in {
            "ADL_STAGE1_MAX_UNITS",
            "ADL_TABLE_WRITER_CONCURRENCY",
            "ADL_SEMANTIC_WRITE_QUEUE_URL",
            "ADL_STAGE1_MAX_ROWS_PER_UNIT",
            "ADL_STAGE1_MAX_ROWS_PER_DELIVERY",
        }
    }
    _set_environment(monkeypatch, values)

    with pytest.raises(InvariantError) as caught:
        config.table_writer_settings()

    rendered = str(caught.value.context["problems"])
    assert "ADL_STAGE1_MAX_UNITS" in rendered
    assert "ADL_TABLE_WRITER_CONCURRENCY" in rendered
    assert "ADL_SEMANTIC_WRITE_QUEUE_URL" in rendered
    assert "ADL_STAGE1_MAX_ROWS_PER_UNIT" in rendered
    assert "ADL_STAGE1_MAX_ROWS_PER_DELIVERY" in rendered


def test_unknown_projection_handoff_state_is_rejected_without_echo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = "UNKNOWN-HANDOFF-SENTINEL"
    _set_environment(
        monkeypatch,
        {**_VALID_INGESTION_ENV, "ADL_PROJECTION_HANDOFF_STATE": rejected},
    )

    with pytest.raises(InvariantError) as caught:
        config.projection_handoff_settings()

    rendered = f"{caught.value} {caught.value.context}"
    assert "ADL_PROJECTION_HANDOFF_STATE" in rendered
    assert rejected not in rendered


@pytest.mark.parametrize(
    ("overrides", "problem_name"),
    [
        ({"ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE": "9"}, "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE"),
        ({"ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE": "11"}, "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE"),
        # The pre-2026-10-10 reviewed value is now refused like any other (§11 amendment).
        ({"ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE": "10"}, "ADL_PLAN_INTENT_RECONCILE_BATCH_SIZE"),
        (
            {"ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE": "50"},
            "ADL_TABLE_ATTEMPT_RECONCILE_BATCH_SIZE",
        ),
        ({"ADL_TABLE_WRITER_CLEANUP_RESERVE_MS": "59000"}, "ADL_TABLE_WRITER_CLEANUP_RESERVE_MS"),
        ({"ADL_TABLE_LEASE_DURATION_SECONDS": "900"}, "ADL_TABLE_LEASE_DURATION_SECONDS"),
        (
            {"ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT": "9"},
            "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_UNIT",
        ),
        (
            {"ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN": "31"},
            "ADL_STAGE1_MAX_ACCEPTED_ATTEMPTS_PER_PLAN",
        ),
        (
            {"ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS": "59"},
            "ADL_TABLE_WRITER_VISIBILITY_MARGIN_SECONDS",
        ),
        # ADR-149: the reviewed row ceilings; any other value refuses, either side.
        ({"ADL_STAGE1_MAX_ROWS_PER_UNIT": "50001"}, "ADL_STAGE1_MAX_ROWS_PER_UNIT"),
        ({"ADL_STAGE1_MAX_ROWS_PER_UNIT": "49999"}, "ADL_STAGE1_MAX_ROWS_PER_UNIT"),
        ({"ADL_STAGE1_MAX_ROWS_PER_DELIVERY": "20001"}, "ADL_STAGE1_MAX_ROWS_PER_DELIVERY"),
        ({"ADL_STAGE1_MAX_ROWS_PER_DELIVERY": "19999"}, "ADL_STAGE1_MAX_ROWS_PER_DELIVERY"),
        (
            {
                "ADL_TABLE_ATTEMPT_RECONCILE_BASE_BACKOFF_SECONDS": "901",
                "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
            },
            "ADL_TABLE_ATTEMPT_RECONCILE_MAX_BACKOFF_SECONDS",
        ),
        (
            {
                "ADL_PLAN_INTENT_RECONCILE_BASE_BACKOFF_SECONDS": "901",
                "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS": "900",
            },
            "ADL_PLAN_INTENT_RECONCILE_MAX_BACKOFF_SECONDS",
        ),
    ],
)
def test_cross_field_and_exact_value_invariants_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    problem_name: str,
) -> None:
    _set_environment(monkeypatch, {**_VALID_INGESTION_ENV, **overrides})

    with pytest.raises(InvariantError) as caught:
        config.stage1_delivery_settings()

    assert problem_name in str(caught.value.context["problems"])


def test_stage1_lifecycle_settings_are_narrow_frozen_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, _VALID_LIFECYCLE_ENV)

    settings = config.stage1_lifecycle_producer_settings()

    assert settings == config.Stage1LifecycleProducerSettings(
        reconcile_batch_size=10,
        max_consecutive_failed_delivery_attempts=3,
        queue_url="https://sqs.test.invalid/status.fifo",
        publications_table="fictional-publications",
        status_table="fictional-operational-status",
    )
    with pytest.raises(FrozenInstanceError):
        settings.reconcile_batch_size = 11  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "rejected"),
    [
        ("ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE", "true"),
        ("ADL_STAGE1_LIFECYCLE_RECONCILE_BATCH_SIZE", "9"),
        ("ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS", "0"),
        ("ADL_STAGE1_OUTBOX_MAX_CONSECUTIVE_FAILED_DELIVERY_ATTEMPTS", "4"),
        ("ADL_OPERATIONAL_STATUS_QUEUE_URL", ""),
    ],
)
def test_stage1_lifecycle_settings_reject_invalid_or_missing_values_without_echo(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    rejected: str,
) -> None:
    values = {**_VALID_LIFECYCLE_ENV, name: rejected}
    _set_environment(monkeypatch, {key: value for key, value in values.items() if value})

    with pytest.raises(InvariantError) as caught:
        config.stage1_lifecycle_producer_settings()

    rendered = f"{caught.value} {caught.value.context}"
    assert name in rendered
    assert not rejected or repr(rejected) not in rendered


def test_generated_stage1_span_constants_are_reexported_without_stale_alias() -> None:
    expected = {
        "TABLE_WRITER_PROCESS": "ingest.table_writer",
        "TABLE_WRITER_UNIT": "ingest.table_writer.unit",
        "TABLE_WRITER_PLAN_TERMINAL": "ingest.table_writer.plan_terminal",
        "TABLE_INLINE_RUN": "ingest.table_inline_run",
    }

    for constant, span_name in expected.items():
        assert getattr(tracing, constant) is getattr(generated_spans, constant)
        assert getattr(tracing, constant) == span_name
        assert constant in tracing.__all__
    assert "TABLE_WRITER_TERMINAL" not in tracing.__all__
    assert not hasattr(tracing, "TABLE_WRITER_TERMINAL")


def test_projector_settings_resolve_status_and_publications_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ADL_OPERATIONAL_STATUS_TABLE", "fictional-operational-status")
    monkeypatch.setenv("ADL_SEMANTIC_PUBLICATIONS_TABLE", "fictional-publications")

    settings = config.operational_projector_settings()

    assert settings == config.OperationalProjectorSettings(
        status_table="fictional-operational-status",
        publications_table="fictional-publications",
    )

    monkeypatch.delenv("ADL_SEMANTIC_PUBLICATIONS_TABLE")
    config.operational_projector_settings.cache_clear()
    with pytest.raises(InvariantError) as caught:
        config.operational_projector_settings()
    assert "semantic_publications_table" in str(caught.value.context["missing"])


def test_stage1_recovery_settings_require_only_the_two_deployed_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(
        monkeypatch,
        {
            "ADL_SEMANTIC_PUBLICATIONS_TABLE": "fictional-publications",
            "ADL_OPERATIONAL_STATUS_TABLE": "fictional-operational-status",
        },
    )

    settings = config.stage1_outbox_recovery_settings()

    assert settings == config.Stage1OutboxRecoverySettings(
        publications_table="fictional-publications",
        status_table="fictional-operational-status",
    )
    with pytest.raises(FrozenInstanceError):
        settings.status_table = "changed"  # type: ignore[misc]
