# SPDX-License-Identifier: MIT-0
"""Tests for runtime configuration resolution.

No I/O and no AWS: `load_settings` takes an explicit mapping, so every failure
mode is reachable without an environment or a deployed stack.

The two properties worth the most here are that a failure names *every* missing
key rather than the first, and that it never repeats a value — one of those
values is a client secret.
"""

from collections.abc import Iterator
from typing import Protocol

import pytest

from adapters.config import (
    CONFIG_UNRESOLVED,
    ENV_PREFIX,
    Settings,
    TokenServiceSettings,
    agent_quality_processor_settings,
    authorizer_public_client_id,
    evaluation_outbox_dispatcher_settings,
    evaluation_result_ingress_settings,
    evaluation_result_persister_settings,
    evaluation_writer_settings,
    insight_precompute_settings,
    load_settings,
    read_quality_review_settings,
    read_token_service_settings,
    settings_without_client_secret,
)
from domain.identity.cookies import CookieScope
from domain.shared_kernel.errors import ErrorClass, InvariantError

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-012")]

VALID = {
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    # pragma: allowlist nextline secret (A-TEST-LITERALS-001)
    "ADL_COGNITO_CLIENT_SECRET": "the-client-secret",
    # Required with no default, deliberately: a default workgroup would be Athena's
    # `primary`, which has no scan cutoff, so F-14's breaker would be absent exactly
    # when a misconfiguration made it matter.
    "ADL_ATHENA_WORKGROUP": "adlytics-test-interactive",
    "ADL_SEMANTIC_ATTEMPTS_TABLE": "adlytics-test-semantic-attempts",
    "ADL_SEMANTIC_LEASES_TABLE": "adlytics-test-semantic-leases",
    "ADL_SEMANTIC_PUBLICATIONS_TABLE": "adlytics-test-semantic-publications",
    "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
    "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "8",
    "ADL_COGNITO_LOGIN_DOMAIN": "auth.customer.com",
    "ADL_APP_BASE_URL": "https://app.customer.com",
    "ADL_API_BASE_URL": "https://api.customer.com",
    "ADL_COOKIE_SCOPE": "host_only",
    "ADL_REFRESH_COOKIE_MAX_AGE_SECONDS": "2592000",
}


def _with(**overrides: str) -> dict[str, str]:
    """Return the valid environment, with keys replaced or removed when set to ''."""
    merged = {**VALID, **overrides}
    return {key: value for key, value in merged.items() if value != ""}


def _load(**overrides: str) -> Settings:
    return load_settings(_with(**overrides))


def test_insight_precompute_requires_the_preset_artifact_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        "ADL_SEMANTIC_PUBLICATIONS_TABLE": "publications",
        "ADL_SEMANTIC_V2_BUCKET": "semantic",
        "ADL_PRESET_ARTIFACT_BUCKET": "artifacts",
        "ADL_INSIGHT_CACHE_TABLE": "cache",
        "ADL_INSIGHT_PROMPT_REGISTRY_TABLE": "prompts",
        "ADL_INSIGHT_QUICK_MODEL_ID": "quick",
        "ADL_INSIGHT_DEEP_MODEL_ID": "deep",
        "ADL_INSIGHT_GENERATION_TIMEOUT_SECONDS": "4",
        "ADL_INSIGHT_GENERATION_MAX_CONCURRENCY": "8",
        "ADL_INSIGHT_PRECOMPUTE_MAX_REQUESTS": "40",
        "ADL_SEMANTIC_V2_ATHENA_INSIGHT_PRECOMPUTE_WORKGROUP": "insight-precompute",
        "ADL_SEMANTIC_V2_CURRENT_DATABASE": "current",
        "ADL_ATHENA_RESULT_REUSE_MAX_AGE_MINUTES": "60",
        "ADL_ATHENA_QUERY_BATCH_MAX_CONCURRENCY": "4",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    insight_precompute_settings.cache_clear()
    assert insight_precompute_settings().preset_artifact_bucket == "artifacts"

    monkeypatch.delenv("ADL_PRESET_ARTIFACT_BUCKET")
    insight_precompute_settings.cache_clear()
    with pytest.raises(InvariantError) as caught:
        insight_precompute_settings()
    assert caught.value.code == CONFIG_UNRESOLVED
    assert caught.value.context == {"missing": ["preset_artifact_bucket"]}
    insight_precompute_settings.cache_clear()


