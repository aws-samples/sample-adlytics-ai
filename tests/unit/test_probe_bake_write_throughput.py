# SPDX-License-Identifier: MIT-0
"""The write-throughput probe drives the Baker's own write path, dev only, and cleans up."""

from __future__ import annotations

import io
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.validate import ParamValidator

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_bake_write_throughput as probe  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-145")]

_ACCOUNT = "000000000000"
_BUCKET = "fictional-artifact-bucket"


_S3_MODEL = boto3.client(
    "s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x"
).meta.service_model


def _validated(operation: str, arguments: dict[str, Any]) -> None:
    """Refuse a request S3's own model rejects, as the real service would (meta-testing)."""
    shape = _S3_MODEL.operation_model(operation).input_shape
    assert shape is not None
    report = ParamValidator().validate(arguments, shape)
    if report.has_errors():
        raise AssertionError(report.generate_report())


class _Backing:
    """One fictional versioned bucket shared by every fake client a session hands out."""

    def __init__(self, *, delay: float, preexisting: bool = False) -> None:
        self.delay = delay
        self.preexisting = preexisting
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, list[str]] = {}
        self.puts: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.fail_deletes = False


def _ignore(*_args: object, **_kw: object) -> None:
    return None


class _FakeS3:
    def __init__(self, backing: _Backing, config: Any = None) -> None:
        self._backing = backing
        self.created_with = config
        self.meta = SimpleNamespace(
            events=SimpleNamespace(register=_ignore),
            config=SimpleNamespace(
                max_pool_connections=10 if config is None else config.max_pool_connections,
                connect_timeout=60,
                read_timeout=60,
                retries={"mode": "legacy"},
                tcp_keepalive=None,
            ),
        )

    def put_object(self, **arguments: Any) -> dict[str, str]:
        _validated("PutObject", arguments)
        backing = self._backing
        with backing.lock:
            backing.in_flight += 1
            backing.peak = max(backing.peak, backing.in_flight)
        started = time.perf_counter()
        time.sleep(backing.delay)
        try:
            with backing.lock:
                key = arguments["Key"]
                exists = key in backing.objects
                backing.objects.setdefault(key, arguments["Body"])
                backing.versions.setdefault(key, []).append(f"v{len(backing.puts)}")
                backing.puts.append({**arguments, "started": started, "ended": time.perf_counter()})
            if exists or backing.preexisting:
                raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
            return {"VersionId": "v"}
        finally:
            with backing.lock:
                backing.in_flight -= 1

    def get_object(self, **arguments: Any) -> dict[str, Any]:
        _validated("GetObject", arguments)
        return {"Body": io.BytesIO(self._backing.objects[arguments["Key"]])}

    def get_paginator(self, name: str) -> Any:
        assert name == "list_object_versions"
        backing = self._backing

        def paginate(**arguments: str) -> list[dict[str, Any]]:
            _validated("ListObjectVersions", dict(arguments))
            prefix = arguments["Prefix"]
            return [
                {
                    "Versions": [
                        {"Key": key, "VersionId": version}
                        for key, versions in backing.versions.items()
                        if key.startswith(prefix)
                        for version in versions
                    ]
                }
            ]

        return SimpleNamespace(paginate=paginate)

    def list_objects_v2(self, **arguments: Any) -> dict[str, int]:
        _validated("ListObjectsV2", arguments)
        prefix = arguments["Prefix"]
        return {
            "KeyCount": min(1, sum(1 for key in self._backing.objects if key.startswith(prefix)))
        }

    def delete_objects(self, **arguments: Any) -> dict[str, Any]:
        _validated("DeleteObjects", arguments)
        backing = self._backing
        if backing.fail_deletes:
            return {"Errors": [{"Key": "x"}]}
        for item in arguments["Delete"]["Objects"]:
            versions = backing.versions.get(item["Key"], [])
            if item["VersionId"] in versions:
                versions.remove(item["VersionId"])
            if not versions:
                backing.versions.pop(item["Key"], None)
                backing.objects.pop(item["Key"], None)
        return {}


def _bucket_parameter(**_kw: object) -> dict[str, dict[str, str]]:
    return {"Parameter": {"Value": _BUCKET}}


