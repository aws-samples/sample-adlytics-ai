# SPDX-License-Identifier: MIT-0
"""Event unwrapping and permanent Semantic V2 ingestion behavior.

No AWS and no I/O: collaborators are replaced at the handler's adapter seams. SDK behavior remains
covered by the real-resource integration tier.
"""

from __future__ import annotations

# This module intentionally exercises the handler's private composition seams.
# pyright: reportPrivateUsage=false
import inspect
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Never, cast
from unittest.mock import MagicMock

import pytest
from aws_lambda_powertools.metrics import MetricUnit
from phase_one_support import (
    CATALOG,
    DELIVERY_ENVIRONMENT,
    SETTINGS,
    TARGET,
    prepared,
    published,
)

from adapters.inbound.ingestion_handlers.delivery_plan import ProjectionComposition
from adapters.inbound.ingestion_handlers.inline_run import InlineRunOutcome
from adapters.jobs.ingest_delivery.landing import LoadedDelivery, ManifestInput
from adapters.jobs.ingest_delivery.semantic_sidecar import (
    SemanticSidecarOutcome,
    SemanticSidecarProgress,
    SemanticSidecarResult,
    SemanticSidecarTooling,
)
from adapters.outbound.telemetry.reporting import OperationLog
from domain.curation.landing import Manifest
from domain.operations.table_attempt import WriterKind
from domain.semantics.source_aligned import LandedDeliveryRef
from domain.shared_kernel.errors import (
    AdlyticsError,
    DependencyError,
    InvariantError,
    ValidationError,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from adapters.jobs.ingest_delivery.semantic_ingestion import SemanticIngestionRequest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-043", "ADR-030")]


def _sqs(*details: dict[str, Any]) -> dict[str, Any]:
    """Wrap S3 details the way EventBridge and then SQS actually wrap them."""
    return {
        "Records": [
            {
                "messageId": f"message-{index}",
                "attributes": {"ApproximateReceiveCount": "1"},
                "body": json.dumps(
                    {
                        "version": "0",
                        "source": "aws.s3",
                        "detail-type": "Object Created",
                        "detail": detail,
                    }
                ),
            }
            for index, detail in enumerate(details)
        ]
    }


def _landed(key: str, bucket: str = "adlytics-test-raw") -> dict[str, Any]:
    return {
        "bucket": {"name": bucket},
        "object": {"key": key, "size": 338, "version-id": "manifest-version-001"},
    }


MANIFEST = (
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    "amc/sponsored_ads_traffic/marketplace=ATVPDKIKX0DER/window=2026-08-21_2026-08-21/"
    "options=44136fa355b3678a/retrieved=20260824T060000Z/manifest.json"
)


def _keys(event: dict[str, Any]) -> tuple[str, ...]:
    """Import inside the call so a collection error cannot come from module-scope clients.

    The module resolves settings and builds boto3 clients at import, which is correct for a handler
    — an unhealthy container must fail before it serves — and means the environment has to be in
    place before the import happens. `manifest_keys` itself touches neither.
    """
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415 — see above

    return load.manifest_keys(event)


def _messages(event: dict[str, Any]) -> tuple[Any, ...]:
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    return load.ingestion_messages(event)


class TestReadingTheEvent:
    def test_a_landed_manifest_yields_its_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch)
        assert _keys(_sqs(_landed(MANIFEST))) == (MANIFEST,)

    def test_the_key_is_taken_verbatim_and_never_decoded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A delivery prefix carries `=` in every partition segment.

        EventBridge delivers the raw key, unlike the legacy S3 notification format which
        percent-encodes it. Decoding one that was never encoded produces a missing object rather
        than an error, which is the hardest kind of wrong to trace back here.
        """
        assert "=" in MANIFEST
        _configure(monkeypatch)
        assert _keys(_sqs(_landed(MANIFEST)))[0] == MANIFEST

    def test_an_object_that_is_not_a_marker_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rule already filters on the suffix, so this means the two disagree.

        Skipped rather than failed: retrying will not change the key, so a failure would spend three
        attempts and dead-letter a message nothing can act on.
        """
        _configure(monkeypatch)
        data = MANIFEST.replace("manifest.json", "data-0001.csv")
        assert _keys(_sqs(_landed(data))) == ()

    def test_a_file_merely_ending_in_the_name_is_not_a_marker(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The suffix includes the separator, so `not-a-manifest.json` is not one."""
        _configure(monkeypatch)
        impostor = MANIFEST.replace("/manifest.json", "/not-a-manifest.json")
        assert _keys(_sqs(_landed(impostor))) == ()

    def test_several_records_all_come_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Batch size is one today. This is what stops raising it becoming a silent data loss."""
        _configure(monkeypatch)
        second = MANIFEST.replace("2026-08-21_2026-08-21", "2026-08-22_2026-08-22")
        assert _keys(_sqs(_landed(MANIFEST), _landed(second))) == (MANIFEST, second)

    def test_a_message_carrying_no_object_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Something other than the manifest rule is writing to the queue.

        A misconfigured target rather than a bad delivery, so it must not be skipped into a green
        invocation — an empty batch and a wrong producer look identical otherwise.
        """
        _configure(monkeypatch)
        with pytest.raises(ValueError, match="S3"):
            _keys({"Records": [{"messageId": "m", "body": json.dumps({"detail": {}})}]})

    def test_a_bucket_with_no_key_is_refused_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Either half missing is a message this function cannot act on.

        Found by mutation review: with both halves absent, an `and` in place of the `or` still
        raises, so the original tests could not tell the two spellings apart. A notification naming
        a bucket and no object would then have been read as key `""` and fetched from
        `s3://bucket/`, which returns a rejected landing — a corrupt-delivery story for what is
        actually a misconfigured producer.
        """
        _configure(monkeypatch)
        with pytest.raises(ValueError, match="S3"):
            _keys(_sqs({"bucket": {"name": "adlytics-dev-raw-000000000000"}}))

    def test_a_key_with_no_bucket_is_refused_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch)
        with pytest.raises(ValueError, match="S3"):
            _keys(_sqs({"object": {"key": MANIFEST}}))

    def test_a_skipped_object_does_not_abandon_the_rest_of_the_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping is per record, not per batch.

        Found by mutation review: every multi-record test used valid markers, so `break` in place of
        `continue` survived. A batch whose first object was a data file would then have silently
        dropped every marker behind it — deliveries acknowledged and never loaded, with the queue
        reporting success.
        """
        _configure(monkeypatch)
        data = MANIFEST.replace("manifest.json", "data-0001.csv")
        assert _keys(_sqs(_landed(data), _landed(MANIFEST))) == (MANIFEST,)

    def test_redelivery_derives_a_new_execution_linked_to_the_prior_receive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        first_event = _sqs(_landed(MANIFEST))
        second_event = _sqs(_landed(MANIFEST))
        second_event["Records"][0]["attributes"]["ApproximateReceiveCount"] = "2"

        first = _messages(first_event)[0]
        second = _messages(second_event)[0]

        assert first.bucket == "adlytics-test-raw"
        assert first.manifest == ManifestInput(MANIFEST, "manifest-version-001")
        assert first.predecessor_execution_id is None
        assert second.execution_id != first.execution_id
        assert second.predecessor_execution_id == first.execution_id

    def test_separate_sqs_messages_get_separate_execution_identities(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        event = _sqs(_landed(MANIFEST), _landed(MANIFEST))
        first, second = _messages(event)
        assert first.execution_id != second.execution_id

    @pytest.mark.parametrize(
        "case",
        (
            ("messageId", "", "messageId"),
            ("ApproximateReceiveCount", "0", "ApproximateReceiveCount"),
            ("ApproximateReceiveCount", 1, "ApproximateReceiveCount"),
            ("version-id", "", "VersionId"),
            ("version-id", 1, "VersionId"),
        ),
    )
    def test_marker_correlation_fields_fail_closed_when_invalid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case: tuple[str, object, str],
    ) -> None:
        field, value, match = case
        _configure(monkeypatch)
        event = _sqs(_landed(MANIFEST))
        record = event["Records"][0]
        if field == "messageId":
            record[field] = value
        elif field == "ApproximateReceiveCount":
            record["attributes"][field] = value
        else:
            detail = json.loads(record["body"])
            detail["detail"]["object"][field] = value
            record["body"] = json.dumps(detail)
        with pytest.raises(ValueError, match=match):
            _messages(event)

    @pytest.mark.parametrize(
        "field",
        ("messageId", "ApproximateReceiveCount", "version-id"),
    )
    def test_marker_correlation_fields_fail_closed_when_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        field: str,
    ) -> None:
        _configure(monkeypatch)
        event = _sqs(_landed(MANIFEST))
        record = event["Records"][0]
        if field == "messageId":
            del record[field]
        elif field == "ApproximateReceiveCount":
            del record["attributes"][field]
        else:
            detail = json.loads(record["body"])
            del detail["detail"]["object"][field]
            record["body"] = json.dumps(detail)
        with pytest.raises(ValueError):
            _messages(event)

    def test_an_empty_batch_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _configure(monkeypatch)
        assert _keys({"Records": []}) == ()


def _configure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the module enough environment to import without reaching AWS.

    Only the configuration is faked. `boto3.client` builds a client without a network call, so
    importing the module is safe with no credentials — what would not be safe is *using* one, and
    nothing here does.

    Every value is obviously a stand-in. A plausible-looking pool id would send the next reader
    looking for it.
    """
    for key, value in {
        "ADL_ENVIRONMENT": "test",
        "ADL_AWS_REGION": "us-east-1",
        "ADL_COGNITO_USER_POOL_ID": "unused-by-ingestion",
        "ADL_COGNITO_CLIENT_ID": "unused-by-ingestion",
        "ADL_COGNITO_LOGIN_DOMAIN": "auth.test.invalid",
        "ADL_APP_BASE_URL": "https://app.test.invalid",
        "ADL_API_BASE_URL": "https://api.test.invalid",
        "ADL_COOKIE_SCOPE": "host_only",
        "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
        "ADL_ATHENA_WORKGROUP": "adlytics-test-interactive",
        "ADL_SEMANTIC_ATTEMPTS_TABLE": "adlytics-test-semantic-attempts",
        "ADL_SEMANTIC_LEASES_TABLE": "adlytics-test-semantic-leases",
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": "adlytics-test-semantic-publications",
        "ADL_RAW_BUCKET": "adlytics-test-raw",
        "ADL_EXECUTION_GRAPH_TABLE": "adlytics-test-execution-graph",
        "ADL_SEMANTIC_V2_BUCKET": "adlytics-test-semantic",
        "ADL_SEMANTIC_V2_ATHENA_BAKE_WORKGROUP": "adlytics-test-semantic-bake",
        "ADL_SEMANTIC_V2_SOURCE_DATABASE": "adlytics_test_source",
        "ADL_SEMANTIC_V2_CANDIDATE_DATABASE": "adlytics_test_candidate",
        "ADL_SEMANTIC_V2_CURRENT_DATABASE": "adlytics_test_current",
        "ADL_SEMANTIC_V2_STAGING_DATABASE": "adlytics_test_staging",
        "ADL_SEMANTIC_V2_STAGING_BUCKET": "adlytics-test-staging",
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "0",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
        "AWS_DEFAULT_REGION": "us-east-1",
        **DELIVERY_ENVIRONMENT,
    }.items():
        monkeypatch.setenv(key, value)