class TestValidConfiguration:
    def test_loads_when_every_key_is_present(self) -> None:
        assert _load().environment == "dev"

    def test_refresh_max_age_is_coerced_to_an_int(self) -> None:
        assert _load().refresh_cookie_max_age_seconds == 2_592_000

    def test_operational_status_provisioned_reads_the_rendered_flag(self) -> None:
        # ADR-115 decision 8: infra renders String(provisioned); absent means not provisioned.
        flag = "ADL_OPERATIONAL_STATUS_PROVISIONED"
        assert _load().operational_status_provisioned is False
        assert _load(**{flag: "false"}).operational_status_provisioned is False
        assert _load(**{flag: "true"}).operational_status_provisioned is True

    def test_settings_are_frozen(self) -> None:
        # Configuration is read, never adjusted. A mutable settings object
        # invites a code path that patches a value for one request.
        with pytest.raises(ValueError, match=r"frozen"):
            _load().environment = "prod"  # type: ignore[misc]


class TestDerivedValues:
    def test_the_issuer_is_derived_from_region_and_pool(self) -> None:
        # Never configured separately. A standalone issuer key could disagree
        # with the pool ID and reject every token.
        assert _load().cognito_issuer == (
            "https://cognito-idp.eu-west-1.amazonaws.com/eu-west-1_example"
        )

    def test_the_jwks_url_hangs_off_the_issuer(self) -> None:
        assert _load().cognito_jwks_url.endswith("/eu-west-1_example/.well-known/jwks.json")

    def test_oauth_endpoints_use_the_login_domain_not_the_issuer(self) -> None:
        # The token endpoint lives on the login domain. Pointing it at the issuer
        # is a plausible mistake that fails only at the first code exchange.
        settings = _load()
        assert settings.cognito_token_url == "https://auth.customer.com/oauth2/token"
        assert settings.cognito_revoke_url == "https://auth.customer.com/oauth2/revoke"
        assert settings.cognito_authorize_url == "https://auth.customer.com/oauth2/authorize"

    def test_runtime_read_timeout_is_the_larger_server_selected_turn_ceiling(self) -> None:
        settings = _load().model_copy(
            update={"quick_chat_timeout_seconds": 240, "deep_dive_timeout_seconds": 180}
        )

        assert settings.harness_read_timeout_seconds == 240

    def test_the_callback_uri_is_derived_from_the_api_origin(self) -> None:
        assert _load().callback_redirect_uri == "https://api.customer.com/api/auth/callback"

    def test_a_trailing_slash_does_not_double_up(self) -> None:
        # Cognito compares redirect_uri as an exact string, so a doubled slash is
        # a rejected login rather than a cosmetic problem.
        settings = _load(ADL_API_BASE_URL="https://api.customer.com/")
        assert settings.callback_redirect_uri == "https://api.customer.com/api/auth/callback"

    def test_the_cookie_policy_is_built_from_the_configured_scope(self) -> None:
        assert _load().cookie_policy.scope is CookieScope.HOST_ONLY


class TestMissingKeys:
    def test_a_missing_key_fails_closed(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_COGNITO_CLIENT_ID="")

    def test_the_failure_alarms(self) -> None:
        # Unresolved configuration is an invariant breach, not a domain answer.
        # CDK sets every value, so a missing one means deploy and runtime disagree.
        with pytest.raises(InvariantError) as caught:
            _load(ADL_COGNITO_CLIENT_ID="")
        assert caught.value.error_class is ErrorClass.INVARIANT
        assert caught.value.alarms is True

    def test_the_missing_key_is_named_with_its_deployed_name(self) -> None:
        with pytest.raises(InvariantError) as caught:
            _load(ADL_COGNITO_CLIENT_ID="")
        assert "ADL_COGNITO_CLIENT_ID" in str(caught.value.context["problems"])

    def test_every_missing_key_is_reported_at_once(self) -> None:
        """The reason pydantic-settings was chosen over a hand-rolled check.

        Fixing one missing variable only to discover the next on the following
        deploy turns a five-minute correction into an afternoon.
        """
        with pytest.raises(InvariantError) as caught:
            _load(ADL_COGNITO_CLIENT_ID="", ADL_COOKIE_SCOPE="", ADL_AWS_REGION="")
        problems = str(caught.value.context["problems"])
        assert "ADL_COGNITO_CLIENT_ID" in problems
        assert "ADL_COOKIE_SCOPE" in problems
        assert "ADL_AWS_REGION" in problems

    def test_nothing_is_missing_silently(self) -> None:
        # Every field is required. If a default is ever added, removing that key
        # stops failing and this notices.
        for key in VALID:
            with pytest.raises(InvariantError):
                _load(**{key: ""})