class _FakeSession:
    def __init__(self, backing: _Backing, account: str = _ACCOUNT) -> None:
        self.backing = backing
        self.account = account
        self.s3_calls: list[dict[str, Any]] = []
        self.s3_clients: list[_FakeS3] = []

    def factory(self, **_kw: object) -> _FakeSession:
        return self

    def client(self, name: str, **arguments: Any) -> Any:
        if name == "sts":
            return SimpleNamespace(get_caller_identity=lambda: {"Account": self.account})
        if name == "ssm":
            return SimpleNamespace(get_parameter=_bucket_parameter)
        assert name == "s3"
        self.s3_calls.append(arguments)
        client = _FakeS3(self.backing, arguments.get("config"))
        self.s3_clients.append(client)
        return client


def _request(**overrides: Any) -> probe.ProbeRequest:
    fields: dict[str, Any] = {
        "target": probe.ProbeTarget("probe-profile", "dev", "us-east-1", _ACCOUNT),
        "widths": (2, 4),
        "repeats": 1,
        "pairs": 12,
        "body_bytes": 300,
        "custody_bytes": 200,
        "shared_pool": False,
        "output": Path("unused.json"),
    }
    return probe.ProbeRequest(**{**fields, **overrides})


def _run(backing: _Backing, **overrides: Any) -> tuple[dict[str, Any], _FakeSession]:
    session = _FakeSession(backing)
    return probe.run(_request(**overrides), session.factory), session


@pytest.mark.parametrize("environment", ["prod", "beta", "DEV", ""])
def test_every_environment_but_dev_is_refused_before_any_aws_call(environment: str) -> None:
    with pytest.raises(probe.ProbeError, match="only against ENV=dev"):
        probe.arguments(
            [
                "--profile=probe-profile",
                f"--environment={environment}",
                "--region=us-east-1",
                f"--expected-account={_ACCOUNT}",
                "--repeats=1",
                "--pairs=4",
                "--body-bytes=300",
                "--custody-bytes=200",
                "--output=unused.json",
            ]
        )
    # Positive control: dev is admitted.
    assert probe.ProbeTarget("probe-profile", "dev", "us-east-1", _ACCOUNT).environment == "dev"


def test_another_account_is_refused_before_any_write() -> None:
    backing = _Backing(delay=0)
    session = _FakeSession(backing, account="111111111111")
    with pytest.raises(probe.ProbeError, match="differs from the expected account"):
        probe.run(_request(), session.factory)
    assert backing.puts == []


def test_a_run_writes_fresh_pairs_through_the_real_stage_under_the_probe_prefix() -> None:
    backing = _Backing(delay=0.02)
    result, session = _run(backing)

    measured_puts = [put for put in backing.puts if "/measured/" in put["Key"]]
    # Every key the stores named is under the probe-only prefix; none is a page or custody key.
    assert backing.puts
    assert all(put["Key"].startswith(probe.PREFIX_ROOT) for put in backing.puts)
    assert all(put["Bucket"] == _BUCKET for put in backing.puts)
    # The stores' own create-once request: IfNoneMatch, JSON, the stage's exact bytes.
    assert {put["IfNoneMatch"] for put in backing.puts} == {"*"}
    assert {put["ContentType"] for put in backing.puts} == {"application/json"}
    # Body before custody for every pair, then the index shard, parent body and parent custody.
    for run in result["runs"]:
        prefix = f"{result['prefix']}{f'w{run["width"]:03d}-r1'}/measured/"
        mine = [put for put in measured_puts if put["Key"].startswith(prefix)]
        # The first PUT of each key: the replay control writes segment 0's body once more.
        by_key = {put["Key"].removeprefix(prefix): put for put in reversed(mine)}
        for index in range(12):
            body = by_key[
                f"pages/audiences/last_complete_month/{_revision(run, 'pages')}"
                f"/segments/probe/segment-{index:04d}.json"
            ]
            custody = by_key[
                f"custody/audiences/last_complete_month/{_revision(run, 'custody')}"
                f"/segments/probe/segment-{index:04d}.json"
            ]
            assert body["ended"] <= custody["started"]
            assert len(body["Body"]) == 300 and len(custody["Body"]) == 200
        tail = sorted(mine, key=lambda put: put["started"])[-4:-1]
        assert [
            "/index/" in tail[0]["Key"],
            tail[1]["Key"].startswith(prefix + "pages/"),
            tail[2]["Key"].startswith(prefix + "custody/"),
        ] == [True, True, True]
        assert run["statuses"] == {"200": 2 * 12 + 3}
        assert run["sibling_objects"] == 24
        # The stage's own width: as many PUTs in flight as the width, never more.
        assert run["puts_peak_in_flight"] == run["width"]
        assert run["distinct_clients"] == 2
        # Control: the replay path answers 412 then one GET, so fresh 200s are not trivial.
        assert run["replay_control"]["statuses"] == ["PreconditionFailed"]
        assert run["replay_control"]["gets"] == 1
        # The listing saw the run's objects before cleanup and none after.
        assert run["cleanup"]["listed_before"] == 1
        assert run["cleanup"]["versions_before"] > 0
        assert run["cleanup"]["empty"] is True
    assert backing.peak <= max(run["width"] for run in result["runs"])
    # The Baker's clients: two `client("s3")` with no configuration per run, plus the lister.
    assert session.s3_calls == [{}] * (1 + 2 * len(result["runs"]))
    assert result["final_cleanup"]["empty"] is True
    assert backing.objects == {} and backing.versions == {}


