# SPDX-License-Identifier: MIT-0
"""ADR-075 §5 step-3 dedicated Athena start client: the closed two-member result."""

from __future__ import annotations

from typing import Any, cast

import pytest
from botocore.exceptions import ClientError, ConnectTimeoutError, ReadTimeoutError

import adapters.outbound.athena.table_attempt_start as start_module
from adapters.outbound.athena.table_attempt_start import (
    START_RETRY_CONFIG,
    Stage1AthenaStartClient,
)
from adapters.outbound.athena.table_attempt_statements import (
    ComposeStartEnvelopeRequest,
    compose_start_envelope,
)
from adapters.outbound.telemetry.reporting import Report
from domain.operations.table_attempt import ProviderErrorClass, SanitizedStartEvidence
from ports.outbound.table_attempt_start import (
    AcceptanceAmbiguous,
    Accepted,
    StartTableAttemptRequest,
    TableAttemptStartClient,
)

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]

# Every identity below is fictional fixture data; none is copied from a live system.
TOKEN = "ab01" * 16
QUERY_ID = "query-exec-fixture-0001"
ENVELOPE = compose_start_envelope(
    ComposeStartEnvelopeRequest(
        workgroup="wg-fixture-batch",
        database="db_fixture",
        sql="MERGE INTO fixture",
        parameters=("alpha",),
    )
)
BARE_ENVELOPE = compose_start_envelope(
    ComposeStartEnvelopeRequest(
        workgroup="wg-fixture-batch", database="db_fixture", sql="SELECT 1", parameters=()
    )
)


