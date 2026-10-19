# SPDX-License-Identifier: MIT-0
"""The D2 over-bound Audiences acceptance: month B finalized through GRAPH, served as pending.

Merge-gate finding M2, owner decision 1b: an over-bound month shows the typed unavailable state,
never a stale month labelled current. Deploy-gated and opt-in. It runs only in D2's
live-acceptance step, after the integrated dev deploy and after the twenty-row root acceptance,
and never in a merge gate. It writes no pointer, body or graph item itself.

Deferred (merge-gate round 6, decision D): no tracked producer can land the month-B input
through the real `land_fixtures` child yet, so this leg is not in D2's live-acceptance plan and
is not claimed live. `validation/README.md` TD-D2-OVERBOUND-LIVE records what is proven offline,
the timing constraint and the follow-up.

The sequence is the prep design's "A body, then B `audiences_body_over_bound`, served as
`window_unavailable`":

1. Strongly read month A: the Audiences CURRENT pointer and its body, a full Audiences body.
2. Land one reserved delivery through the real `land_fixtures` child (dry plan, reserve,
   `--twin-only`), exactly as the GRAPH rolling-freshness control lands its single root. The
   delivery is the deterministic over-bound fixture: a newer profile month B whose profile and
   campaign rows exceed `audiences_row_budget(1)`. The deployed ingestion admits the root, the
   Promoter grants it, and the deployed Baker renders month B, writes the typed marker and
   finalizes.
3. Strongly read back the run, its nodes, the finalized pointer, its body and the grant's serving
   binding, then POST the actual Audiences route through the deployed API for
   `last_complete_month`.
4. `d2_graph_acceptance.audiences_over_bound_verdict` decides, and
   `d2_task_timing_verdict` requires the same run's Audiences task, its marker write included,
   to fit the row's generated budget (merge-gate round 6, F2).

Opt in with `ADL_D2_AUDIENCES_OVER_BOUND_ARGS`, the path of a JSON object holding
`control_argv` (the GRAPH control's own argv, validated by its `parse_inputs` before any AWS
call) and `over_bound_source`, which must be the one entry the tracked
`validation/fixtures/d2-audiences-over-bound-v1.json` declares (any other text refuses before
any AWS call). The validation-only producer of that probe-owned month-B delivery, above the
declared row floor, is the TD-D2-OVERBOUND-LIVE follow-up; it is not in the generated fixture
corpus and normal dev landing never lands it. The API session comes from `ADL_BASE_URL`,
`ADL_REFRESH` and `ADL_CSRF`, as `validation/probe_presentation_hydration.py` takes it; the
values are never printed or recorded. The same fail-closed Baker guard as the traced render
runs before any read. The receipt `d2-audiences-over-bound-acceptance-v1.json` is written to
the evidence directory whether the acceptance passes or fails, with no credential value in it.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from d2_graph_acceptance import (
    AUDIENCES_ROW,
    OverBoundReadBack,
    audiences_over_bound_verdict,
    declared_over_bound_source,
    live_task_timing_verdict,
)
from runtime_bake_target import (
    BAKER_FUNCTION,
    REFUSE_CALLER,
    identity_refusal,
    target_refusal,
)

from adapters.config import preset_artifact_bake_settings
from adapters.jobs.execution_graph.resolution import resolve_graph_table
from adapters.outbound.dynamodb.execution_graph_release import DynamoGraphReleaseStore
from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https
from adapters.outbound.s3.preset_artifacts import S3PresetArtifactBodyStore
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactPointer,
    PresetArtifactVariant,
)

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from validation import run_graph_rolling_freshness_control as control  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-070")]

RECEIPT_NAME = "d2-audiences-over-bound-acceptance-v1.json"
_LABEL = "audiences-over-bound"
AUDIENCES_PATH = "/api/presentation/audiences"
"""The route `presentation_handlers.handler.AUDIENCES_PATH` serves (importing the handler
resolves the full API configuration at import, which this test does not carry)."""
_MEDIA_TYPE = "application/vnd.adlytics.presentation-v2+json"
_TIMEOUT_SECONDS = 60
_HTTP_OK = 200


@dataclass(frozen=True, slots=True)
class _Target:
    """The validated inputs, the bound session, the Baker's stores and the API origin."""

    inputs: control.Inputs
    source: str
    session: boto3.Session
    pointer_table: str
    body_bucket: str
    base_url: str