def _revision(run: dict[str, Any], namespace: str) -> str:
    keys = [sample[0] for sample in run["samples"] if sample[0].startswith(namespace + "/")]
    return keys[0].split("/")[3]


def test_a_shared_pool_is_one_client_holding_width_connections() -> None:
    session = _FakeSession(_Backing(delay=0))
    shape = probe.RunShape(32, 4, 300, 200, shared_pool=True)
    bodies, custody = probe.baker_clients(session, shape, probe.SlowDownCounter())
    assert bodies is custody
    assert session.s3_calls[0]["config"].max_pool_connections == 32


def test_the_default_clients_carry_the_bakers_default_configuration() -> None:
    session = boto3.Session(
        aws_access_key_id="fictional", aws_secret_access_key="fictional", region_name="us-east-1"
    )
    shape = probe.RunShape(16, 4, 300, 200)
    bodies, custody = probe.baker_clients(session, shape, probe.SlowDownCounter())
    baker_default = session.client("s3")  # what `S3PresetArtifactBodyStore(bucket)` builds
    assert bodies is not custody
    assert probe.client_config(bodies) == probe.client_config(baker_default)
    assert probe.client_config(custody) == probe.client_config(baker_default)
    assert probe.client_config(bodies)["max_pool_connections"] == 10


def _attempt(client: Any, status: int) -> list[Any]:
    """Emit one PutObject attempt's `needs-retry`, as botocore's endpoint does after a response."""
    http = SimpleNamespace(status_code=status, headers={})
    parsed = {"Error": {"Code": "SlowDown"}} if status == 503 else {}
    responses = client.meta.events.emit(
        "needs-retry.s3.PutObject",
        response=(http, parsed),
        endpoint=None,
        operation=_S3_MODEL.operation_model("PutObject"),
        attempts=1,
        caught_exception=None,
        request_dict={"context": {}},
    )
    return [value for _, value in responses if value is not None]


def test_every_503_attempt_is_counted_and_botocore_still_decides_the_retry() -> None:
    session = boto3.Session(
        aws_access_key_id="fictional", aws_secret_access_key="fictional", region_name="us-east-1"
    )
    counter = probe.SlowDownCounter()
    bodies, custody = probe.baker_clients(session, probe.RunShape(1, 1, 300, 200), counter)
    decisions = _attempt(bodies, 503) + _attempt(custody, 503)
    assert counter.count == 2
    # botocore's own handler still answered a retry for each 503; the counter added none.
    assert len(decisions) == 2
    # Control: a 200 attempt counts nothing and is not retried.
    assert _attempt(bodies, 200) == []
    assert counter.count == 2


def test_a_run_whose_keys_already_exist_is_refused_and_still_cleaned() -> None:
    backing = _Backing(delay=0, preexisting=True)
    with pytest.raises(probe.ProbeError, match="were not fresh"):
        _run(backing, widths=(2,))
    assert backing.objects == {} and backing.versions == {}


def test_a_cleanup_that_leaves_an_object_fails_the_run() -> None:
    backing = _Backing(delay=0)
    backing.fail_deletes = True
    with pytest.raises(probe.ProbeError, match="not proven cleaned"):
        _run(backing, widths=(2,))


def test_cleanup_refuses_any_prefix_outside_the_probe_root() -> None:
    store = probe._Store(_FakeS3(_Backing(delay=0)), _BUCKET)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(probe.ProbeError, match="only under its own prefix"):
        store.clean("pages/")


def test_synthetic_bodies_are_canonical_and_exactly_sized() -> None:
    payload = probe.padded_json({"probe": probe.SCHEMA, "segment": 3}, 2_138)
    assert len(payload.encode()) == 2_138
    with pytest.raises(probe.ProbeError):
        probe.padded_json({"probe": probe.SCHEMA}, 10)


def test_the_schedule_alternates_width_order_between_repeats() -> None:
    assert probe.schedule((8, 16), 2) == [(1, 8), (1, 16), (2, 16), (2, 8)]