def _delivery(*, landed_ref: LandedDeliveryRef | None = None) -> LoadedDelivery:
    reference = landed_ref or LandedDeliveryRef(
        manifest_key=MANIFEST,
        manifest_version="manifest-version-001",
        manifest_checksum="sha256:manifest-001",
        object_key=MANIFEST.replace("manifest.json", "data-0001.csv"),
        object_version="object-version-001",
        object_checksum="sha256:object-001",
        first_observed_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    return LoadedDelivery(
        manifest=cast(Manifest, object()),
        manifest_key=MANIFEST,
        text="landed bytes",
        landed_ref=reference,
    )


def _phase_one_runtime(load: Any, tooling: SemanticSidecarTooling) -> Any:
    """Supply injected queued-composition collaborators to focused execution tests.

    The real fictional target, catalog, and §11 settings are used because the published roster
    and its feasibility re-proof read them even for an empty receipt set.
    """
    return load._V2Runtime(
        bucket="raw-v2",
        tooling=tooling,
        target=TARGET,
        catalog=CATALOG,
        publications=MagicMock(),
        coordinator=MagicMock(),
        reads=MagicMock(),
        settings=SETTINGS,
        composition=ProjectionComposition.QUEUED,
        inline=None,
        mode_item=lambda: None,
        ingress=load.DeliveryIngress(
            mode=lambda: load.ModeState(load.AuthorityMode.LEGACY, 0),
            admit=None,
            visibility=MagicMock(),
            requester=lambda: "arn:aws:sts::000000000000:assumed-role/ingest/fn",
            logger=MagicMock(),
        ),
        materialization=_PassThroughMaterialization(load),
    )


class _PassThroughMaterialization:
    """The governed port with its historical check passed: these tests cover the legacy path."""

    def __init__(self, load: Any) -> None:
        self._load = load

    def fetch(self, manifest: Any, claim: Any = None) -> Any:
        del claim
        return self._load.fetch_v2_delivery(self._load.S3, "raw-v2", manifest)


def _inline_runtime(load: Any) -> Any:
    """Compose the same collaborators for the transitional inline writer."""
    return replace(
        _phase_one_runtime(load, cast(SemanticSidecarTooling, object())),
        composition=ProjectionComposition.INLINE,
        inline=cast(Any, object()),
    )


def _no_budget() -> int:
    raise AssertionError("the remaining-time budget is consumed only by an inline run")


class TestPermanentSemanticRuntime:
    def test_typed_runtime_retains_phase_one_collaborators(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        semantic = cast(SemanticSidecarTooling, object())
        runtime = _phase_one_runtime(load, semantic)

        assert isinstance(runtime, load._V2Runtime)
        assert runtime.bucket == "raw-v2"
        assert runtime.tooling is semantic
        assert runtime.target is TARGET
        assert runtime.catalog is CATALOG
        assert runtime.settings is SETTINGS
        assert runtime.publications is not None
        assert runtime.coordinator is not None
        assert runtime.reads is not None
        assert runtime.composition is ProjectionComposition.QUEUED
        assert runtime.inline is None

    @pytest.mark.parametrize(
        "mismatch",
        (
            {"composition": ProjectionComposition.QUEUED, "inline": object()},
            {"composition": ProjectionComposition.INLINE, "inline": None},
        ),
    )
    def test_runtime_pairs_an_inline_writer_exactly_with_the_inline_composition(
        self, monkeypatch: pytest.MonkeyPatch, mismatch: dict[str, Any]
    ) -> None:
        """§9: the queued composition constructs no Athena client and runs no statement."""
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        runtime = _phase_one_runtime(load, cast(SemanticSidecarTooling, object()))
        with pytest.raises(ValueError, match="inline runtime"):
            replace(runtime, **mismatch)

    def test_deployed_composition_follows_the_handoff_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        assert DELIVERY_ENVIRONMENT["ADL_PROJECTION_HANDOFF_STATE"] == "INLINE_ACTIVE"
        assert load.COMPOSITION is ProjectionComposition.INLINE
        assert load.RUNTIME.inline is not None
        assert load.DELIVERY_SETTINGS.table_lease_duration_seconds == 930

    def test_composition_refuses_a_missing_raw_bucket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        monkeypatch.setattr(load, "SETTINGS", load.SETTINGS.model_copy(update={"raw_bucket": " "}))
        with pytest.raises(Exception, match="raw bucket"):
            load._runtime()

    @pytest.mark.covers("ADR-149")
    @pytest.mark.parametrize(
        "case",
        [
            ("semantic_v2_staging_database", "staging database"),
            ("semantic_v2_staging_bucket", "staging bucket"),
        ],
    )
    def test_composition_refuses_a_blank_staging_setting(
        self, monkeypatch: pytest.MonkeyPatch, case: tuple[str, str]
    ) -> None:
        field, label = case
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        # Control: the module imported, so the runtime built with every staging setting present.
        assert load.RUNTIME is not None
        monkeypatch.setattr(load, "SETTINGS", load.SETTINGS.model_copy(update={field: " "}))
        with pytest.raises(ValueError, match=label):
            load._runtime()

    @pytest.mark.covers("ADR-149")
    def test_the_inline_runtime_stages_and_the_queued_composition_builds_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415
        from adapters.outbound.s3.unit_staging import S3UnitStager  # noqa: PLC0415

        inline = load._inline_runtime(load.RUNTIME.coordinator, load.RUNTIME.reads)
        assert isinstance(inline.stager, S3UnitStager)
        # Control: the shipped queued composition builds no inline runtime, so nothing stages
        # in the delivery function.
        assert load.COMPOSITION is not load.ProjectionComposition.INLINE
        assert load.RUNTIME.inline is None

    def test_execution_uses_exact_fetch_and_semantic_ingestion(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        delivery = _delivery()
        tooling = cast(SemanticSidecarTooling, object())
        results = (
            SemanticSidecarResult(
                contract_id="contract-v2",
                outcome=SemanticSidecarOutcome.PUBLISHED,
                detail="published exact landed evidence",
            ),
        )
        captured: list[SemanticIngestionRequest] = []
        operations: list[Any] = []
        traced = MagicMock()
        fetch_span = MagicMock()
        traced.return_value.__enter__.return_value = fetch_span

        def capture_operation(event: object, _logger: object) -> None:
            operations.append(event)

        def fetch(client: object, bucket: str, manifest: ManifestInput) -> LoadedDelivery:
            assert client is load.S3
            assert (bucket, manifest) == (
                "raw-v2",
                ManifestInput(MANIFEST, "manifest-version-001"),
            )
            return delivery

        def semantic_ingest(
            request: SemanticIngestionRequest,
            selected_tooling: SemanticSidecarTooling,
        ) -> tuple[SemanticSidecarResult, ...]:
            assert selected_tooling is tooling
            captured.append(request)
            request.progress(SemanticSidecarProgress("publish", ("contract-v2",), 1, 1))
            return results

        monkeypatch.setattr(load, "RUNTIME", _phase_one_runtime(load, tooling))
        monkeypatch.setattr(load, "fetch_v2_delivery", fetch)
        monkeypatch.setattr(load, "ingest_semantic_delivery", semantic_ingest)
        monkeypatch.setattr(load, "report_operation", capture_operation)
        monkeypatch.setattr(load, "span", traced)
        now = datetime(2026, 8, 25, tzinfo=UTC)

        message = load.IngestionMessage(
            "raw-v2",
            ManifestInput(MANIFEST, "manifest-version-001"),
            "execution-message-1",
            "execution-message-0",
        )
        outcome = load._execute(message, now, _no_budget)

        # No receipts, so no unit: authority is published and neither a plan nor an intent exists.
        assert (outcome.results, outcome.write_plan_key) == (results, load.NO_PLAN_KEY)
        assert (outcome.intent_id, outcome.acknowledgment) == (None, None)
        assert outcome.plan_digest == load._delivery_identity(message).plan_digest
        (request,) = captured
        assert (request.delivery, request.now) == (delivery, now)
        assert (request.contracts, request.columns, request.installed_governance) == (
            load.CONTRACTS,
            load.COLUMNS,
            load.INSTALLED_GOVERNANCE,
        )
        assert request.operation_id == load.v2_operation_id(delivery)
        assert request.telemetry_operation_id == load._message_operation_id(message)
        assert (request.execution_id, request.predecessor_execution_id) == (
            "execution-message-1",
            "execution-message-0",
        )
        assert request.plan_digest == outcome.plan_digest
        assert len(request.plan_digest) == 64
        traced.assert_called_once_with(
            load.INGEST_FETCH,
            operation_id=request.telemetry_operation_id,
            execution_id="execution-message-1",
            predecessor_execution_id="execution-message-0",
            plan_digest=request.plan_digest,
            stage="fetch",
            completed=0,
            total=1,
        )
        fetch_span.record.assert_called_once_with(
            completed=1,
            outcome="completed",
        )
        assert len(operations) == 1
        progress = operations[0]
        assert progress.operation == "ingest_delivery"
        assert progress.component == "ingest_handler"
        assert progress.phase == "progress"
        assert progress.outcome == "completed"
        assert progress.details == {
            "operation_id": request.telemetry_operation_id,
            "execution_id": "execution-message-1",
            "predecessor_execution_id": "execution-message-0",
            "plan_digest": request.plan_digest,
            "stage": "publish",
            "completed": 1,
            "total": 1,
            "contract_ids": ("contract-v2",),
        }

    @pytest.mark.parametrize("bucket", ("other-raw-v2", "RAW-V2", "raw-v2 "))
    def test_execution_refuses_a_marker_from_another_exact_bucket_before_s3(
        self,
        monkeypatch: pytest.MonkeyPatch,
        bucket: str,
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        called = False

        def fetch(*args: object, **kwargs: object) -> Never:
            nonlocal called
            del args, kwargs
            called = True
            raise AssertionError("bucket mismatch reached S3")

        monkeypatch.setattr(
            load,
            "RUNTIME",
            _phase_one_runtime(load, cast(SemanticSidecarTooling, object())),
        )
        monkeypatch.setattr(load, "fetch_v2_delivery", fetch)
        message = load.IngestionMessage(
            bucket,
            ManifestInput(MANIFEST, "manifest-version-001"),
            "execution-message-1",
            None,
        )

        with pytest.raises(ValueError, match="outside this runtime"):
            load._execute(message, datetime(2026, 8, 25, tzinfo=UTC), _no_budget)

        assert not called

    def test_feasibility_callback_refuses_an_over_budget_prepared_plan_before_authority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§2.1: the callback runs after grouping and before the first lease or mutation."""
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415
        from adapters.inbound.ingestion_handlers.delivery_plan import (  # noqa: PLC0415
            FEASIBILITY_REFUSED,
        )

        runtime = _phase_one_runtime(load, cast(SemanticSidecarTooling, object()))
        load._prove_feasible(runtime, datetime(2026, 8, 25, tzinfo=UTC), prepared())

        tight = replace(runtime, settings=replace(SETTINGS, max_units=2))
        with pytest.raises(ValidationError) as caught:
            load._prove_feasible(tight, datetime(2026, 8, 25, tzinfo=UTC), prepared())
        assert caught.value.code == FEASIBILITY_REFUSED
        assert caught.value.context["reason"] == "units_exceed_cap"

    def test_inline_composition_drives_every_unit_in_roster_order_and_correlates_receipts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415
        from adapters.inbound.ingestion_handlers.inline_run import (  # noqa: PLC0415
            InlineRunOutcome,
            InlineRunResult,
        )

        runtime = _inline_runtime(load)
        results = published()
        roster = load.published_roster(
            load.PublishedRosterRequest(results=results, target=TARGET, catalog=CATALOG)
        )
        seen: list[Any] = []

        def run(inline: object, request: Any) -> InlineRunResult:
            assert inline is runtime.inline
            seen.append(request)
            return InlineRunResult(
                InlineRunOutcome.FINALIZED,
                f"run-{len(seen)}",
                retryable=False,
                flow_id=f"00000000-0000-4000-8000-{len(seen):012d}",
                execution_id=f"10000000-0000-4000-8000-{len(seen):012d}",
                predecessor_execution_id=None,
            )

        monkeypatch.setattr(load, "run_inline_table", run)
        delivery = load._InlineDelivery(
            roster=roster,
            operation_id="v2-runtime-fictional",
            execution_id="execution-message-1",
            remaining_budget_ms=lambda: 600_000,
        )

        outcomes = load._run_inline_tables(runtime, delivery)

        assert [request.physical_table_id for request in seen] == [
            unit.entry.unit_key for unit in roster.units
        ]
        assert [request.table_kind for request in seen] == [
            unit.entry.table_kind for unit in roster.units
        ]
        assert {request.writer_kind for request in seen} == {WriterKind.TRANSITIONAL_DELIVERY}
        assert {request.operation_id for request in seen} == {"v2-runtime-fictional"}
        assert {request.execution_id for request in seen} == {"execution-message-1"}
        assert [result.outcome for result in outcomes] == [InlineRunOutcome.FINALIZED] * 3
        correlated = load._with_write_plan_key(results, load.INLINE_PLAN_KEY)
        assert {receipt.write_plan_key for result in correlated for receipt in result.receipts} == {
            "inline"
        }

    @pytest.mark.parametrize(
        "case",
        (
            (InlineRunOutcome.REFUSED_TABLE_BUSY, True, DependencyError, "INLINE_RUN_RETRYABLE"),
            (InlineRunOutcome.STATEMENT_FAILED, False, InvariantError, "INLINE_RUN_FAILED"),
        ),
    )
    def test_inline_refusals_retry_the_delivery_and_failures_raise_after_authority(
        self,
        monkeypatch: pytest.MonkeyPatch,
        case: tuple[str, bool, type[AdlyticsError], str],
    ) -> None:
        """§9: TABLE_BUSY and TABLE_ATTEMPT_PENDING are typed retries; nothing else is."""
        outcome, retryable, error_type, code = case
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415
        from adapters.inbound.ingestion_handlers.inline_run import (  # noqa: PLC0415
            InlineRunResult,
        )

        runtime = _inline_runtime(load)
        roster = load.published_roster(
            load.PublishedRosterRequest(results=published(), target=TARGET, catalog=CATALOG)
        )
        calls = 0

        def run(_inline: object, _request: Any) -> InlineRunResult:
            nonlocal calls
            calls += 1
            return InlineRunResult(
                outcome,
                "run-1",
                retryable=retryable,
                flow_id="00000000-0000-4000-8000-000000000001",
                execution_id="10000000-0000-4000-8000-000000000001",
                predecessor_execution_id=None,
            )

        monkeypatch.setattr(load, "run_inline_table", run)
        delivery = load._InlineDelivery(
            roster=roster,
            operation_id="v2-runtime-fictional",
            execution_id="execution-message-1",
            remaining_budget_ms=lambda: 600_000,
        )

        with pytest.raises(error_type) as caught:
            load._run_inline_tables(runtime, delivery)

        assert getattr(load, code) == caught.value.code
        assert caught.value.context == {"table": roster.units[0].entry.unit_key, "outcome": outcome}
        assert calls == 1, "the first non-finalized table stops the delivery"

    def test_queued_composition_never_runs_a_table_inline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        runtime = _phase_one_runtime(load, cast(SemanticSidecarTooling, object()))
        roster = load.published_roster(
            load.PublishedRosterRequest(results=published(), target=TARGET, catalog=CATALOG)
        )
        with pytest.raises(InvariantError) as caught:
            load._run_inline_tables(
                runtime,
                load._InlineDelivery(roster, "v2-runtime-fictional", "execution-1", _no_budget),
            )
        assert caught.value.code == load.INLINE_RUN_FAILED


class TestSemanticRuntimeReporting:
    def test_operation_identity_uses_every_exact_landed_version_and_checksum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        delivery = _delivery()
        reference = delivery.landed_ref
        operation_id = load.v2_operation_id(delivery)
        assert reference.first_observed_at is not None
        later_observation = replace(
            reference,
            first_observed_at=reference.first_observed_at + timedelta(hours=1),
        )
        changed_evidence = (
            replace(reference, manifest_key=f"prefix/{reference.manifest_key}"),
            replace(reference, manifest_version="manifest-version-002"),
            replace(reference, manifest_checksum="sha256:manifest-002"),
            replace(reference, object_key=f"prefix/{reference.object_key}"),
            replace(reference, object_version="object-version-002"),
            replace(reference, object_checksum="sha256:object-002"),
        )

        assert operation_id.startswith("v2-runtime-")
        assert load.v2_operation_id(_delivery(landed_ref=later_observation)) == operation_id
        assert {
            load.v2_operation_id(_delivery(landed_ref=changed)) for changed in changed_evidence
        }.isdisjoint({operation_id})

    def test_v2_results_emit_one_bounded_outcome_metric_and_a_decision_log(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        emitted: list[tuple[dict[str, object], list[tuple[str, str]]]] = []
        operations: list[Any] = []

        class _Metric:
            def __init__(self) -> None:
                self.dimensions: list[tuple[str, str]] = []

            def add_dimension(self, *, name: str, value: str) -> None:
                self.dimensions.append((name, value))

        @contextmanager
        def metric(**kwargs: object) -> Generator[_Metric]:
            item = _Metric()
            emitted.append((kwargs, item.dimensions))
            yield item

        def capture_operation(event: object, _logger: object) -> None:
            operations.append(event)

        monkeypatch.setattr(load, "single_metric", metric)
        monkeypatch.setattr(load, "report_operation", capture_operation)
        results = (
            SemanticSidecarResult(
                "published-contract",
                SemanticSidecarOutcome.PUBLISHED,
                "published",
            ),
            SemanticSidecarResult(
                "blocked-contract",
                SemanticSidecarOutcome.BLOCKED,
                "blocked",
            ),
        )

        load._report_v2(
            MANIFEST,
            load._V2Outcome(
                "operation-v2-runtime-result",
                "v2-runtime-result",
                "sha256:v2-runtime-plan",
                results,
                write_plan_key=load.INLINE_PLAN_KEY,
                run_flow_bindings=(
                    (
                        "00000000-0000-4000-8000-000000000001",
                        "10000000-0000-4000-8000-000000000001",
                        None,
                    ),
                ),
            ),
            elapsed_ms=1234.5,
            receive_count=1,
        )

        assert emitted == [
            (
                {
                    "name": load.PARTITION_METRIC,
                    "unit": MetricUnit.Count,
                    "value": 1,
                    "namespace": load.METRICS_NAMESPACE,
                },
                [("Action", SemanticSidecarOutcome.PUBLISHED.value)],
            ),
            (
                {
                    "name": load.PARTITION_METRIC,
                    "unit": MetricUnit.Count,
                    "value": 1,
                    "namespace": load.METRICS_NAMESPACE,
                },
                [("Action", SemanticSidecarOutcome.BLOCKED.value)],
            ),
        ]
        assert len(operations) == 1
        operation = operations[0]
        assert operation.operation == "ingest_delivery"
        assert operation.component == "ingest_handler"
        assert operation.phase == "terminal"
        assert operation.outcome == "completed"
        assert operation.details == {
            "manifest_key": MANIFEST,
            "receive_count": 1,
            "operation_id": "operation-v2-runtime-result",
            "semantic_operation_id": "v2-runtime-result",
            "plan_digest": "sha256:v2-runtime-plan",
            "write_plan_key": "inline",
            "intent_id": "none",
            "proved_outcome_ordinal": 0,
            "source_acknowledgment_digest": "none",
            "contracts": 2,
            "outcomes": ["blocked", "published"],
            "publications": 0,
            "elapsed_ms": 1234.5,
            "run_flows": [
                {
                    "flow_id": "00000000-0000-4000-8000-000000000001",
                    "execution_id": "10000000-0000-4000-8000-000000000001",
                    "predecessor_execution_id": None,
                }
            ],
        }
        assert vars(load.handler)["__trace_boundaries__"] == (load.INGEST_HANDLER_PROCESS,)

    @pytest.mark.parametrize(
        ("outcome", "action"),
        [
            # Every bucket older than CURRENT: counted as superseded, never as blocked.
            (SemanticSidecarOutcome.SUPERSEDED, "superseded"),
            # Paired control: a genuine block still counts on the alarmed action.
            (SemanticSidecarOutcome.BLOCKED, "blocked"),
        ],
    )
    def test_superseded_and_blocked_count_on_separate_partition_actions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        outcome: SemanticSidecarOutcome,
        action: str,
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        actions: list[list[tuple[str, str]]] = []

        class _Metric:
            def __init__(self) -> None:
                self.dimensions: list[tuple[str, str]] = []

            def add_dimension(self, *, name: str, value: str) -> None:
                self.dimensions.append((name, value))

        @contextmanager
        def metric(**kwargs: object) -> Generator[_Metric]:
            assert kwargs["name"] == load.PARTITION_METRIC
            item = _Metric()
            actions.append(item.dimensions)
            yield item

        monkeypatch.setattr(load, "single_metric", metric)

        def ignore_operation(_event: object, _logger: object) -> None:
            return None

        monkeypatch.setattr(load, "report_operation", ignore_operation)
        load._report_v2(
            MANIFEST,
            load._V2Outcome(
                "operation-v2-outcome-action",
                "v2-outcome-action",
                "sha256:v2-outcome-action",
                (SemanticSidecarResult("contract", outcome, "decided"),),
                write_plan_key=load.INLINE_PLAN_KEY,
                run_flow_bindings=(),
            ),
            elapsed_ms=1.0,
            receive_count=1,
        )

        assert actions == [[("Action", action)]]

    def test_load_reports_and_propagates_an_undecided_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _configure(monkeypatch)
        from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

        failure = RuntimeError("dependency failed")
        reported: list[BaseException] = []

        def fail(message: object, now: datetime, budget: object) -> Never:
            del message, now, budget
            raise failure

        def report(error: BaseException, operation: object) -> None:
            del operation
            reported.append(error)

        def ignore_metric(**kwargs: object) -> None:
            del kwargs

        monkeypatch.setattr(load, "_execute", fail)
        monkeypatch.setattr(load, "report_error", report)
        monkeypatch.setattr(load.METRICS, "add_metric", ignore_metric)

        with pytest.raises(RuntimeError) as raised:
            load._load_legacy(
                load.IngestionMessage(
                    "adlytics-test-raw",
                    ManifestInput(MANIFEST, "manifest-version-001"),
                    "execution-message-1",
                    None,
                ),
                _no_budget,
            )

        assert raised.value is failure
        assert reported == [failure]


def test_handler_root_span_encloses_event_parsing_and_records_batch_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    traced = MagicMock()
    process_span = MagicMock()
    traced.return_value.__enter__.return_value = process_span
    monkeypatch.setattr(load, "span", traced)

    assert inspect.unwrap(load.handler)({"Records": []}, object()) == {"batchItemFailures": []}

    traced.assert_called_once_with(load.INGEST_HANDLER_PROCESS, operation="ingest.handler")
    assert [item.kwargs for item in process_span.record.call_args_list] == [
        {"record_count": 0},
        {"outcome": "completed"},
    ]


def test_handler_reports_malformed_event_at_the_boundary_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    error_reports: list[BaseException] = []

    def capture_error(error: BaseException, _report: object) -> None:
        error_reports.append(error)

    monkeypatch.setattr(load, "report_error", capture_error)

    with pytest.raises(ValueError, match="Records list") as raised:
        inspect.unwrap(load.handler)({"Records": "invalid"}, object())

    assert error_reports == [raised.value]


def test_queued_terminal_serializes_no_unowned_run_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued delivery has no inline run proof; an empty roster keeps the reporter fail-closed."""
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    operations: list[OperationLog] = []

    def capture(event: OperationLog, _logger: object) -> None:
        operations.append(event)

    monkeypatch.setattr(load, "report_operation", capture)

    load._report_v2(
        MANIFEST,
        load._V2Outcome(
            operation_id="operation-queued-fixture",
            semantic_operation_id="v2-runtime-queued-fixture",
            plan_digest="sha256:queued-fixture",
            results=(),
            write_plan_key="WRITE_PLAN#fixture",
        ),
        elapsed_ms=100.0,
        receive_count=1,
    )

    assert len(operations) == 1
    assert operations[0].details["run_flows"] == []


# --- S6: ingress by mode (V-19) -------------------------------------------------------------------


def _s6_ingress(load: Any, modes: list[Any], visibility: list[tuple[str, str, int]]) -> Any:
    reads = iter(modes)

    def hold(queue: str, handle: str, seconds: int) -> None:
        visibility.append((queue, handle, seconds))

    return load.DeliveryIngress(
        mode=lambda: next(reads),
        admit=MagicMock(side_effect=AssertionError("no admission call during the drain")),
        visibility=hold,
        requester=lambda: "arn:aws:sts::000000000000:assumed-role/ingest/fn",
        logger=MagicMock(),
        now=lambda: datetime(2026, 10, 9, tzinfo=UTC),
    )


def _s6_message(load: Any) -> Any:
    return load.IngestionMessage(
        "raw-v2",
        ManifestInput(MANIFEST, "manifest-version-001"),
        "execution-message-1",
        None,
        "message-1",
        "handle-1",
        1,
        "https://sqs.us-east-1.amazonaws.com/000000000000/q",
    )


def test_v19a_a_draining_read_takes_no_legacy_path_and_leaves_the_message_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    visibility: list[tuple[str, str, int]] = []
    deadline = datetime(2026, 10, 9, 1, tzinfo=UTC)
    draining = load.ModeState(
        load.AuthorityMode.LEGACY_DRAINING, 2, drain_deadline=deadline.isoformat()
    )
    runtime = MagicMock()
    # S6-10: an abort that commits after the routing read must not reach the drain wait, so the
    # second mode (LEGACY) is never read; the wait uses the routing read's `D`.
    aborted = load.ModeState(load.AuthorityMode.LEGACY, 3)
    runtime.ingress = _s6_ingress(load, [draining, aborted], visibility)
    monkeypatch.setattr(load, "RUNTIME", runtime)
    monkeypatch.setattr(
        load, "_load_legacy", MagicMock(side_effect=AssertionError("no legacy path"))
    )
    assert load._load(_s6_message(load), _no_budget) is False
    assert visibility == [("https://sqs.us-east-1.amazonaws.com/000000000000/q", "handle-1", 3660)]
    runtime.coordinator.count_drain.assert_called_once_with(load.AuthorityMode.LEGACY_DRAINING)


def test_v19d_a_drain_that_already_ended_leaves_the_message_for_its_next_receive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S6-10: refused by the drain, then the abort committed; no error, no visibility change."""
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    visibility: list[tuple[str, str, int]] = []
    runtime = MagicMock()
    runtime.ingress = _s6_ingress(
        load,
        [
            load.ModeState(load.AuthorityMode.SHADOW, 1),
            load.ModeState(load.AuthorityMode.LEGACY, 3),
        ],
        visibility,
    )

    def refused(*_args: object) -> None:
        raise load.GraphAuthorityRefusedError("AUTHORITY_MODE_DRAINING")

    monkeypatch.setattr(load, "RUNTIME", runtime)
    monkeypatch.setattr(load, "_load_legacy", refused)
    assert load._load(_s6_message(load), _no_budget) is False
    assert visibility == []


def test_v19d_a_submission_refused_by_the_drain_takes_the_drain_wait_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler read SHADOW; the drain CAS committed before its submission (V-13(b))."""
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    visibility: list[tuple[str, str, int]] = []
    draining = load.ModeState(
        load.AuthorityMode.LEGACY_DRAINING,
        2,
        drain_deadline=datetime(2026, 10, 9, 0, 30, tzinfo=UTC).isoformat(),
    )
    runtime = MagicMock()
    runtime.ingress = _s6_ingress(
        load, [load.ModeState(load.AuthorityMode.SHADOW, 1), draining], visibility
    )

    def refused(*_args: object) -> None:
        raise load.GraphAuthorityRefusedError("AUTHORITY_MODE_DRAINING")

    monkeypatch.setattr(load, "RUNTIME", runtime)
    monkeypatch.setattr(load, "_load_legacy", refused)
    assert load._load(_s6_message(load), _no_budget) is False
    assert visibility == [("https://sqs.us-east-1.amazonaws.com/000000000000/q", "handle-1", 1860)]
    runtime.coordinator.count_drain.assert_called_once_with(load.AuthorityMode.SHADOW)


def test_v19b_shadow_runs_the_legacy_path_then_records_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch)
    from adapters.inbound.ingestion_handlers import load  # noqa: PLC0415

    order: list[str] = []
    runtime = MagicMock()
    runtime.ingress = load.DeliveryIngress(
        mode=lambda: load.ModeState(load.AuthorityMode.SHADOW, 1),
        admit=lambda request: order.append(f"admit:{request.caller_kind}") or MagicMock(),
        visibility=MagicMock(),
        requester=lambda: "arn:aws:sts::000000000000:assumed-role/ingest/fn",
        logger=MagicMock(),
    )
    monkeypatch.setattr(load, "RUNTIME", runtime)

    def legacy(*_args: object) -> None:
        order.append("legacy")

    monkeypatch.setattr(load, "_load_legacy", legacy)
    assert load._load(_s6_message(load), _no_budget) is True
    assert order == ["legacy", "admit:delivery_event"]
