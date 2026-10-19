# SPDX-License-Identifier: MIT-0
"""The measurement Lambda entry point, run out of the built bundle against real AWS.

Every other test of this handler imports it from `src/`, which cannot fail the way a deployment
fails. This one runs the code that would actually ship: a subprocess whose working directory is
`build/src`, which is the tree that becomes `/var/task`. So it proves the things only a packaged
artefact can get wrong — that `contracts/` reached the bundle, that `contracts_root()` resolves in
the deployed layout rather than only the repository one, and that module-scope resolution completes
without the repository around it.

**What it does not prove is the layer.** `make build-lambdas` installs dependencies as
aarch64-manylinux2014 wheels with compiled extensions, so they cannot be imported on a macOS
host at all; the subprocess resolves them from the development environment instead. The layer's
completeness is checked by the build's own architecture verification, and is genuinely proven only
by invoking the deployed function — which needs AgentStack, and is Task 4's to claim.

The event shape is the Gateway Lambda target contract (verified 2026-08-19): a flat
map of the tool's `inputSchema` properties, with everything else in `client_context.custom` and the
tool name arriving prefixed. Faking it by hand is the point — a fixture built from what
`GatewayInvocation` happens to read would agree with the code rather than with the transport.

**The caller is verified against a key made here (ADR-077 §7).** Every tool verifies the bearer
the Gateway propagates before it reads the call, and only the token service can sign with the real
KMS key. The driver therefore gives the bundled handler the real `CallerVerifier` over a key
generated in this test, and presents a token signed by it in the propagated-header channel.
Everything after the caller check runs against the deployed data plane unchanged.
"""

from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from datetime import date
from typing import cast
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from domain.shared_kernel.values import MONETARY_DECIMAL_PLACES, DateRange

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-006", "G-36")]

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BUNDLE = ROOT / "build" / "src"

TARGET_NAME = "measurement"
TOOL = "measurement_metric_by_grain"
PREFIXED = f"{TARGET_NAME}___{TOOL}"
"""Three underscores, as Gateway sends it. Stripping this is the handler's job, not Gateway's."""

WEEK = DateRange(date(2026, 8, 11), date(2026, 8, 11))
"""One exact V2 CURRENT day; transport behavior is the concern here."""

MARKETPLACE_NAME = "AMAZON.COM"

MESSAGE_ID = "mcp-message-under-test"
"""The fixed AgentCore target-local support reference; never browser correlation."""

CALLER_ENV = {
    "ADL_TOKEN_ISSUER": "https://issuer-under-test.execute-api.us-east-1.amazonaws.com/v1",
    "ADL_TOKEN_SIGNING_KEY_ARN": (
        "arn:aws:kms:us-east-1:111122223333:key/1234abcd-12ab-34cd-56ef-1234567890ab"
    ),
    "ADL_GATEWAY_TOKEN_AUDIENCE": "adlytics-test-gateway",
    "ADL_AGENT_CLIENT_ID": "adlytics-test-agent",
}
"""Fictional caller configuration: the verifier is real, the issuer and key are this test's."""

CALLER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

_DRIVER = """
import json, pathlib, sys

from adapters.config import tool_caller_settings
from adapters.inbound.tool_handlers import measurement_entrypoint
from adapters.inbound.tool_handlers.caller import CallerVerifier


class _PublicKey:
    # Stands in for KMS GetPublicKey only: the verification itself is the shipped code.
    def __init__(self, der):
        self._der = der

    def public_key_der(self):
        return self._der


class _ClientContext:
    def __init__(self, custom):
        self.custom = custom


class _Context:
    # The attributes Powertools reads off a Lambda context. Absent ones raise inside the
    # logger decorator, which would fail the test for a reason unrelated to the handler.
    function_name = "adlytics-measurement"
    function_version = "$LATEST"
    invoked_function_arn = "arn:aws:lambda:us-east-1:000000000000:function:adlytics-measurement"
    memory_limit_in_mb = 1024
    aws_request_id = "00000000-0000-4000-8000-000000000000"

    def __init__(self, custom):
        self.client_context = _ClientContext(custom)

    def get_remaining_time_in_millis(self):
        return 300000


event = json.loads(sys.argv[1])
custom = json.loads(sys.argv[2])
out = pathlib.Path(sys.argv[3])
der = bytes.fromhex(sys.argv[4])

measurement_entrypoint.CALLER = CallerVerifier(tool_caller_settings(), _PublicKey(der))
answer = measurement_entrypoint.handler(event, _Context(custom))
out.write_text(
    json.dumps(
        {
            # Where the module was imported from, so "ran out of the bundle" is asserted
            # rather than assumed. The venv holds an importable copy of the same package.
            "loaded_from": measurement_entrypoint.__file__,
            "answer": answer,
        }
    )
)
"""


