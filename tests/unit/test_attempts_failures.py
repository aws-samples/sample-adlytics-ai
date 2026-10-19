# SPDX-License-Identifier: MIT-0
"""Attempt-stream failure-queue disposition: exact refusals, each with a paired positive control."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from adapters.jobs.operational_status import attempts_failures
from adapters.jobs.operational_status.attempts_failures import (
    EVIDENCE_EXISTS,
    EXPECTED_COUNT_MISMATCH,
    MALFORMED_FAILURE_RECORD,
    STREAM_FAILURE_QUEUE_STACKS,
    STREAM_FAILURE_QUEUE_SUFFIXES,
    DrainRefusedError,
    drain,
    main,
    queue_status,
)

pytestmark = [pytest.mark.unit]

URL = "https://sqs.example/000000000000/adlytics-dev-operational-status-attempts-failures"


def _failure_body(shard: str, start: str) -> str:
    # The documented stream on-failure record shape, trimmed to the fields drain reads.
    return json.dumps(
        {
            "requestContext": {"condition": "RetryAttemptsExhausted"},
            "timestamp": "2026-10-09T19:00:00.000Z",
            "DDBStreamBatchInfo": {
                "shardId": shard,
                "startSequenceNumber": start,
                "endSequenceNumber": start,
                "batchSize": 2,
            },
        }
    )


class _StubSqs:
    """Visible messages, in-flight holds, and an ordered call log."""

    def __init__(self, bodies: list[str]) -> None:
        self.visible = {f"m{index}": body for index, body in enumerate(bodies)}
        self.held: dict[str, str] = {}
        self.calls: list[str] = []

    def get_queue_attributes(self, **_kwargs: Any) -> dict[str, Any]:
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": str(len(self.visible)),
                "ApproximateNumberOfMessagesNotVisible": str(len(self.held)),
                "ApproximateNumberOfMessagesDelayed": "0",
            }
        }

    def receive_message(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("receive")
        taken = list(self.visible)[: kwargs["MaxNumberOfMessages"]]
        messages: list[dict[str, str]] = []
        for message_id in taken:
            self.held[message_id] = self.visible.pop(message_id)
            messages.append(
                {
                    "MessageId": message_id,
                    "ReceiptHandle": message_id,
                    "Body": self.held[message_id],
                }
            )
        return {"Messages": messages}

    def change_message_visibility(self, **kwargs: Any) -> None:
        self.calls.append("release")
        handle = kwargs["ReceiptHandle"]
        self.visible[handle] = self.held.pop(handle)

    def delete_message(self, **kwargs: Any) -> None:
        self.calls.append("delete")
        del self.held[kwargs["ReceiptHandle"]]


def test_status_reads_the_three_counts() -> None:
    stub = _StubSqs([_failure_body("s1", "1")])

    assert queue_status(stub, URL) == {"visible": 1, "in_flight": 0, "delayed": 0}
    assert stub.calls == []


def test_drain_records_every_failure_then_deletes_exactly_the_expected_count(
    tmp_path: Path,
) -> None:
    stub = _StubSqs([_failure_body(f"s{index}", str(index)) for index in range(12)])
    evidence = tmp_path / "drain.json"

    result = drain(stub, URL, expected_count=12, evidence=evidence)

    assert result["drained"] == 12
    assert result["after"] == {"visible": 0, "in_flight": 0, "delayed": 0}
    written = json.loads(evidence.read_text())
    assert sorted(item["batch"]["shardId"] for item in written["records"]) == sorted(
        f"s{index}" for index in range(12)
    )
    assert {item["condition"] for item in written["records"]} == {"RetryAttemptsExhausted"}
    assert stub.calls.count("delete") == 12
    assert stub.calls.index("delete") > max(
        index for index, call in enumerate(stub.calls) if call == "receive"
    )


def test_drain_refuses_a_count_that_differs_and_deletes_nothing(tmp_path: Path) -> None:
    assert (
        drain(
            _StubSqs([_failure_body("s", "1")]),
            URL,
            expected_count=1,
            evidence=tmp_path / "ok.json",
        )["drained"]
        == 1
    )
    stub = _StubSqs([_failure_body("s1", "1"), _failure_body("s2", "2")])

    with pytest.raises(DrainRefusedError) as refused:
        drain(stub, URL, expected_count=1, evidence=tmp_path / "drain.json")

    assert refused.value.reason == EXPECTED_COUNT_MISMATCH
    assert stub.calls == []
    assert not (tmp_path / "drain.json").exists()


def test_drain_refuses_a_non_failure_record_releases_it_and_deletes_nothing(tmp_path: Path) -> None:
    assert (
        drain(
            _StubSqs([_failure_body("s", "1")]),
            URL,
            expected_count=1,
            evidence=tmp_path / "ok.json",
        )["drained"]
        == 1
    )
    stub = _StubSqs([_failure_body("s1", "1"), json.dumps({"unexpected": True})])

    with pytest.raises(DrainRefusedError) as refused:
        drain(stub, URL, expected_count=2, evidence=tmp_path / "drain.json")

    assert refused.value.reason == MALFORMED_FAILURE_RECORD
    assert "delete" not in stub.calls
    assert stub.calls.count("release") == 2
    assert len(stub.visible) == 2
    assert not (tmp_path / "drain.json").exists()


def test_drain_refuses_to_overwrite_evidence(tmp_path: Path) -> None:
    assert (
        drain(
            _StubSqs([_failure_body("s", "1")]),
            URL,
            expected_count=1,
            evidence=tmp_path / "ok.json",
        )["drained"]
        == 1
    )
    evidence = tmp_path / "drain.json"
    evidence.write_text("{}")
    stub = _StubSqs([_failure_body("s1", "1")])

    with pytest.raises(DrainRefusedError) as refused:
        drain(stub, URL, expected_count=1, evidence=evidence)

    assert refused.value.reason == EVIDENCE_EXISTS
    assert stub.calls == []
    assert evidence.read_text() == "{}"


def test_cli_refuses_the_default_profile_and_a_drain_without_its_count(
    capsys: pytest.CaptureFixture[str],
) -> None:
    common = [
        "--consumer",
        "attempts",
        "--region",
        "r",
        "--expected-account",
        "000000000000",
        "--env",
        "dev",
    ]

    assert main(["status", "--profile", "default", *common]) == 2
    assert main(["drain", "--profile", "operator", *common]) == 2

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line.removeprefix("RESULT "))["refused"] for line in lines] == [
        "an explicit nondefault profile is required",
        "drain needs --expected-count >= 1 and --evidence",
    ]


CONSUMERS = (
    "attempts",
    "graph-projector",
    "stage1-publications",
    "agent-quality-audit",
    "insight-precompute",
    "publisher-enqueue",
)


def test_the_consumer_set_is_closed_and_every_consumer_has_a_stack() -> None:
    assert tuple(STREAM_FAILURE_QUEUE_SUFFIXES) == CONSUMERS
    assert tuple(STREAM_FAILURE_QUEUE_STACKS) == CONSUMERS


_CHOICES = ", ".join(f"'{consumer}'" for consumer in CONSUMERS)


@pytest.mark.parametrize(
    "case",
    [
        ([], "the following arguments are required: --consumer"),
        (
            ["--consumer", "not-a-consumer"],
            f"argument --consumer: invalid choice: 'not-a-consumer' (choose from {_CHOICES})",
        ),
    ],
)
def test_cli_refuses_a_missing_or_unknown_consumer_before_any_aws_call(
    case: tuple[list[str], str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, refusal = case

    def no_session(**_kwargs: Any) -> None:
        raise AssertionError("no AWS call may precede the consumer check")

    monkeypatch.setattr(attempts_failures.boto3, "Session", no_session)
    common = ["--profile", "operator", "--region", "r", "--expected-account", "0", "--env", "dev"]

    with pytest.raises(SystemExit) as exited:
        main(["status", *argv, *common])

    assert exited.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    # The usage line names the interpreter-chosen program; the refusal after it is exact.
    assert captured.err.endswith("\n")
    assert captured.err.splitlines()[-1].partition(": error: ")[2] == refusal


class _StubCloudFormation:
    def __init__(self, stack: str, names: list[str]) -> None:
        self.stack = stack
        self.names = names

    def get_paginator(self, _name: str) -> _StubCloudFormation:
        return self

    def paginate(self, *, StackName: str) -> list[dict[str, Any]]:  # noqa: N803 -- boto3 kwarg
        assert StackName == self.stack
        prefix = "https://sqs.example/000000000000/"
        return [
            {
                "StackResourceSummaries": [
                    {"ResourceType": "AWS::SQS::Queue", "PhysicalResourceId": prefix + name}
                    for name in self.names
                ]
            }
        ]


class _StubSession:
    def __init__(self, cloudformation: _StubCloudFormation) -> None:
        self.cloudformation = cloudformation

    def client(self, _name: str) -> _StubCloudFormation:
        return self.cloudformation


class _StubSts:
    def get_caller_identity(self) -> dict[str, str]:
        return {"Account": "000000000000"}


class _StubBoundaries:
    """A boto3 session whose identity, CloudFormation and SQS clients are stubs."""

    def __init__(self, cloudformation: _StubCloudFormation, sqs: _StubSqs) -> None:
        self.clients: dict[str, object] = {
            "sts": _StubSts(),
            "cloudformation": cloudformation,
            "sqs": sqs,
        }
        self.requested: list[str] = []

    def client(self, name: str) -> object:
        self.requested.append(name)
        return self.clients[name]


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_cli_status_selects_the_named_consumers_stack_and_queue(
    consumer: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    every = [f"adlytics-dev{suffix}" for suffix in STREAM_FAILURE_QUEUE_SUFFIXES.values()]
    session = _StubBoundaries(
        _StubCloudFormation(f"{STREAM_FAILURE_QUEUE_STACKS[consumer]}-dev", every),
        _StubSqs([_failure_body("s1", "1")]),
    )
    opened: list[dict[str, Any]] = []

    def open_session(**kwargs: Any) -> _StubBoundaries:
        opened.append(kwargs)
        return session

    monkeypatch.setattr(attempts_failures.boto3, "Session", open_session)

    code = main(
        [
            "status",
            "--consumer",
            consumer,
            "--profile",
            "operator",
            "--region",
            "r",
            "--expected-account",
            "000000000000",
            "--env",
            "dev",
        ]
    )

    assert code == 0
    assert opened == [{"profile_name": "operator", "region_name": "r"}]
    assert session.requested == ["sts", "cloudformation", "sqs"]
    line = capsys.readouterr().out
    assert line.startswith("RESULT ")
    assert json.loads(line.removeprefix("RESULT ")) == {
        "command": "status",
        "delayed": 0,
        "in_flight": 0,
        "queue_url": "https://sqs.example/000000000000/adlytics-dev"
        + STREAM_FAILURE_QUEUE_SUFFIXES[consumer],
        "visible": 1,
    }


@pytest.mark.parametrize("consumer", CONSUMERS)
def test_each_consumer_resolves_exactly_its_own_queue_in_its_own_stack(consumer: str) -> None:
    every = [f"adlytics-dev{suffix}" for suffix in STREAM_FAILURE_QUEUE_SUFFIXES.values()]
    session = _StubSession(
        _StubCloudFormation(f"{STREAM_FAILURE_QUEUE_STACKS[consumer]}-dev", every)
    )

    url = attempts_failures.resolve_queue_url(session, consumer, "dev")

    assert url.endswith(f"/adlytics-dev{STREAM_FAILURE_QUEUE_SUFFIXES[consumer]}")
