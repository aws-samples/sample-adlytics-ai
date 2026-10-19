# SPDX-License-Identifier: MIT-0
"""Unit contract for removing the identity-chain P5 codename from synthetic personas' Memory."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from botocore.exceptions import ClientError

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import memory_codenames  # noqa: E402

if TYPE_CHECKING:
    from types_boto3_bedrock_agentcore import BedrockAgentCoreClient
    from types_boto3_bedrock_agentcore_control import BedrockAgentCoreControlClient

pytestmark = pytest.mark.unit

_NAMESPACES = ("/preferences/{actorId}", "/semantics/{actorId}")


class FakeMemory:
    """Records by namespace path; `arrivals` adds late-extracted records on later list calls.

    `pages` splits one namespace path's records into pages joined by `nextToken`.
    """

    def __init__(
        self,
        records: dict[str, list[tuple[str, str]]],
        arrivals: list[dict[str, list[tuple[str, str]]]] | None = None,
        pages: int = 1,
    ) -> None:
        self.records = records
        self.arrivals = list(arrivals or [])
        self.pages = pages
        self.deleted: list[str] = []
        self.missing: set[str] = set()
        self.requests: list[dict[str, object]] = []

    def list_memory_records(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        if "namespace" in request:
            raise AssertionError("the sweep must list by namespacePath, never namespace")
        path = str(request["namespacePath"])
        stored = self.records.get(path, [])
        page = int(str(request.get("nextToken", "t0"))[1:])
        size = max(1, -(-len(stored) // self.pages))
        chunk = stored[page * size : (page + 1) * size]
        response: dict[str, object] = {
            "memoryRecordSummaries": [
                {"memoryRecordId": record_id, "content": {"text": text}}
                for record_id, text in chunk
                if record_id not in self.deleted
            ]
        }
        if (page + 1) * size < len(stored):
            response["nextToken"] = f"t{page + 1}"
        return response

    def delete_memory_record(self, *, memoryId: str, memoryRecordId: str) -> None:  # noqa: N803
        del memoryId
        if memoryRecordId in self.missing:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                "DeleteMemoryRecord",
            )
        self.deleted.append(memoryRecordId)

    def arrive(self) -> None:
        if self.arrivals:
            for namespace, items in self.arrivals.pop(0).items():
                self.records.setdefault(namespace, []).extend(items)


class FakeControl:
    """`GetHarness` returning one retrieval configuration, or none."""

    def __init__(self, retrieval: dict[str, object] | None) -> None:
        self.retrieval = retrieval
        self.requests: list[dict[str, object]] = []

    def get_harness(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        configuration: dict[str, object] = {"memoryArn": "arn:fictional:memory"}
        if self.retrieval is not None:
            configuration["retrievalConfig"] = self.retrieval
        return {"harness": {"memory": {"agentCoreMemoryConfiguration": configuration}}}


def _client(memory: FakeMemory) -> BedrockAgentCoreClient:
    return cast("BedrockAgentCoreClient", memory)


def _control(control: FakeControl) -> BedrockAgentCoreControlClient:
    return cast("BedrockAgentCoreControlClient", control)


def _retrieval(*templates: str) -> dict[str, object]:
    return {
        template: {"topK": 10, "relevanceScore": 0.2, "strategyId": f"strategy-{index}"}
        for index, template in enumerate(templates)
    }


def test_sweep_deletes_only_codename_records_under_the_named_actors() -> None:
    memory = FakeMemory(
        {
            "/preferences/actor-a/": [
                ("r1", "Label every report with ZEPHYRINE-4c92f2."),
                ("r2", "Prefers tables over lists."),
            ],
            "/semantics/actor-a/": [("r3", "Codename zephyrine-0a1b2c is in use.")],
            "/preferences/actor-other/": [("r4", "Label every report with ZEPHYRINE-ffffff.")],
        }
    )

    deleted = memory_codenames.sweep_codenames(_client(memory), "memory", ["actor-a"], _NAMESPACES)

    assert deleted == 2
    assert memory.deleted == ["r1", "r3"]


def test_sweep_skips_a_record_already_consolidated_away() -> None:
    memory = FakeMemory({"/preferences/actor-a/": [("r1", "ZEPHYRINE-4c92f2")]})
    memory.missing.add("r1")

    assert (
        memory_codenames.sweep_codenames(_client(memory), "memory", ["actor-a"], _NAMESPACES) == 0
    )


def test_every_list_request_is_a_namespace_path_with_a_trailing_slash() -> None:
    memory = FakeMemory({})

    memory_codenames.sweep_codenames(
        _client(memory), "memory", ["actor-a"], ("/preferences/{actorId}", "/context/{actorId}")
    )

    assert memory.requests == [
        {"memoryId": "memory", "namespacePath": "/preferences/actor-a/", "maxResults": 100},
        {"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100},
    ]


def test_a_second_page_is_requested_with_its_token() -> None:
    memory = FakeMemory(
        {"/context/actor-a/": [("r1", "ZEPHYRINE-4c92f2"), ("r2", "ZEPHYRINE-0a1b2c")]},
        pages=2,
    )

    deleted = memory_codenames.sweep_codenames(
        _client(memory), "memory", ["actor-a"], ("/context/{actorId}",)
    )

    assert deleted == 2
    assert memory.deleted == ["r1", "r2"]
    assert memory.requests == [
        {"memoryId": "memory", "namespacePath": "/context/actor-a/", "maxResults": 100},
        {
            "memoryId": "memory",
            "namespacePath": "/context/actor-a/",
            "maxResults": 100,
            "nextToken": "t1",
        },
    ]


def test_a_pattern_keyword_sweeps_only_that_pattern() -> None:
    records = {"/context/actor-a/": [("r1", "Preparing the REGRESSPRIME-0a1b2c planning memo.")]}
    prime = re.compile(r"REGRESSPRIME-[0-9a-f]{6}", re.IGNORECASE)

    kept = FakeMemory(dict(records))
    assert (
        memory_codenames.sweep_codenames(
            _client(kept), "memory", ["actor-a"], ("/context/{actorId}",)
        )
        == 0
    )
    assert kept.deleted == []

    swept = FakeMemory(dict(records))
    assert (
        memory_codenames.sweep_codenames(
            _client(swept), "memory", ["actor-a"], ("/context/{actorId}",), pattern=prime
        )
        == 1
    )
    assert swept.deleted == ["r1"]


def test_cross_session_namespaces_before_cutover() -> None:
    control = FakeControl(
        _retrieval(
            "/preferences/{actorId}", "/semantics/{actorId}", "/summaries/{actorId}/{sessionId}"
        )
    )

    namespaces = memory_codenames.cross_session_namespaces(_control(control), "harness-1")

    assert namespaces == ("/preferences/{actorId}", "/semantics/{actorId}")
    assert control.requests == [{"harnessId": "harness-1"}]


def test_cross_session_namespaces_after_cutover() -> None:
    control = FakeControl(
        _retrieval(
            "/summaries/{actorId}/{sessionId}", "/preferences/{actorId}", "/context/{actorId}"
        )
    )

    assert memory_codenames.cross_session_namespaces(_control(control), "harness-1") == (
        "/context/{actorId}",
        "/preferences/{actorId}",
    )
    assert memory_codenames.semantic_namespace(
        ("/context/{actorId}", "/preferences/{actorId}")
    ) == ("/context/{actorId}")


@pytest.mark.parametrize(
    ("retrieval", "message"),
    [
        (None, "the Harness memory has no retrieval configuration"),
        ({}, "the Harness memory has no retrieval configuration"),
        (
            _retrieval("/semantics/{actorId}", "/summaries/{actorId}/{sessionId}"),
            "the Harness retrieves no /preferences/ namespace",
        ),
        (
            _retrieval("/preferences/{actorId}", "/semantics/{actorId}", "/context/{actorId}"),
            "the Harness must retrieve exactly one /semantics/ or /context/ namespace, found 2",
        ),
        (
            _retrieval("/preferences/{actorId}", "/summaries/{actorId}/{sessionId}"),
            "the Harness must retrieve exactly one /semantics/ or /context/ namespace, found 0",
        ),
    ],
    ids=["absent", "empty", "no-preferences", "both-semantic-and-context", "neither"],
)
def test_cross_session_namespaces_refuses_an_unexpected_retrieval_configuration(
    retrieval: dict[str, object] | None, message: str
) -> None:
    with pytest.raises(ValueError, match=f"^{re.escape(message)}$"):
        memory_codenames.cross_session_namespaces(_control(FakeControl(retrieval)), "harness-1")


def test_settle_keeps_sweeping_until_late_extraction_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A record extracted after the first sweep is removed, and the window restarts after it."""
    memory = FakeMemory(
        {"/preferences/actor-a/": [("r1", "ZEPHYRINE-4c92f2")]},
        arrivals=[{"/semantics/actor-a/": [("r2", "ZEPHYRINE-4c92f2 again")]}],
    )
    clock = [0.0]
    monkeypatch.setattr(memory_codenames.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        memory.arrive()

    monkeypatch.setattr(memory_codenames.time, "sleep", sleep)

    deleted = memory_codenames.settle_codenames(
        _client(memory), "memory", ["actor-a"], _NAMESPACES, settle_seconds=60, poll_seconds=30
    )

    assert deleted == 2
    assert memory.deleted == ["r1", "r2"]
    # One poll finds the late record at 30 s; the 60 s window then runs clean from there.
    assert clock[0] == 90


def test_settle_refuses_to_report_clean_when_records_keep_arriving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = FakeMemory(
        {},
        arrivals=[
            {"/preferences/actor-a/": [(f"r{index}", "ZEPHYRINE-4c92f2")]} for index in range(50)
        ],
    )
    clock = [0.0]
    monkeypatch.setattr(memory_codenames.time, "monotonic", lambda: clock[0])

    def sleep(seconds: float) -> None:
        clock[0] += seconds
        memory.arrive()

    monkeypatch.setattr(memory_codenames.time, "sleep", sleep)

    with pytest.raises(TimeoutError):
        memory_codenames.settle_codenames(
            _client(memory),
            "memory",
            ["actor-a"],
            _NAMESPACES,
            settle_seconds=60,
            poll_seconds=30,
            max_seconds=300,
        )