def _guarded_session(inputs: control.Inputs) -> tuple[boto3.Session, dict[str, str]]:
    """Run the F2 Baker guard and return the session and the Baker's environment."""
    refusal = target_refusal(os.environ)
    if refusal is not None:
        pytest.fail(f"refused before any AWS call: {refusal}")
    expected = os.environ["ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT"]
    if expected != inputs.expected_account:
        pytest.fail("refused before any AWS call: the Baker and control accounts differ")
    session = boto3.Session(profile_name=inputs.profile, region_name=inputs.region)
    try:
        caller = session.client("sts").get_caller_identity()["Account"]
        if caller != expected:
            pytest.fail(f"refused before any read: {REFUSE_CALLER}")
        configuration = session.client("lambda").get_function_configuration(
            FunctionName=BAKER_FUNCTION
        )
    except (BotoCoreError, ClientError) as exc:  # opted in: an unreadable target refuses
        pytest.fail(f"the dev target could not be verified: {type(exc).__name__}")
    refusal = identity_refusal(expected, caller, configuration.get("FunctionArn", ""))
    if refusal is not None:
        pytest.fail(f"refused before any read: {refusal}")
    return session, dict(configuration.get("Environment", {}).get("Variables", {}))


@pytest.fixture(scope="module")
def target() -> _Target:
    """Validate the inputs, the API origin and the Baker guard before any read, or skip."""
    raw = os.environ.get("ADL_D2_AUDIENCES_OVER_BOUND_ARGS", "").strip()
    if not raw:
        pytest.skip("set ADL_D2_AUDIENCES_OVER_BOUND_ARGS in D2's live-acceptance step to opt in")
    try:
        document = cast("dict[str, Any]", json.loads(pathlib.Path(raw).read_text()))
        inputs = control.parse_inputs(cast("list[str]", document["control_argv"]))
        source = declared_over_bound_source(str(document["over_bound_source"]))
    except (OSError, KeyError, TypeError, ValueError, control.ControlError) as error:
        pytest.fail(f"refused before any AWS call: {type(error).__name__}: {error}")
    if not all(os.environ.get(name, "").strip() for name in ("ADL_REFRESH", "ADL_CSRF")):
        pytest.fail("refused before any AWS call: ADL_REFRESH and ADL_CSRF are required")
    try:
        base_url = require_https(os.environ.get("ADL_BASE_URL", "").strip().rstrip("/"))
    except InsecureUrlError:
        pytest.fail("refused before any AWS call: ADL_BASE_URL must be an https URL")
    session, variables = _guarded_session(inputs)
    with pytest.MonkeyPatch.context() as patch:
        for name, value in variables.items():
            patch.setenv(name, value)
        settings = preset_artifact_bake_settings()
    return _Target(
        inputs, source, session, settings.pointer_table, settings.artifact_bucket, base_url
    )