class TestThePageContextTableIsToleratedUntilItsConsumers:
    """Only page-aware handlers require this table, so shared Settings permits absence."""

    def test_an_unrelated_function_starts_without_it(self) -> None:
        assert _load().page_context_table == ""

    def test_an_explicit_table_name_is_preserved(self) -> None:
        assert _load(ADL_PAGE_CONTEXT_TABLE="page-context").page_context_table == "page-context"


class TestTheRawBucketIsToleratedUntilTheIngestionBoundary:
    """Only ingestion requires the raw bucket, so shared Settings permits its absence."""

    def test_a_function_that_never_reads_a_delivery_starts_without_it(self) -> None:
        assert _load().raw_bucket == ""


class TestValuesAreNeverEchoed:
    def test_a_rejected_value_does_not_appear_in_the_error(self) -> None:
        """Pydantic reports the offending input by default; we strip it.

        The sentinel stands in for the client secret, which is the value that
        must never reach a log line. If the raw pydantic message ever gets
        forwarded, this fails.
        """
        with pytest.raises(InvariantError) as caught:
            _load(ADL_REFRESH_COOKIE_MAX_AGE_SECONDS="SENTINEL-DO-NOT-LOG")
        rendered = f"{caught.value} {caught.value.context} {caught.value.description}"
        assert "SENTINEL-DO-NOT-LOG" not in rendered

    def test_the_user_visible_description_stays_generic(self) -> None:
        with pytest.raises(InvariantError) as caught:
            _load(ADL_COGNITO_CLIENT_ID="")
        assert ENV_PREFIX not in caught.value.user_visible_description

    def test_the_client_secret_is_masked(self) -> None:
        settings = _load()
        assert "the-client-secret" not in repr(settings)
        assert "the-client-secret" not in str(settings.cognito_client_secret)
        assert settings.cognito_client_secret.get_secret_value() == "the-client-secret"


class TestUrlsMustBeDeployedValues:
    @pytest.mark.parametrize("key", ["ADL_APP_BASE_URL", "ADL_API_BASE_URL"])
    def test_localhost_is_rejected(self, key: str) -> None:
        """The defect this team has hit repeatedly on real deployments.

        A localhost URL cannot be caught later: the service starts, serves, and
        redirects users somewhere that does not exist.
        """
        with pytest.raises(InvariantError):
            _load(**{key: "https://localhost:3000"})

    @pytest.mark.parametrize("key", ["ADL_APP_BASE_URL", "ADL_API_BASE_URL"])
    def test_loopback_by_address_is_also_rejected(self, key: str) -> None:
        with pytest.raises(InvariantError):
            _load(**{key: "https://127.0.0.1:3000"})

    @pytest.mark.parametrize("key", ["ADL_APP_BASE_URL", "ADL_API_BASE_URL"])
    def test_plain_http_is_rejected(self, key: str) -> None:
        # These cookies are Secure, so an http origin cannot carry a session.
        with pytest.raises(InvariantError):
            _load(**{key: "http://app.customer.com"})

    @pytest.mark.parametrize("key", ["ADL_APP_BASE_URL", "ADL_API_BASE_URL"])
    def test_a_bare_host_is_rejected(self, key: str) -> None:
        with pytest.raises(InvariantError):
            _load(**{key: "app.customer.com"})


class TestLoginDomain:
    def test_a_scheme_is_rejected(self) -> None:
        # Endpoints are built by string concatenation, so a scheme here produces
        # https://https://auth.customer.com/oauth2/token.
        with pytest.raises(InvariantError):
            _load(ADL_COGNITO_LOGIN_DOMAIN="https://auth.customer.com")

    def test_a_path_is_rejected(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_COGNITO_LOGIN_DOMAIN="auth.customer.com/oauth2")


class TestCookieScope:
    def test_an_unknown_scope_is_rejected(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_COOKIE_SCOPE="whatever")

    def test_parent_scope_without_a_domain_is_rejected(self) -> None:
        # Validated through CookiePolicy, so the domain rule has one home.
        with pytest.raises(InvariantError):
            _load(ADL_COOKIE_SCOPE="parent_domain")

    def test_a_public_suffix_domain_is_rejected(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_COOKIE_SCOPE="parent_domain", ADL_COOKIE_DOMAIN=".cloudfront.net")

    def test_parent_scope_with_a_real_domain_is_accepted(self) -> None:
        settings = _load(ADL_COOKIE_SCOPE="parent_domain", ADL_COOKIE_DOMAIN=".customer.com")
        assert settings.cookie_policy.domain == ".customer.com"


class TestRefreshLifetime:
    def test_zero_is_rejected(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_REFRESH_COOKIE_MAX_AGE_SECONDS="0")

    def test_a_negative_value_is_rejected(self) -> None:
        with pytest.raises(InvariantError):
            _load(ADL_REFRESH_COOKIE_MAX_AGE_SECONDS="-1")