class _StubAthenaClient:
    """Hand-rolled stub: records every call and returns or raises one configured case."""

    def __init__(self, response: object = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def start_query_execution(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


def _client(stub: _StubAthenaClient) -> Stage1AthenaStartClient:
    return Stage1AthenaStartClient(cast("Any", stub))


def _request(envelope: str = ENVELOPE) -> StartTableAttemptRequest:
    return StartTableAttemptRequest(start_envelope=envelope, client_request_token=TOKEN)


def _ok_response(query_id: object) -> dict[str, object]:
    return {"QueryExecutionId": query_id, "ResponseMetadata": {"HTTPStatusCode": 200}}


def _client_error(code: str, status: int) -> ClientError:
    response: dict[str, object] = {
        "Error": {"Code": code, "Message": "fixture"},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(cast("Any", response), "StartQueryExecution")


def test_a_valid_identity_returns_accepted_with_provider_error_class_none() -> None:
    stub = _StubAthenaClient(response=_ok_response(QUERY_ID))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, Accepted)
    assert result.query_execution_id == QUERY_ID
    assert result.sanitized_evidence == SanitizedStartEvidence(200, ProviderErrorClass.NONE)
    assert len(stub.calls) == 1


def test_the_call_replays_the_stored_envelope_verbatim_with_the_attempt_token() -> None:
    stub = _StubAthenaClient(response=_ok_response(QUERY_ID))
    _client(stub).start_table_attempt(_request())
    (call,) = stub.calls
    assert call["QueryString"] == "MERGE INTO fixture"
    assert call["WorkGroup"] == "wg-fixture-batch"
    assert call["QueryExecutionContext"] == {"Database": "db_fixture"}
    assert call["ClientRequestToken"] == TOKEN
    assert call["ExecutionParameters"] == ["'alpha'"]
    assert call["ResultReuseConfiguration"] == {"ResultReuseByAgeConfiguration": {"Enabled": False}}


def test_a_parameterless_envelope_sends_no_execution_parameters_key() -> None:
    stub = _StubAthenaClient(response=_ok_response(QUERY_ID))
    _client(stub).start_table_attempt(_request(BARE_ENVELOPE))
    (call,) = stub.calls
    assert "ExecutionParameters" not in call


@pytest.mark.parametrize(
    "identity", ["", "   ", 7, None], ids=["empty", "whitespace", "integer", "missing-type"]
)
def test_a_blank_or_wrong_typed_identity_is_ambiguous_response_malformed(
    identity: object,
) -> None:
    stub = _StubAthenaClient(response=_ok_response(identity))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.RESPONSE_MALFORMED
    assert result.sanitized_evidence.provider_status_code == 200


def test_a_response_missing_the_identity_field_is_ambiguous_response_malformed() -> None:
    stub = _StubAthenaClient(response={"ResponseMetadata": {"HTTPStatusCode": 200}})
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.RESPONSE_MALFORMED


def test_a_non_mapping_sdk_result_is_ambiguous_response_malformed_with_null_status() -> None:
    stub = _StubAthenaClient(response=["not", "a", "mapping"])
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.RESPONSE_MALFORMED
    assert result.sanitized_evidence.provider_status_code is None


@pytest.mark.parametrize("code", ["ThrottlingException", "Throttling", "TooManyRequestsException"])
def test_a_recognized_throttling_code_maps_to_throttling(code: str) -> None:
    stub = _StubAthenaClient(error=_client_error(code, 400))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.THROTTLING
    assert result.sanitized_evidence.provider_status_code == 400


def test_an_http_429_with_an_unmapped_code_still_maps_to_throttling() -> None:
    stub = _StubAthenaClient(error=_client_error("SomethingElse", 429))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.THROTTLING


def test_a_service_unavailable_signal_maps_to_service_unavailable() -> None:
    by_code = _client(
        _StubAthenaClient(error=_client_error("ServiceUnavailableException", 500))
    ).start_table_attempt(_request())
    assert isinstance(by_code, AcceptanceAmbiguous)
    assert by_code.reason is ProviderErrorClass.SERVICE_UNAVAILABLE
    by_status = _client(
        _StubAthenaClient(error=_client_error("SomethingElse", 503))
    ).start_table_attempt(_request())
    assert isinstance(by_status, AcceptanceAmbiguous)
    assert by_status.reason is ProviderErrorClass.SERVICE_UNAVAILABLE


def test_an_unmapped_client_error_is_exactly_unknown_with_its_status_code() -> None:
    stub = _StubAthenaClient(error=_client_error("InvalidRequestException", 400))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert result.sanitized_evidence.provider_status_code == 400


def test_a_transport_timeout_maps_to_network_timeout_with_null_status() -> None:
    for error in (
        ReadTimeoutError(endpoint_url="https://athena.fixture"),
        ConnectTimeoutError(endpoint_url="https://athena.fixture"),
    ):
        stub = _StubAthenaClient(error=error)
        result = _client(stub).start_table_attempt(_request())
        assert isinstance(result, AcceptanceAmbiguous)
        assert result.reason is ProviderErrorClass.NETWORK_TIMEOUT
        assert result.sanitized_evidence.provider_status_code is None
        assert len(stub.calls) == 1


def test_an_unknown_exception_class_is_exactly_unknown_and_never_raises() -> None:
    stub = _StubAthenaClient(error=RuntimeError("fixture transport defect"))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert result.sanitized_evidence.provider_status_code is None


def test_a_malformed_persisted_envelope_is_unknown_and_makes_no_sdk_call() -> None:
    stub = _StubAthenaClient(response=_ok_response(QUERY_ID))
    result = _client(stub).start_table_attempt(_request("not-a-canonical-envelope"))
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert stub.calls == []


def test_the_ambiguous_reason_is_always_the_selected_provider_error_class_value() -> None:
    stub = _StubAthenaClient(error=_client_error("ThrottlingException", 400))
    result = _client(stub).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is result.sanitized_evidence.provider_error_class


def test_the_adapter_satisfies_the_start_client_port() -> None:
    assert isinstance(_client(_StubAthenaClient()), TableAttemptStartClient)


def test_the_default_retry_configuration_permits_exactly_one_sdk_attempt() -> None:
    """Botocore adds ``mode`` to the shared retries map once any client is built from it."""
    retries = cast("dict[str, object]", cast("Any", START_RETRY_CONFIG).retries)
    assert retries["total_max_attempts"] == 1
    assert set(retries) <= {"total_max_attempts", "mode"}


# --- Closed port forms ----------------------------------------------------------------------------


def test_accepted_always_carries_provider_error_class_none() -> None:
    with pytest.raises(ValueError, match="none"):
        Accepted(
            query_execution_id=QUERY_ID,
            sanitized_evidence=SanitizedStartEvidence(None, ProviderErrorClass.THROTTLING),
        )


def test_accepted_requires_at_least_one_non_whitespace_character() -> None:
    with pytest.raises(ValueError, match="non-whitespace"):
        Accepted(
            query_execution_id="   ",
            sanitized_evidence=SanitizedStartEvidence(None, ProviderErrorClass.NONE),
        )


def test_an_ambiguous_reason_may_never_be_none_or_disagree_with_its_evidence() -> None:
    with pytest.raises(ValueError, match="none"):
        AcceptanceAmbiguous(
            reason=ProviderErrorClass.NONE,
            sanitized_evidence=SanitizedStartEvidence(None, ProviderErrorClass.NONE),
        )
    with pytest.raises(ValueError, match="exactly"):
        AcceptanceAmbiguous(
            reason=ProviderErrorClass.THROTTLING,
            sanitized_evidence=SanitizedStartEvidence(None, ProviderErrorClass.UNKNOWN),
        )


def test_the_start_request_requires_the_64_hex_attempt_token_and_an_envelope() -> None:
    with pytest.raises(ValueError, match="lowercase hex"):
        # A-TEST-LITERALS-001: the short token under test; see docs/security-dispositions.md
        StartTableAttemptRequest(start_envelope=ENVELOPE, client_request_token="short")  # nosec B106
    with pytest.raises(ValueError, match="envelope"):
        StartTableAttemptRequest(start_envelope="  ", client_request_token=TOKEN)


def _reports(monkeypatch: pytest.MonkeyPatch) -> list[tuple[BaseException, str, str]]:
    reported: list[tuple[BaseException, str, str]] = []

    def capture(error: BaseException, report: Report) -> None:
        reported.append((error, report.operation, report.correlation_id))

    monkeypatch.setattr(start_module, "report_error", capture)
    return reported


@pytest.mark.parametrize(
    "error",
    [_client_error("InvalidRequestException", 400), RuntimeError("fixture transport defect")],
    ids=["client-error", "unknown"],
)
def test_a_failed_start_is_reported_against_the_attempt_before_it_becomes_ambiguous(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    reported = _reports(monkeypatch)
    result = _client(_StubAthenaClient(error=error)).start_table_attempt(_request())
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert reported == [(error, "table_attempt.start", TOKEN)]


def test_a_malformed_persisted_envelope_is_reported_and_still_makes_no_sdk_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = _reports(monkeypatch)
    stub = _StubAthenaClient(response=_ok_response(QUERY_ID))
    result = _client(stub).start_table_attempt(_request("not-a-canonical-envelope"))
    assert isinstance(result, AcceptanceAmbiguous)
    assert result.reason is ProviderErrorClass.UNKNOWN
    assert stub.calls == []
    assert [(operation, token) for _error, operation, token in reported] == [
        ("table_attempt.start", TOKEN)
    ]


def test_a_transport_timeout_and_an_accepted_start_report_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reported = _reports(monkeypatch)
    _client(
        _StubAthenaClient(error=ReadTimeoutError(endpoint_url="https://athena.fixture"))
    ).start_table_attempt(_request())
    _client(_StubAthenaClient(response=_ok_response(QUERY_ID))).start_table_attempt(_request())
    assert reported == []