def _route(base_url: str) -> tuple[int, dict[str, object]]:
    """Refresh one API session, then POST the actual Audiences route for its one preset."""
    refresh, csrf = os.environ["ADL_REFRESH"], os.environ["ADL_CSRF"]
    with HttpsSession() as http:
        http.trust_env = False
        with http.post(
            require_https(f"{base_url}/api/auth/refresh"),
            data=b"",
            headers={
                "Cookie": f"adl_refresh={refresh}; adl_csrf={csrf}",
                "x-adl-csrf": csrf,
                "Sec-Fetch-Site": "same-origin",
            },
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            if response.status_code != _HTTP_OK:
                pytest.fail(f"the API session refresh was refused ({response.status_code})")
            cookies = dict(
                header.split(";", 1)[0].partition("=")[::2]
                for header in response.raw.headers.getlist("Set-Cookie")
            )
        if "adl_session" not in cookies:
            pytest.fail("the API session refresh issued no session cookie")
        csrf = cookies.get("adl_csrf", csrf)
        body = {
            "session_id": str(uuid4()),
            "temporal_selection": {"kind": "preset", "preset_id": "last_complete_month"},
        }
        with http.post(
            require_https(f"{base_url}{AUDIENCES_PATH}"),
            data=json.dumps(body).encode(),
            headers={
                "Cookie": f"adl_session={cookies['adl_session']}; adl_csrf={csrf}",
                "x-adl-csrf": csrf,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
                "Accept": _MEDIA_TYPE,
            },
            timeout=_TIMEOUT_SECONDS,
            allow_redirects=False,
        ) as response:
            decoded: object = None
            try:
                decoded = cast("object", response.json()) if response.content else None
            except ValueError:
                decoded = None
            if not isinstance(decoded, dict):
                return response.status_code, {}
            return response.status_code, cast("dict[str, object]", decoded)


def _read(
    pointers: DynamoPresetArtifactPointerStore, bodies: S3PresetArtifactBodyStore
) -> tuple[PresetArtifactPointer | None, str | None]:
    """Strongly read the Audiences CURRENT pointer and the body it names."""
    page, variant = AUDIENCES_ROW.split("#", 1)
    pointer = pointers.current(PresetArtifactPage(page), PresetArtifactVariant(variant))
    return pointer, None if pointer is None else bodies.read(pointer).payload_json


def test_an_over_bound_month_finalizes_through_graph_and_serves_as_pending(
    target: _Target,
) -> None:
    """Read month A, launch month B's root, wait, then read back, call the route and decide."""
    inputs, session = target.inputs, target.session
    inputs.evidence_dir.mkdir(parents=True)
    document: dict[str, object] = {
        "schema_version": "d2-audiences-over-bound-acceptance-v1",
        "acceptance_execution_id": inputs.acceptance_execution_id,
        "over_bound_source": target.source,
        "started_at": datetime.now(UTC).isoformat(),
        "disposition": "NOT_PROVEN",
    }
    try:
        client = session.client("dynamodb")
        pointers = DynamoPresetArtifactPointerStore(target.pointer_table, client)
        bodies = S3PresetArtifactBodyStore(target.body_bucket, session.client("s3"))
        before = _read(pointers, bodies)
        document["month_a_revision"] = None if before[0] is None else before[0].revision.value
        entry = control.Entry("single_root", _LABEL, target.source)
        planned = control.dry_plan(inputs, [entry], control.run_child)
        control.reserve(inputs, planned, control.run_child)
        table = resolve_graph_table(session, inputs.environment, inputs.graph_store_stack)
        reads = control.BotoControlReads(client, table, INGESTION_RELEASE_DOMAIN)
        (run_id,) = control.launch(inputs, planned, control.run_child, control.Collection())
        document["run_id"] = run_id
        document["terminal"] = control._terminal(reads, [run_id], control.Clock())  # pyright: ignore[reportPrivateUsage]
        store = DynamoGraphReleaseStore(table, client)
        run = store.read_run(run_id)
        assert run is not None, "the launched run has no RUN item"
        reader = DynamoPresentationGraphReader(table, client)
        nodes = store.read_nodes(run_id)
        verdict = audiences_over_bound_verdict(
            OverBoundReadBack(
                run,
                nodes,
                before,
                _read(pointers, bodies),
                reader.serving_binding,
                _route(target.base_url),
            )
        )
        document["verdict"] = verdict
        timing = live_task_timing_verdict(
            session,
            inputs.environment,
            started_at=datetime.fromisoformat(str(document["started_at"])),
            accepted=((AUDIENCES_ROW,), run, nodes),
        )
        document["timing"] = timing
        failures = [*cast("list[str]", verdict["failures"]), *cast("list[str]", timing["failures"])]
        document["disposition"] = "PASS" if not failures else "FAIL"
        assert failures == [], failures
    except BaseException as error:
        document.setdefault("failure", type(error).__name__)
        raise
    finally:
        document["ended_at"] = datetime.now(UTC).isoformat()
        (inputs.evidence_dir / RECEIPT_NAME).write_text(
            json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"
        )