@dataclasses.dataclass(frozen=True, slots=True)
class Driver:
    """A bundle and the environment to run it in, which always travel together."""

    bundle: pathlib.Path
    env: dict[str, str]

    def invoke(  # noqa: PLR0913 — one keyword per transport field a test varies
        self,
        tmp_path: pathlib.Path,
        *,
        event: dict[str, object],
        tool_name: str = PREFIXED,
        bearer: bool = True,
    ) -> dict[str, object]:
        """Run one invocation inside the bundle and return what the handler produced.

        The answer goes to a file rather than stdout because the handler logs JSON and flushes EMF
        metrics there, and parsing a stream carrying three kinds of JSON would fail for reasons
        that have nothing to do with the answer. `bearer=False` omits the propagated headers, as
        a Gateway with no header propagation would.
        """
        custom: dict[str, object] = {
            "bedrockAgentCoreToolName": tool_name,
            "bedrockAgentCoreMcpMessageId": MESSAGE_ID,
            "bedrockAgentCoreAwsRequestId": "00000000-0000-4000-8000-000000000000",
            "bedrockAgentCoreGatewayId": "gw-under-test",
            "bedrockAgentCoreTargetId": "tgt-under-test",
            "bedrockAgentCoreMessageVersion": "1.0",
        }
        if bearer:
            custom["bedrockAgentCorePropagatedHeaders"] = {
                "Authorization": f"Bearer {_caller_token()}"
            }
        der = CALLER_KEY.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        out = tmp_path / "answer.json"
        # A-SUBPROCESS-001: fixed interpreter and argv; see docs/security-dispositions.md
        # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
        completed = subprocess.run(  # noqa: S603 — fixed argv, no shell, values are JSON literals
            [
                sys.executable,
                "-c",
                _DRIVER,
                json.dumps(event),
                json.dumps(custom),
                str(out),
                der.hex(),
            ],
            cwd=self.bundle,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if completed.returncode != 0:
            pytest.fail(f"the bundled handler exited {completed.returncode}\n{completed.stderr}")
        return json.loads(out.read_text())


def _caller_token() -> str:
    """Sign a Gateway-audience token as the token service would, with this test's key."""
    now = int(time.time())
    client = CALLER_ENV["ADL_AGENT_CLIENT_ID"]
    claims = {
        "iss": CALLER_ENV["ADL_TOKEN_ISSUER"],
        "sub": "5fdbe1d5-7901-4fee-9d1d-518103789c94",
        "aud": CALLER_ENV["ADL_GATEWAY_TOKEN_AUDIENCE"],
        "client_id": client,
        "act": {"sub": client},
        "iat": now,
        "exp": now + 600,
        "jti": uuid.uuid4().hex,
    }
    key_id = CALLER_ENV["ADL_TOKEN_SIGNING_KEY_ARN"].rsplit("/", 1)[-1]
    return jwt.encode(claims, key=CALLER_KEY, algorithm="RS256", headers={"kid": key_id})


@pytest.fixture(scope="module")
def driver(deployed_env: dict[str, str]) -> Driver:
    """Point a driver at the built tree, or skip naming how to produce one.

    The environment is the one this function would really hold, and it names no secret at all.
    `deployed_env` carries `ADL_COGNITO_CLIENT_SECRET_ID` because the auth handlers need it;
    removing it here is an assertion rather than tidying. A tool function must start without being
    given a credential it is not permitted to read, and leaving the key in would let a handler that
    still called `settings()` pass by fetching it with the test's own credentials.

    `PYTHONPATH` is cleared so nothing but the working directory and the interpreter's own
    site-packages can satisfy an import, which is what makes `loaded_from` meaningful.
    """
    if not (BUNDLE / "contracts" / "shared" / "metric-dictionary.yaml").is_file():
        pytest.skip("no built bundle; run `make build-lambdas` first")

    env = {**os.environ, **deployed_env, **CALLER_ENV}
    env.pop("ADL_COGNITO_CLIENT_SECRET_ID", None)
    env.pop("ADL_COGNITO_CLIENT_SECRET", None)
    env.pop("PYTHONPATH", None)
    # An unset namespace makes the Powertools flush warn, which is noise rather than a finding.
    env.setdefault("POWERTOOLS_METRICS_NAMESPACE", "Adlytics")
    return Driver(bundle=BUNDLE, env=env)


@pytest.fixture(scope="module")
def invoked(
    driver: Driver,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, object]:
    """Ask the target question once, through the bundled handler.

    Module-scoped: this is a real Athena query costing real scanned bytes, and every assertion
    below reads the same answer.
    """
    return driver.invoke(
        tmp_path_factory.mktemp("target"),
        event={
            "metric_reference": "Retail revenue (shipped)",
            "grains": ["product"],
            "operation": "BREAKDOWN",
            "period_start_candidate": WEEK.start.isoformat(),
            "period_end_candidate": WEEK.end.isoformat(),
            "entity_references": {"marketplace": MARKETPLACE_NAME},
            "currency_mode": "NATIVE",
        },
    )


@pytest.fixture(scope="module")
def answer(invoked: dict[str, object]) -> dict[str, object]:
    return _body(invoked)


class TestItRunsOutOfTheBundle:
    def test_the_module_was_imported_from_the_built_tree(self, invoked: dict[str, object]) -> None:
        """Otherwise this whole file silently tests `src/` twice.

        The development environment holds an importable copy of the same package, so without this
        the working directory could be ignored and every assertion would still pass.
        """
        loaded_from = str(invoked["loaded_from"])
        assert loaded_from.startswith(str(BUNDLE))

    def test_it_started_without_being_given_any_client_secret(
        self, answer: dict[str, object]
    ) -> None:
        """The least-privilege config path, proven by the container starting at all.

        Module-scope resolution runs before the handler is reachable, so an answer coming back at
        all means configuration resolved with neither `ADL_COGNITO_CLIENT_SECRET` nor
        `ADL_COGNITO_CLIENT_SECRET_ID` present. A handler still calling `settings()` would have
        failed at import demanding one of them.
        """
        assert answer.get("refused") is not True


class TestTheAnswerSurvivesTheTransport:
    def test_it_is_json_the_gateway_can_parse(self, invoked: dict[str, object]) -> None:
        # Already parsed by the time it is here; asserting the shape it must have arrived in.
        assert isinstance(invoked["answer"], dict)

    def test_figures_arrive_as_strings_at_their_computed_scale(
        self, answer: dict[str, object]
    ) -> None:
        """The reason `to_json_safe` renders `Decimal` as a string rather than a float.

        Through `float`, 88.5000 stops stating four decimal places and money loses a cent. The
        envelope's premise is that the tool already rounded and scaled, so the scale is part of the
        answer and has to survive encoding.
        """
        rows = _rows(answer)
        assert rows, "the loaded window returned nothing, so nothing here is being asserted"
        for row in rows:
            figure = row["value"]
            assert isinstance(figure, str)
            assert len(figure.partition(".")[2]) == MONETARY_DECIMAL_PLACES

    def test_it_states_its_units_and_its_grain(self, answer: dict[str, object]) -> None:
        data = _section(answer, "data")
        units = data["units"]
        assert isinstance(units, dict)
        assert units["kind"] == "currency"
        assert units["currency_code"] == "USD"
        grain = data["grain"]
        assert isinstance(grain, dict)
        assert grain["dimensions"] == ["amazon_asin"]

    def test_the_metadata_a_user_must_see_is_present(self, answer: dict[str, object]) -> None:
        """Coverage, freshness and suppression are not optional in the envelope."""
        metadata = _section(answer, "metadata")
        assert metadata["row_count"] == len(_rows(answer))
        assert metadata["freshness_status"]
        assert metadata["coverage_start"]
        assert metadata["coverage_end"]
        assert isinstance(metadata["suppression"], dict)
        grounding = metadata["grounding"]
        assert isinstance(grounding, dict)
        assert grounding["renderings"]

    def test_the_provenance_field_reaches_the_caller_populated(
        self, answer: dict[str, object]
    ) -> None:
        """G-37 across the wire, not only in the domain.

        `contract_ids` used to arrive as `[]` on every answer, which reads as no contract having
        been involved. The value itself is asserted by `test_measurement_end_to_end`; what matters
        here is that the bundled handler's encoding does not drop it.
        """
        assert _section(answer, "metadata")["contract_ids"]

    def test_enums_arrive_unwrapped(self, answer: dict[str, object]) -> None:
        # `fresh`, not `FreshnessStatus.FRESH`, which is not something a model should have to read.
        assert "FreshnessStatus." not in str(_section(answer, "metadata")["freshness_status"])

    def test_the_narrative_reaches_the_caller(self, answer: dict[str, object]) -> None:
        narrative = answer["narrative"]
        assert isinstance(narrative, list)
        assert narrative


class TestTheGatewayMessageReferenceIsNotCorrelation:
    def test_a_refusal_uses_a_distinct_response_correlation(
        self,
        driver: Driver,
        tmp_path: pathlib.Path,
    ) -> None:
        """The target-local AgentCore reference must not be relabelled as browser correlation."""
        refusal = driver.invoke(
            tmp_path,
            event={
                "metric_reference": "Retail revenue (shipped)",
                "grains": ["product"],
            },
        )
        answer = _body(refusal)
        correlation_id = answer["correlation_id"]
        assert isinstance(correlation_id, str)
        assert correlation_id != MESSAGE_ID
        assert str(UUID(correlation_id)) == correlation_id
        assert _first_grounding_text(answer) == answer["reason"]


class TestAClassifiedErrorIsARefusalRatherThanAFailure:
    def test_a_missing_candidate_period_is_refused_without_crashing(
        self,
        driver: Driver,
        tmp_path: pathlib.Path,
    ) -> None:
        """A raise escaping here would present to Gateway as a broken tool.

        The distinction matters to the model: a refusal it can relay and act on, against an
        invocation error it can only retry.
        """
        refusal = driver.invoke(
            tmp_path,
            event={
                "metric_reference": "Retail revenue (shipped)",
                "grains": ["product"],
            },
        )
        answer = _body(refusal)
        assert answer["refused"] is True
        reason = str(answer["reason"])
        assert reason.strip()
        assert "ADLY-" not in reason
        assert _first_grounding_text(answer) == answer["reason"]

    def test_an_unknown_tool_name_is_refused_naming_the_real_ones(
        self,
        driver: Driver,
        tmp_path: pathlib.Path,
    ) -> None:
        """A Gateway target pointed at the wrong function is a registration mistake.

        Refusing with the available names turns a silent routing failure into one that says what
        was expected.
        """
        refusal = driver.invoke(
            tmp_path,
            event={
                "metric_reference": "Retail revenue (shipped)",
                "grains": ["product"],
            },
            tool_name=f"{TARGET_NAME}___not_a_tool",
        )
        answer = _body(refusal)
        assert answer["refused"] is True
        assert "not_a_tool" not in str(answer["reason"])
        assert TOOL in str(answer["reason"])
        assert _first_grounding_text(answer) == answer["reason"]

    def test_an_underspecified_question_is_refused_rather_than_answered(
        self,
        driver: Driver,
        tmp_path: pathlib.Path,
    ) -> None:
        """No marketplace means no population and no currency.

        ADR-024 reaching a caller through the real transport, and the refusal arrives before Athena
        is touched — a refusal that had already queried would have cost money to say no.
        """
        refusal = driver.invoke(
            tmp_path,
            event={
                "metric_reference": "Retail revenue (shipped)",
                "grains": ["product"],
                "period_start_candidate": WEEK.start.isoformat(),
                "period_end_candidate": WEEK.end.isoformat(),
            },
        )
        answer = _body(refusal)
        assert answer["refused"] is True
        assert "currency" in str(answer["reason"])
        assert _first_grounding_text(answer) == answer["reason"]


class TestTheCallerIsVerifiedFirst:
    @pytest.mark.covers("ADR-077")
    def test_a_call_without_a_propagated_bearer_is_refused_before_any_query(
        self,
        driver: Driver,
        tmp_path: pathlib.Path,
    ) -> None:
        """The same complete question `invoked` answers, so only the missing bearer refuses it.

        The caller code is the gate's own: every later refusal carries a different one, so this
        code means the bundled handler stopped before reading the call or querying Athena.
        """
        refusal = driver.invoke(
            tmp_path,
            event={
                "metric_reference": "Retail revenue (shipped)",
                "grains": ["product"],
                "operation": "BREAKDOWN",
                "period_start_candidate": WEEK.start.isoformat(),
                "period_end_candidate": WEEK.end.isoformat(),
                "entity_references": {"marketplace": MARKETPLACE_NAME},
                "currency_mode": "NATIVE",
            },
            bearer=False,
        )
        answer = _body(refusal)
        assert answer["refused"] is True
        assert answer["code"] == "ADLY-CALLER-0001"
        assert _first_grounding_text(answer) == answer["reason"]


def _body(result: dict[str, object]) -> dict[str, object]:
    """Read the handler's own return value out of what the driver recorded."""
    return _mapping(result["answer"])


def _mapping(value: object) -> dict[str, object]:
    """Narrow one JSON object, keeping its value type declared rather than unknown.

    The cast is what `isinstance` alone cannot give: it narrows to `dict` but leaves both parameters
    unknown, and every read downstream is then untyped.
    """
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def _section(answer: dict[str, object], name: str) -> dict[str, object]:
    return _mapping(answer[name])


def _first_grounding_text(answer: dict[str, object]) -> object:
    grounding = _section(_section(answer, "metadata"), "grounding")
    renderings = grounding["renderings"]
    assert isinstance(renderings, list)
    return _mapping(cast("list[object]", renderings)[0])["text"]


def _rows(answer: dict[str, object]) -> list[dict[str, object]]:
    values = _section(answer, "data")["values"]
    assert isinstance(values, list)
    return [_mapping(row) for row in cast("list[object]", values)]
