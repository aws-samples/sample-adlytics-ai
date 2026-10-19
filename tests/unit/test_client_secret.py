# SPDX-License-Identifier: MIT-0
"""The agent client secret's stored shape and comparison (ADR-077 S4).

Pure halves only: parsing the stored document, the constant-time match, and the refresh gate.
The Secrets Manager call itself is covered against the deployed secret in
tests/integration/test_agent_client_secret.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
from pydantic import SecretStr

from adapters.outbound.secretsmanager.client_secret import (
    CLIENT_SECRET_UNAVAILABLE,
    REFRESH_INTERVAL_SECONDS,
    RefreshGate,
    client_secret_from,
)
from domain.shared_kernel.errors import DependencyError
from ports.outbound.client_secret import ClientSecret

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-077")]

VALUE = "Fictional-Agent-Secret-For-Tests"


class TestTheStoredDocument:
    def test_the_generated_document_yields_its_client_secret(self) -> None:
        secret = client_secret_from(json.dumps({"client_secret": VALUE}))

        assert secret.matches(VALUE)

    @pytest.mark.parametrize(
        ("stored", "cause"),
        [
            (f"not json {VALUE}", "secret_not_json"),
            (json.dumps([VALUE]), "secret_not_object"),
            (json.dumps({"password": VALUE}), "secret_key_absent"),
            (json.dumps({"client_secret": ""}), "secret_key_absent"),
            (json.dumps({"client_secret": 7}), "secret_key_absent"),
        ],
    )
    def test_a_malformed_document_names_the_defect_and_never_the_value(
        self, stored: str, cause: str
    ) -> None:
        client_secret_from(json.dumps({"client_secret": VALUE}))

        with pytest.raises(DependencyError) as caught:
            client_secret_from(stored)

        assert caught.value.code == CLIENT_SECRET_UNAVAILABLE
        assert caught.value.context == {"cause": cause}
        assert caught.value.__cause__ is None
        assert VALUE not in str(caught.value)


@dataclass
class _Clock:
    now: float = 1_000.0

    def __call__(self) -> float:
        return self.now


class TestTheRefreshGate:
    """A flood of wrong secrets under the public client id must not become store reads."""

    def test_the_first_read_is_always_ready(self) -> None:
        assert RefreshGate(REFRESH_INTERVAL_SECONDS, clock=_Clock()).ready()

    def test_a_second_read_waits_the_whole_interval_in_seconds(self) -> None:
        clock = _Clock()
        gate = RefreshGate(REFRESH_INTERVAL_SECONDS, clock=clock)
        gate.record()

        clock.now += REFRESH_INTERVAL_SECONDS - 0.001
        assert not gate.ready()
        clock.now += 0.001
        assert gate.ready()

    def test_readiness_is_only_a_question_so_asking_never_restarts_the_interval(self) -> None:
        clock = _Clock()
        gate = RefreshGate(REFRESH_INTERVAL_SECONDS, clock=clock)
        gate.record()
        for _ in range(100):
            clock.now += 1
            gate.ready()

        assert gate.ready()

    def test_the_interval_is_one_minute(self) -> None:
        assert REFRESH_INTERVAL_SECONDS == 60.0


class TestTheComparison:
    @pytest.mark.parametrize("presented", ["", VALUE[:-1], f"{VALUE} ", VALUE.lower(), "é"])
    def test_only_the_exact_value_matches(self, presented: str) -> None:
        secret = ClientSecret(SecretStr(VALUE))
        assert secret.matches(VALUE)

        assert not secret.matches(presented)

    def test_the_value_never_reaches_repr_or_str(self) -> None:
        secret = ClientSecret(SecretStr(VALUE))

        assert VALUE not in repr(secret)
        assert VALUE not in str(secret)

    def test_an_empty_secret_cannot_be_held(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            ClientSecret(SecretStr(""))