class TestOperatorDirectoryConcurrency:
    """Concurrent Cognito lookups per versions response default to 4 and stay within 1-8."""

    def test_it_defaults_to_four_when_unset(self) -> None:
        assert _load().operator_directory_max_concurrency == 4

    @pytest.mark.parametrize("value", ["0", "9"])
    def test_a_value_outside_one_to_eight_fails_closed(self, value: str) -> None:
        with pytest.raises(InvariantError) as caught:
            _load(ADL_OPERATOR_DIRECTORY_MAX_CONCURRENCY=value)
        assert caught.value.alarms is True
        assert "ADL_OPERATOR_DIRECTORY_MAX_CONCURRENCY" in str(caught.value.context["problems"])


class TestLoadingWithoutTheClientSecret:
    """The least-privilege path a tool function uses instead of `settings()`.

    Asserted through `load_settings` rather than through the
    `settings_without_client_secret` accessor, because that accessor is a cached
    read of the real environment and a unit test that reached it would either fail
    for want of env vars or pick up ambient ones. What the accessor adds over these
    assertions is one line of delegation; that the measurement entry point actually
    calls it is proven by driving that function against a deployed environment.

    None of these tests reach `_fetch_secret`. Each either skips resolution or is
    refused before the fetch, which is what keeps them in the unit tier.
    """

    def test_it_loads_with_neither_secret_key_present(self) -> None:
        """The deployed environment of a function that is not the BFF.

        It is given no secret and no secret ARN, and must still start. Requiring one
        would mean naming a credential it is not permitted to read in order to pass
        validation.
        """
        settings = load_settings(_with(ADL_COGNITO_CLIENT_SECRET=""), resolve_client_secret=False)
        assert settings.environment == "dev"

    def test_the_secret_is_empty_rather_than_fetched(self) -> None:
        """The assertion that proves no Secrets Manager call was made.

        Resolution has exactly two outcomes — it fills the field or it raises. An
        empty field after a successful load therefore means the fetch never ran.
        """
        settings = load_settings(
            _with(ADL_COGNITO_CLIENT_SECRET="", ADL_COGNITO_CLIENT_SECRET_ID="arn:aws:secret"),
            resolve_client_secret=False,
        )
        assert settings.cognito_client_secret.get_secret_value() == ""

    def test_resolution_is_still_required_by_default(self) -> None:
        """The control that makes the two tests above mean something.

        With resolution left on, the same mapping is refused for having no secret.
        So the flag is genuinely what relaxed the requirement, rather than the
        requirement having never been enforced.
        """
        with pytest.raises(InvariantError):
            load_settings(_with(ADL_COGNITO_CLIENT_SECRET=""))

    def test_skipping_the_fetch_does_not_skip_any_other_key(self) -> None:
        """Otherwise this accessor becomes a way to boot a misconfigured container.

        The fetch is the only thing being switched off. A missing workgroup is still
        a deploy that disagrees with its runtime.
        """
        with pytest.raises(InvariantError):
            load_settings(_with(ADL_ATHENA_WORKGROUP=""), resolve_client_secret=False)

    def test_a_directly_supplied_secret_is_carried_through(self) -> None:
        """This skips resolution; it is not a scrubber.

        Worth pinning so nobody later reads the accessor as one and relies on it to
        strip a value it never touches.
        """
        settings = load_settings(_with(), resolve_client_secret=False)
        assert settings.cognito_client_secret.get_secret_value() == "the-client-secret"


class TestTheHarnessArnIsRequiredAtConversationComposition:
    def test_an_unrelated_function_starts_without_it(self) -> None:
        assert _load(ADL_HARNESS_ARN="").harness_arn == ""

    def test_an_explicit_harness_arn_is_preserved(self) -> None:
        harness_arn = "arn:aws:bedrock-agentcore:eu-west-1:111122223333:harness/example"
        assert _load(ADL_HARNESS_ARN=harness_arn).harness_arn == harness_arn


@pytest.mark.covers("ADR-077")
class TestTheRouteExchangeFunctionIsReadByItsDeployedName:
    def test_an_unrelated_function_starts_without_it(self) -> None:
        assert _load().token_route_exchange_function == ""

    def test_the_deployed_arn_is_preserved(self) -> None:
        arn = "arn:aws:lambda:eu-west-1:111122223333:function:adlytics-dev-TokenRouteExchange"
        assert _load(ADL_TOKEN_ROUTE_EXCHANGE_FUNCTION=arn).token_route_exchange_function == arn

    def test_the_removed_freshness_buffer_is_no_longer_a_setting(self) -> None:
        """ADR-077 §2 removed the minimum-lifetime refusal; no dual path may survive it."""
        assert "harness_token_freshness_buffer_seconds" not in Settings.model_fields
        assert not hasattr(Settings, "harness_bearer_minimum_lifetime_seconds")


