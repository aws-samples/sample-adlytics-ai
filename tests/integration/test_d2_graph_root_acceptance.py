# SPDX-License-Identifier: MIT-0
"""The D2 GRAPH-path runtime acceptance: one deployed twenty-row root to finalization and serving.

Deploy-gated and opt-in. It runs only in D2's live-acceptance step, after the integrated dev
deploy, and never in a merge gate. It writes no pointer, body or graph item itself: it lands one
reserved probe delivery through the real `land_fixtures` child, exactly as the GRAPH
rolling-freshness control lands its single root, so the deployed ingestion admits the root, the
Promoter grants it, and the deployed Baker fans the twenty roster rows out and finalizes them.
It then strongly reads back the run, its nodes, every row's CURRENT pointer and the grant's
serving binding, and `d2_graph_acceptance.d2_root_verdict` decides (finalized grant-bearing
pointers, serving generation, and the presentation routes' own serving decision). The same run's
Baker `task_summary` lines and `artifact.write` spans are then read, and
`d2_task_timing_verdict` requires every row's complete task, write phase and tail included, to
fit its generated budget (merge-gate round 6, F2); both verdicts must pass.

Opt in with `ADL_D2_GRAPH_ACCEPTANCE_ARGS`, the path of a JSON list holding the control's own
argv (`--profile --environment dev --region --expected-account --data-stack --bucket
--graph-store-stack --manifest --acceptance-execution-id --evidence-dir`). The control's
`parse_inputs` validates it before any AWS call (dev only, a nondefault profile, a 12-digit
account, a new evidence directory under `validation/evidence/`, an unused execution id). Only the
manifest's `single_root` entry is launched. The same fail-closed Baker guard as the traced render
runs before any read: `ADL_TEST_ENV=dev`, `ADL_RUNTIME_BAKE_EXPECTED_ACCOUNT` equal to the
control's account and STS, and `ADL_RUNTIME_BAKE_FUNCTION=adlytics-dev-PresetArtifactBaker` in
that account. The receipt `d2-graph-root-acceptance-v1.json` is written to the evidence directory
whether the root passes or fails.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import boto3
import pytest
from botocore.exceptions import BotoCoreError, ClientError
from d2_graph_acceptance import D2_ROSTER_ROWS, d2_root_verdict, live_task_timing_verdict
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
from domain.operations.execution_graph_release import INGESTION_RELEASE_DOMAIN
from domain.operations.generated_execution_graphs import BAKE_TASKS
from domain.presentation.preset_artifacts import PresetArtifactPage, PresetArtifactVariant

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
from validation import run_graph_rolling_freshness_control as control  # noqa: E402

pytestmark = [pytest.mark.integration, pytest.mark.covers("ADR-070")]

RECEIPT_NAME = "d2-graph-root-acceptance-v1.json"


@dataclass(frozen=True, slots=True)
class _Target:
    """The validated control inputs, the bound session and the Baker's pointer table."""

    inputs: control.Inputs
    session: boto3.Session
    pointer_table: str


@pytest.fixture(scope="module")
def target() -> _Target:
    """Validate the control argv and the Baker guard before any read, or skip until opted in."""
    raw = os.environ.get("ADL_D2_GRAPH_ACCEPTANCE_ARGS", "").strip()
    if not raw:
        pytest.skip("set ADL_D2_GRAPH_ACCEPTANCE_ARGS in D2's live-acceptance step to opt in")
    try:
        inputs = control.parse_inputs(cast("list[str]", json.loads(pathlib.Path(raw).read_text())))
    except (OSError, json.JSONDecodeError, control.ControlError) as error:
        pytest.fail(f"refused before any AWS call: {type(error).__name__}: {error}")
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
    variables = dict(configuration.get("Environment", {}).get("Variables", {}))
    with pytest.MonkeyPatch.context() as patch:
        for name, value in variables.items():
            patch.setenv(name, value)
        pointer_table = preset_artifact_bake_settings().pointer_table
    return _Target(inputs, session, pointer_table)


def _roster() -> tuple[str, ...]:
    return tuple(f"{row['page']}#{row['variant']}" for row in BAKE_TASKS["roster"])


def test_one_twenty_row_root_finalizes_and_serves_through_graph(target: _Target) -> None:
    """Launch the single root, wait for its terminal state, then read back and decide."""
    inputs, session = target.inputs, target.session
    roster = _roster()
    assert len(roster) == D2_ROSTER_ROWS, f"the deployed contract roster has {len(roster)} rows"
    inputs.evidence_dir.mkdir(parents=True)
    document: dict[str, object] = {
        "schema_version": "d2-graph-root-acceptance-v1",
        "acceptance_execution_id": inputs.acceptance_execution_id,
        "started_at": datetime.now(UTC).isoformat(),
        "disposition": "NOT_PROVEN",
    }
    try:
        _manifest_sha, entries = control.load_manifest(inputs.manifest)
        single = [entry for entry in entries if entry.roster == "single_root"]
        planned = control.dry_plan(inputs, single, control.run_child)
        control.reserve(inputs, planned, control.run_child)
        table = resolve_graph_table(session, inputs.environment, inputs.graph_store_stack)
        client = session.client("dynamodb")
        reads = control.BotoControlReads(client, table, INGESTION_RELEASE_DOMAIN)
        collection = control.Collection()
        (run_id,) = control.launch(inputs, planned, control.run_child, collection)
        document["run_id"] = run_id
        document["terminal"] = control._terminal(reads, [run_id], control.Clock())  # pyright: ignore[reportPrivateUsage]
        store = DynamoGraphReleaseStore(table, client)
        run = store.read_run(run_id)
        assert run is not None, "the launched run has no RUN item"
        pointers = DynamoPresetArtifactPointerStore(target.pointer_table, client)
        current = {
            key: pointers.current(
                PresetArtifactPage(key.split("#", 1)[0]),
                PresetArtifactVariant(key.split("#", 1)[1]),
            )
            for key in roster
        }
        reader = DynamoPresentationGraphReader(table, client)
        nodes = store.read_nodes(run_id)
        verdict = d2_root_verdict(roster, run, nodes, current, reader.serving_binding)
        document["verdict"] = verdict
        timing = live_task_timing_verdict(
            session,
            inputs.environment,
            started_at=datetime.fromisoformat(str(document["started_at"])),
            accepted=(roster, run, nodes),
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
            json.dumps(cast("dict[str, Any]", document), indent=2, sort_keys=True, default=str)
            + "\n"
        )
