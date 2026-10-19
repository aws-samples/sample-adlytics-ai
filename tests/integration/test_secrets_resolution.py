# SPDX-License-Identifier: MIT-0
"""The one AWS SDK call in the repository, against real Secrets Manager.

`adapters.config._fetch_secret` is the only boto3 call under `src/`. Every other
outbound adapter reaches AWS over HTTPS with urllib, so for the current codebase
this module is the whole of ADR-014's "every SDK call needs an integration test"
obligation.

The call is already exercised here transitively: the session `settings` fixture
supplies `ADL_COGNITO_CLIENT_SECRET_ID` and no literal secret, so `load_settings`
resolves the BFF client secret through Secrets Manager before any other test in
this tier runs. What that indirection cannot do is say *why* a run went wrong —
`conftest` catches `InvariantError` and skips, so an unreadable secret reads
exactly like "nothing deployed". These assertions name the failure instead of
letting it hide in a skip.

The failure cases need no fixture resource: a secret name that was never created
is the input, and Secrets Manager's own rejection is the behaviour under test.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from adapters.config import CONFIG_UNRESOLVED, Settings, load_settings
from domain.shared_kernel.errors import ErrorClass, InvariantError

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-012", "ADR-014")]

NEVER_CREATED = "adlytics/no-such-env/auth/bff-client-secret"


def _pointed_at(deployed_env: dict[str, str], secret_id: str) -> dict[str, str]:
    """Point the deployed environment at a different secret, changing nothing else."""
    return {**deployed_env, "ADL_COGNITO_CLIENT_SECRET_ID": secret_id}


class TestTheDeployedSecretResolves:
    def test_the_fetch_produced_a_non_empty_value(self, settings: Settings) -> None:
        """Covers the response shape, not just the call.

        A wrong service name, method name or `SecretString` key would raise inside
        `_fetch_secret` and arrive here as an empty settings skip. Asserting the
        value exists is what distinguishes "read it" from "did not crash".
        """
        assert settings.cognito_client_secret.get_secret_value()

    def test_the_value_is_not_the_identifier_it_was_fetched_by(self, settings: Settings) -> None:
        """A fetch that returned its own argument would satisfy a non-empty check.

        The realistic version of this mistake is returning `response["Name"]` or
        `response["ARN"]`, both of which are present in the same response.
        """
        resolved = settings.cognito_client_secret.get_secret_value()
        assert resolved != settings.cognito_client_secret_id
        assert not resolved.startswith("arn:")

    def test_the_resolved_secret_is_still_masked(self, settings: Settings) -> None:
        """`model_copy` could have replaced the SecretStr with a bare string.

        Only reachable in this tier: the unit path supplies the secret directly and
        never runs the update that could downgrade its type. If it were a plain
        str, logging the settings object would print the client secret.
        """
        assert isinstance(settings.cognito_client_secret, SecretStr)
        assert settings.cognito_client_secret.get_secret_value() not in repr(settings)


class TestAnUnreadableSecret:
    def test_a_name_that_was_never_created_raises_invariant_error(
        self, deployed_env: dict[str, str]
    ) -> None:
        """Not a DependencyError: per ADR-012 unresolved configuration alarms.

        CDK sets every value, so a secret the runtime cannot read means the deploy
        and the runtime disagree rather than that a dependency is having a bad day.
        """
        with pytest.raises(InvariantError) as raised:
            load_settings(_pointed_at(deployed_env, NEVER_CREATED))

        assert raised.value.code == CONFIG_UNRESOLVED
        assert raised.value.error_class is ErrorClass.INVARIANT

    def test_the_failure_names_no_infrastructure_and_leaks_no_secret(
        self, deployed_env: dict[str, str], settings: Settings
    ) -> None:
        """The description can reach a caller, so it carries neither."""
        with pytest.raises(InvariantError) as raised:
            load_settings(_pointed_at(deployed_env, NEVER_CREATED))

        error = raised.value
        assert NEVER_CREATED not in error.description
        assert "arn:" not in error.description
        assert settings.cognito_client_secret.get_secret_value() not in str(error.context)

    def test_the_cause_is_recorded_as_a_type_name_and_not_a_message(
        self, deployed_env: dict[str, str]
    ) -> None:
        """A botocore message carries the account id and the full secret ARN.

        Asserted as a shape rather than a fixed class name: which exception boto3
        raises for an unreadable secret depends on why it was unreadable — absent
        and forbidden are different types — and pinning one would make this test
        assert the reason rather than the redaction.
        """
        with pytest.raises(InvariantError) as raised:
            load_settings(_pointed_at(deployed_env, NEVER_CREATED))

        cause = raised.value.context["cause"]
        assert isinstance(cause, str)
        assert cause.isidentifier(), f"expected a bare exception type name, got {cause!r}"