TOKEN_SERVICE_VALID = {
    "ADL_ENVIRONMENT": "dev",
    "ADL_AWS_REGION": "eu-west-1",
    "ADL_COGNITO_USER_POOL_ID": "eu-west-1_example",
    "ADL_COGNITO_CLIENT_ID": "1example23456789",
    "ADL_TOKEN_ISSUER": "https://abc123.execute-api.eu-west-1.amazonaws.com/v1",
    "ADL_TOKEN_SIGNING_KEY_ID": "1234abcd-12ab-34cd-56ef-1234567890ab",
    "ADL_HARNESS_TOKEN_AUDIENCE": "adlytics-dev-harness",
    "ADL_ROUTE_CLIENT_ID": "adlytics-dev-conversation-route",
    "ADL_HARNESS_TOKEN_LIFETIME_SECONDS": "240",
    "ADL_GATEWAY_TOKEN_AUDIENCE": "adlytics-dev-gateway",
    "ADL_AGENT_CLIENT_ID": "adlytics-dev-agent",
    "ADL_AGENT_CLIENT_SECRET_ID": (
        "arn:aws:secretsmanager:eu-west-1:111122223333:secret:"
        "adlytics/dev/identity/agent-client-secret-AbCdEf"
    ),
}


def _token_service(**overrides: str) -> TokenServiceSettings:
    merged = {**TOKEN_SERVICE_VALID, **overrides}
    return read_token_service_settings({k: v for k, v in merged.items() if v != ""})


@pytest.mark.covers("ADR-077")
class TestTokenServiceSettings:
    def test_every_value_is_read_from_its_deployed_name(self) -> None:
        assert _token_service() == TokenServiceSettings(
            environment="dev",
            aws_region="eu-west-1",
            cognito_user_pool_id="eu-west-1_example",
            cognito_client_id="1example23456789",
            token_issuer="https://abc123.execute-api.eu-west-1.amazonaws.com/v1",
            token_signing_key_id="1234abcd-12ab-34cd-56ef-1234567890ab",
            harness_token_audience="adlytics-dev-harness",
            route_client_id="adlytics-dev-conversation-route",
            harness_token_lifetime_seconds=240,
            gateway_token_audience="adlytics-dev-gateway",
            agent_client_id="adlytics-dev-agent",
            agent_client_secret_id=(
                "arn:aws:secretsmanager:eu-west-1:111122223333:secret:"
                "adlytics/dev/identity/agent-client-secret-AbCdEf"
            ),
        )

    def test_the_agent_exchange_values_are_each_required_and_reported_together(self) -> None:
        with pytest.raises(InvariantError) as caught:
            _token_service(
                ADL_GATEWAY_TOKEN_AUDIENCE="", ADL_AGENT_CLIENT_ID="", ADL_AGENT_CLIENT_SECRET_ID=""
            )

        assert caught.value.context["missing"] == [
            "ADL_AGENT_CLIENT_ID",
            "ADL_AGENT_CLIENT_SECRET_ID",
            "ADL_GATEWAY_TOKEN_AUDIENCE",
        ]

    def test_the_subject_issuer_and_key_set_are_the_routes_own_derivation(self) -> None:
        """The exchange must verify against exactly the issuer the route verifies against."""
        service = _token_service()
        route = _load()

        assert service.cognito_issuer == route.cognito_issuer
        assert service.cognito_jwks_url == route.cognito_jwks_url

    def test_every_missing_value_is_reported_at_once_by_name(self) -> None:
        with pytest.raises(InvariantError) as caught:
            _token_service(ADL_TOKEN_ISSUER="", ADL_ROUTE_CLIENT_ID="")

        assert caught.value.code == CONFIG_UNRESOLVED
        assert caught.value.context["missing"] == ["ADL_ROUTE_CLIENT_ID", "ADL_TOKEN_ISSUER"]

    @pytest.mark.parametrize("lifetime", ["0", "-1", "240.0", "two-hundred", " "])
    def test_a_lifetime_that_is_not_a_positive_whole_second_count_is_refused(
        self, lifetime: str
    ) -> None:
        with pytest.raises(InvariantError) as caught:
            _token_service(ADL_HARNESS_TOKEN_LIFETIME_SECONDS=lifetime)

        missing = caught.value.context["missing"]
        assert missing == ["ADL_HARNESS_TOKEN_LIFETIME_SECONDS (must be a positive integer)"]
        assert lifetime.strip() == "" or lifetime not in str(caught.value.context)

    @pytest.mark.parametrize(
        "issuer",
        [
            "http://abc123.execute-api.eu-west-1.amazonaws.com/v1",
            "https://abc123.execute-api.eu-west-1.amazonaws.com/v1/",
        ],
    )
    def test_an_issuer_that_is_not_bare_https_is_refused_without_echoing_it(
        self, issuer: str
    ) -> None:
        with pytest.raises(InvariantError) as caught:
            _token_service(ADL_TOKEN_ISSUER=issuer)

        assert caught.value.context["missing"] == [
            "ADL_TOKEN_ISSUER (must be https without a trailing slash)"
        ]
        assert issuer not in str(caught.value.context)


_REVIEW_RECORDS = {
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "adlytics-test-agent-quality-audit",
    "ADL_AGENT_QUALITY_FINDINGS_TABLE": "adlytics-test-agent-quality-findings",
}
_REVIEW_EVALUATION = {
    "ADL_EVALUATION_DELIVERIES_TABLE": "adlytics-test-evaluation-deliveries",
    "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "adlytics-test-evaluation-writer-receipts",
    "ADL_EVALUATION_VERDICTS_TABLE": "adlytics-test-evaluation-verdicts",
}


@pytest.mark.covers("ADR-080", "ADR-078")
class TestQualityReviewRecordTables:
    """The one review function reads every record table (ADR-078 §5, ADR-080 §1)."""

    @pytest.fixture(autouse=True)
    def _no_ambient_tables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Settings also reads the process environment, which other test modules populate.
        for name in (
            "AGENT_QUALITY_AUDIT_TABLE",
            "AGENT_QUALITY_FINDINGS_TABLE",
            "EVALUATION_DELIVERIES_TABLE",
            "EVALUATION_WRITER_RECEIPTS_TABLE",
            "EVALUATION_VERDICTS_TABLE",
            "EVALUATION_RESULT_DEADLINE_SECONDS",
            "COGNITO_PUBLIC_CLIENT_ID",
        ):
            monkeypatch.delenv(f"{ENV_PREFIX}{name}", raising=False)

    def test_the_receipts_and_findings_tables_are_always_required(self) -> None:
        with pytest.raises(InvariantError) as caught:
            read_quality_review_settings(_load())
        assert caught.value.code == CONFIG_UNRESOLVED
        assert caught.value.context["problems"] == [
            "ADL_AGENT_QUALITY_AUDIT_TABLE is required by the quality review API",
            "ADL_AGENT_QUALITY_FINDINGS_TABLE is required by the quality review API",
        ]

    def test_without_the_evaluation_plane_the_function_reads_two_tables(self) -> None:
        resolved = read_quality_review_settings(_load(**_REVIEW_RECORDS))
        assert (resolved.receipts_table, resolved.findings_table, resolved.evaluation) == (
            "adlytics-test-agent-quality-audit",
            "adlytics-test-agent-quality-findings",
            None,
        )

    def test_the_evaluation_tables_resolve_together_or_not_at_all(self) -> None:
        with pytest.raises(InvariantError) as caught:
            _load(ADL_EVALUATION_DELIVERIES_TABLE="adlytics-test-evaluation-deliveries")
        assert "configured together" in str(caught.value.context)

    def test_the_evaluation_plane_requires_its_result_deadline(self) -> None:
        """Without a deadline a turn nobody judged would read as pending forever."""
        with pytest.raises(InvariantError) as caught:
            read_quality_review_settings(_load(**_REVIEW_RECORDS, **_REVIEW_EVALUATION))
        assert caught.value.context["problems"] == [
            "ADL_EVALUATION_RESULT_DEADLINE_SECONDS is required by the quality review API"
        ]
        resolved = read_quality_review_settings(
            _load(
                **_REVIEW_RECORDS,
                **_REVIEW_EVALUATION,
                ADL_EVALUATION_RESULT_DEADLINE_SECONDS="3600",
            )
        )
        assert resolved.evaluation is not None
        assert resolved.evaluation.verdicts_table == "adlytics-test-evaluation-verdicts"
        assert resolved.evaluation.result_deadline_seconds == 3600


@pytest.mark.covers("ADR-078")
class TestAuthorizerPublicClient:
    @pytest.fixture(autouse=True)
    def _isolated(self, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
        for name, value in VALID.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv(f"{ENV_PREFIX}COGNITO_PUBLIC_CLIENT_ID", raising=False)
        settings_without_client_secret.cache_clear()
        authorizer_public_client_id.cache_clear()
        yield
        settings_without_client_secret.cache_clear()
        authorizer_public_client_id.cache_clear()

    def test_a_blank_public_client_refuses_to_start(self) -> None:
        with pytest.raises(InvariantError) as caught:
            authorizer_public_client_id()
        assert caught.value.context["problems"] == ["ADL_COGNITO_PUBLIC_CLIENT_ID is required"]

    def test_the_public_client_resolves_from_its_own_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADL_COGNITO_PUBLIC_CLIENT_ID", "2publicexample345678")
        assert authorizer_public_client_id() == "2publicexample345678"


class _Retained(Protocol):
    @property
    def receipt_retention_days(self) -> int: ...


class _CachedResolver(Protocol):
    def __call__(self) -> _Retained: ...

    def cache_clear(self) -> None: ...


_WRITER_JOBS: dict[str, tuple[_CachedResolver, dict[str, str]]] = {
    "processor": (
        agent_quality_processor_settings,
        {
            "ADL_AGENT_QUALITY_AUDIT_TABLE": "adlytics-test-agent-quality-audit",
            "ADL_AGENT_QUALITY_FINDINGS_TABLE": "adlytics-test-agent-quality-findings",
            "ADL_AGENT_QUALITY_AGGREGATE_TABLE": "adlytics-test-agent-quality-aggregate",
        },
    ),
    "dispatcher": (
        evaluation_outbox_dispatcher_settings,
        {
            "ADL_EVALUATION_OUTBOX_TABLE": "adlytics-test-evaluation-outbox",
            "ADL_EVALUATION_DELIVERIES_TABLE": "adlytics-test-evaluation-deliveries",
            "ADL_EVALUATION_QUEUE_URL": "https://sqs.eu-west-1.amazonaws.com/111122223333/q",
            "ADL_EVALUATION_OUTBOX_RETENTION_DAYS": "14",
            "ADL_EVALUATION_RELEASE_PARAMETERS": "/adlytics/test/evaluation/release-v4",
            "ADL_AGENT_QUALITY_AUDIT_TABLE": "adlytics-test-agent-quality-audit",
            "ADL_EVALUATION_DELIVERY_WAIT_SECONDS": "420",
        },
    ),
    "writer": (
        evaluation_writer_settings,
        {
            "ADL_EVALUATION_INPUT_LOG_GROUP": "/adlytics/test/evaluation-input",
            "ADL_EVALUATION_INPUT_SERVICE_NAME": "adlytics-test-evaluation",
            "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "adlytics-test-evaluation-writer-receipts",
        },
    ),
    "linker": (
        evaluation_result_persister_settings,
        {"ADL_EVALUATION_VERDICTS_TABLE": "adlytics-test-evaluation-verdicts"},
    ),
}


@pytest.mark.covers("ADR-081")
class TestReceiptRetention:
    """Every record writer declares the receipt retention, in whole days, with no default."""

    @pytest.fixture(autouse=True)
    def _fresh_job_settings(self) -> Iterator[None]:
        for resolve, _ in _WRITER_JOBS.values():
            resolve.cache_clear()
        yield
        for resolve, _ in _WRITER_JOBS.values():
            resolve.cache_clear()

    @pytest.mark.parametrize("job", sorted(_WRITER_JOBS))
    def test_a_writer_job_resolves_the_declared_retention_in_days(
        self, job: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolve, environment = _WRITER_JOBS[job]
        for name, value in {**environment, "ADL_RECEIPT_RETENTION_DAYS": "90"}.items():
            monkeypatch.setenv(name, value)

        assert resolve().receipt_retention_days == 90

    @pytest.mark.parametrize("raw", ["", "0", "-1", "90.5", "ninety", "\u0669\u0660", "1000000"])
    @pytest.mark.parametrize("job", sorted(_WRITER_JOBS))
    def test_a_writer_job_without_a_positive_whole_retention_refuses_to_start(
        self, job: str, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        resolve, environment = _WRITER_JOBS[job]
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv("ADL_RECEIPT_RETENTION_DAYS", raw)

        with pytest.raises(InvariantError) as caught:
            resolve()

        assert caught.value.code == CONFIG_UNRESOLVED
        assert caught.value.context == {"missing": ["receipt_retention_days"]}

    def test_the_route_settings_leave_retention_to_the_route_that_writes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only the conversation route writes receipts; readers load without the value."""
        monkeypatch.delenv(f"{ENV_PREFIX}RECEIPT_RETENTION_DAYS", raising=False)
        assert _load().receipt_retention_days is None
        assert _load(ADL_RECEIPT_RETENTION_DAYS="90").receipt_retention_days == 90

    @pytest.mark.parametrize("raw", ["0", "-5", "1000000"])
    def test_the_route_settings_refuse_a_retention_outside_whole_positive_days(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(f"{ENV_PREFIX}RECEIPT_RETENTION_DAYS", raising=False)
        with pytest.raises(InvariantError):
            _load(ADL_RECEIPT_RETENTION_DAYS=raw)


_DISPATCHER_ENVIRONMENT = {**_WRITER_JOBS["dispatcher"][1], "ADL_RECEIPT_RETENTION_DAYS": "90"}
_INGRESS_ENVIRONMENT = {
    "ADL_AGENT_QUALITY_AUDIT_TABLE": "adlytics-test-agent-quality-audit",
    "ADL_EVALUATION_WRITER_RECEIPTS_TABLE": "adlytics-test-evaluation-writer-receipts",
    "ADL_EVALUATION_DELIVERIES_TABLE": "adlytics-test-evaluation-deliveries",
    "ADL_EVALUATION_RESULT_PROJECTION_QUEUE_URL": (
        "https://sqs.eu-west-1.amazonaws.com/111122223333/result"
    ),
    "ADL_RELEASED_EVALUATION_RESULT_LOG_GROUPS": "/adlytics/test/evaluation/results-v4",
}


@pytest.mark.covers("ADR-079")
class TestEvaluationGateJobSettings:
    """The dispatcher reads the release records; the ingress admits by delivery receipt."""

    @pytest.fixture(autouse=True)
    def _fresh_job_settings(self) -> Iterator[None]:
        evaluation_outbox_dispatcher_settings.cache_clear()
        evaluation_result_ingress_settings.cache_clear()
        yield
        evaluation_outbox_dispatcher_settings.cache_clear()
        evaluation_result_ingress_settings.cache_clear()

    def test_the_dispatcher_resolves_its_release_parameters_in_order_and_its_wait(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in _DISPATCHER_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setenv(
            "ADL_EVALUATION_RELEASE_PARAMETERS",
            " /adlytics/test/evaluation/release-v4 ,, /adlytics/test/evaluation/release-v5 ",
        )

        settings = evaluation_outbox_dispatcher_settings()

        assert settings.release_parameters == (
            "/adlytics/test/evaluation/release-v4",
            "/adlytics/test/evaluation/release-v5",
        )
        assert settings.audit_table == "adlytics-test-agent-quality-audit"
        # A whole number of seconds after admission, not minutes.
        assert settings.delivery_wait_seconds == 420

    @pytest.mark.parametrize(
        ("override", "missing"),
        [
            (("ADL_EVALUATION_RELEASE_PARAMETERS", " , "), "release_parameters"),
            (("ADL_AGENT_QUALITY_AUDIT_TABLE", ""), "audit_table"),
            (("ADL_EVALUATION_DELIVERY_WAIT_SECONDS", "0"), "delivery_wait_seconds"),
            (("ADL_EVALUATION_DELIVERY_WAIT_SECONDS", "-30"), "delivery_wait_seconds"),
            (("ADL_EVALUATION_DELIVERY_WAIT_SECONDS", "7.5"), "delivery_wait_seconds"),
            (("ADL_EVALUATION_DELIVERY_WAIT_SECONDS", ""), "delivery_wait_seconds"),
        ],
    )
    def test_the_dispatcher_refuses_to_start_without_a_gate_input(
        self, monkeypatch: pytest.MonkeyPatch, override: tuple[str, str], missing: str
    ) -> None:
        for key, value in _DISPATCHER_ENVIRONMENT.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv(*override)

        with pytest.raises(InvariantError) as caught:
            evaluation_outbox_dispatcher_settings()

        assert caught.value.code == CONFIG_UNRESOLVED
        assert caught.value.context == {"missing": [missing]}

    def test_the_ingress_requires_the_deliveries_table_and_no_configuration_ids(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for name, value in _INGRESS_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)
        monkeypatch.delenv("ADL_RELEASED_ONLINE_EVALUATION_CONFIG_IDS", raising=False)

        assert evaluation_result_ingress_settings().deliveries_table == (
            "adlytics-test-evaluation-deliveries"
        )

        evaluation_result_ingress_settings.cache_clear()
        monkeypatch.setenv("ADL_EVALUATION_DELIVERIES_TABLE", "")
        with pytest.raises(InvariantError) as caught:
            evaluation_result_ingress_settings()
        assert caught.value.context == {"missing": ["deliveries_table"]}
