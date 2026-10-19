# SPDX-License-Identifier: MIT-0
"""The traceability probe runner's pure parts: the dry-run gate, the import scan and JUnit.

The runner refuses the real put unless the dry run reports exactly one non-historical delivery
(R2-13, V-24), and it fails a probe on any skipped or missing integration test (R2-14). The
import scan resolves handler closures over a staged tree and counts a changed contract as
touched when a closure module names it or walks its directory (R4-5, R5-6, R5-11, R5-12).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
from typing import cast

import pytest
from dynamodb_reserved_words import guard_reserved_words

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))
from validation import probe_ingestion_traceability_gate as gate  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


def _line(**overrides: object) -> str:
    report: dict[str, object] = {
        "deliveries": 1,
        "historical": False,
        "original_historical": False,
        "body_sha256": "a",
        "original_body_sha256": "a",
        "key": "k",
    }
    report.update(overrides)
    return f"noise\nPROBE_RETRIEVAL {json.dumps(report)}\nmore\n"


def test_v24_the_runner_accepts_exactly_one_non_historical_delivery() -> None:
    assert gate.parse_probe_report(_line())["key"] == "k"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("nothing printed\n", "exactly one is required"),
        (_line() + _line(), "exactly one is required"),
        (_line(deliveries=2), "exactly one delivery"),
        (_line(historical=True), "non-historical"),
        (_line(original_historical=True), "non-historical"),
        (_line(body_sha256="b"), "data bytes differ"),
    ],
)
def test_v24_the_runner_refuses_the_real_put_on_any_other_dry_run(text: str, message: str) -> None:
    with pytest.raises(gate.ProbeError, match=message):
        gate.parse_probe_report(text)


def _staged(tmp_path: pathlib.Path) -> pathlib.Path:
    files = {
        "adapters/__init__.py": "",
        "adapters/inbound/__init__.py": "",
        "adapters/inbound/handler.py": "from . import helper\nfrom domain.ops import rule\n",
        "adapters/inbound/helper.py": "import adapters.outbound.store\n",
        "adapters/outbound/__init__.py": "",
        "adapters/outbound/store.py": "X = 1\n",
        "adapters/outbound/loader.py": (
            'ROOT = "contracts"\n'
            'def walk(root):\n    return list((root / "contracts" / "insights").rglob("*"))\n'
        ),
        "adapters/jobs/__init__.py": "",
        "adapters/jobs/tool.py": "from domain.ops.history import predicate\n",
        "domain/__init__.py": "",
        "domain/ops/__init__.py": "",
        "domain/ops/rule.py": 'PATH = "operations/execution-graphs.yaml"\n',
        "domain/ops/history.py": "def predicate(): ...\n",
        "run.sh": "#!/bin/sh\nexec python -m adapters.inbound.handler\n",
    }
    for name, text in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def test_import_closure_follows_absolute_relative_and_package_imports(
    tmp_path: pathlib.Path,
) -> None:
    staged = _staged(tmp_path)
    closure = {
        path.relative_to(staged).as_posix()
        for path in gate.import_closure(staged, "adapters.inbound.handler")
    }
    assert closure == {
        "adapters/__init__.py",
        "adapters/inbound/__init__.py",
        "adapters/inbound/handler.py",
        "adapters/inbound/helper.py",
        "adapters/outbound/__init__.py",
        "adapters/outbound/store.py",
        "domain/__init__.py",
        "domain/ops/__init__.py",
        "domain/ops/rule.py",
    }


def test_the_scan_is_empty_for_a_change_no_handler_imports(tmp_path: pathlib.Path) -> None:
    staged = _staged(tmp_path)
    changed = ("src/domain/ops/history.py", "src/adapters/jobs/tool.py", "validation/x.py")
    assert gate.scan_intersection(staged, ["adapters.inbound.handler"], changed) == ()


def test_the_scan_counts_a_changed_module_in_the_closure(tmp_path: pathlib.Path) -> None:
    staged = _staged(tmp_path)
    changed = ("src/adapters/outbound/store.py",)
    assert gate.scan_intersection(staged, ["adapters.inbound.handler"], changed) == changed


def test_a_contract_is_touched_by_its_path_literal_or_a_directory_walk(
    tmp_path: pathlib.Path,
) -> None:
    staged = _staged(tmp_path)
    named = ("contracts/operations/execution-graphs.yaml",)
    assert gate.scan_intersection(staged, ["adapters.inbound.handler"], named) == named
    walked = ("contracts/insights/new-insight.yaml",)
    assert gate.scan_intersection(staged, ["adapters.inbound.handler"], walked) == ()
    assert gate.scan_intersection(staged, ["adapters.outbound.loader"], walked) == walked


def test_handlers_are_resolved_or_classified(tmp_path: pathlib.Path) -> None:
    staged = _staged(tmp_path)
    python = gate.classify_handler(
        "Fn", {"Handler": "adapters.inbound.handler.handler", "Runtime": "python3.14"}, staged
    )
    assert python.module == "adapters.inbound.handler"
    script = gate.classify_handler("Web", {"Handler": "run.sh", "Runtime": "python3.14"}, staged)
    assert script.module == "adapters.inbound.handler"
    framework = gate.classify_handler(
        "Cr", {"Handler": "index.handler", "Runtime": "nodejs22.x"}, staged
    )
    assert (framework.module, framework.touched_unresolved) == (None, False)
    unknown = gate.classify_handler(
        "X", {"Handler": "nope.handler", "Runtime": "python3.14"}, staged
    )
    assert unknown.touched_unresolved


def test_asset_comparison_requires_every_recorded_key_deployed() -> None:
    recorded = {"A": {"s3_key": "src.zip"}, "B": {"s3_key": "framework.zip"}}
    assert gate.compare_assets(recorded, recorded, "src.zip")["equal"] is True
    stale = {"A": {"s3_key": "old.zip"}, "B": {"s3_key": "framework.zip"}}
    result = gate.compare_assets(recorded, stale, "src.zip")
    assert (result["equal"], result["mismatches"]) == (False, ["A"])
    assert gate.compare_assets(recorded, {"A": {"s3_key": "src.zip"}}, "src.zip")["missing"] == [
        "B"
    ]


def test_the_junit_check_fails_closed_on_any_skip_error_or_count_mismatch() -> None:
    clean = '<testsuites><testsuite tests="3" skipped="0" errors="0" failures="0"/></testsuites>'
    assert gate.junit_verdict(clean, 3)["tests"] == 3
    for xml, expected in (
        ('<testsuite tests="3" skipped="1" errors="0" failures="0"/>', 3),
        ('<testsuite tests="3" skipped="0" errors="1" failures="0"/>', 3),
        ('<testsuite tests="3" skipped="0" errors="0" failures="1"/>', 3),
        (clean, 4),
    ):
        with pytest.raises(gate.ProbeError):
            gate.junit_verdict(xml, expected)


def test_row_digests_ignore_order_and_see_any_value() -> None:
    rows = [["a", "1"], ["b", "2"]]
    assert gate.rows_digest(rows) == gate.rows_digest(list(reversed(rows)))
    assert gate.rows_digest(rows) != gate.rows_digest([["a", "1"], ["b", "3"]])


def test_function_assets_reads_every_lambda_key() -> None:
    template: dict[str, object] = {
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Handler": "a.b.handler",
                    "Runtime": "python3.14",
                    "Code": {"S3Bucket": "x", "S3Key": "k.zip"},
                },
            },
            "Other": {"Type": "AWS::SQS::Queue", "Properties": {}},
        }
    }
    assert gate.function_assets(template) == {
        "Fn": {"handler": "a.b.handler", "runtime": "python3.14", "s3_key": "k.zip"}
    }


def test_sole_writer_analysis_finds_write_grants_on_the_live_table_only() -> None:
    template: dict[str, object] = {
        "Resources": {
            "Writer": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "Roles": [{"Ref": "WriterRole"}],
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                                "Resource": {"Ref": "SsmParameterValueexecutiongraphtablearnX"},
                            }
                        ]
                    },
                },
            },
            "Reader": {
                "Type": "AWS::IAM::Policy",
                "Properties": {
                    "Roles": [{"Ref": "ReaderRole"}],
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "dynamodb:GetItem",
                                "Resource": "arn:aws:dynamodb:::table/adlytics-dev-execution-graph",
                            },
                            {
                                "Effect": "Allow",
                                "Action": "dynamodb:PutItem",
                                "Resource": "arn:aws:dynamodb:::table/other",
                            },
                        ]
                    },
                },
            },
        }
    }
    found = gate.table_writers(
        template, ["execution-graph-table-arn", "adlytics-dev-execution-graph"]
    )
    assert found == [
        {"policy": "Writer", "roles": [{"Ref": "WriterRole"}], "actions": ["dynamodb:PutItem"]}
    ]


class _CloudFormation:
    def __init__(self, templates: dict[str, dict[str, object]]) -> None:
        self.templates = templates

    def get_paginator(self, _name: str) -> object:
        names = list(self.templates)

        class _Pages:
            @staticmethod
            def paginate() -> list[dict[str, object]]:
                return [{"Stacks": [{"StackName": name} for name in names]}]

        return _Pages()

    def get_template(self, **kwargs: object) -> dict[str, object]:
        return {"TemplateBody": self.templates[str(kwargs["StackName"])]}


def test_a_recorded_lagging_stack_passes_only_within_its_accepted_files(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = _staged(tmp_path)
    function: dict[str, object] = {
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Handler": "adapters.inbound.handler.handler",
                    "Runtime": "python3.14",
                },
            }
        }
    }
    cloudformation = _CloudFormation({"Adlytics-X-dev": function})

    class _Session:
        @staticmethod
        def client(_name: str) -> _CloudFormation:
            return cloudformation

    session = _Session()

    def changed(_worktree: object, _sha: str) -> tuple[str, ...]:
        return ("src/adapters/outbound/store.py",)

    monkeypatch.setattr(gate, "changed_files", changed)
    sources = _FakeGit(
        {"src/adapters/outbound/store.py": ("X = 1\n", (staged / "adapters/outbound/store.py"))}
    )
    monkeypatch.setattr(gate, "_git", sources)
    request = gate.AssetScanRequest(tmp_path, "sha", staged, "dev", frozenset())
    assert gate.asset_scan(session, request)["passed"] is False
    pinned = {"src/adapters/outbound/store.py": _BLOB}
    accepted = gate.AssetScanRequest(
        tmp_path, "sha", staged, "dev", frozenset(), frozenset({"Adlytics-X-dev"}), pinned
    )
    result = gate.asset_scan(session, accepted)
    assert (result["passed"], result["accepted_lagging"]) == (True, ["Adlytics-X-dev"])
    narrower = gate.AssetScanRequest(
        tmp_path, "sha", staged, "dev", frozenset(), frozenset({"Adlytics-X-dev"}), {}
    )
    assert gate.asset_scan(session, narrower)["passed"] is False
    # B7: a later edit to an accepted file changes its blob, and the acceptance lapses.
    sources.blob = "f" * 40
    moved = gate.asset_scan(session, accepted)
    assert (moved["passed"], moved["lagging_files_moved"]) == (
        False,
        ["src/adapters/outbound/store.py"],
    )


_BLOB = "a" * 40


class _FakeGit:
    """`git rev-parse <sha>:<path>` and `git show <rev>:<path>` over fixed contents."""

    def __init__(self, files: dict[str, tuple[str, pathlib.Path]]) -> None:
        self.files = files
        self.blob = _BLOB

    def __call__(self, _worktree: object, *args: str) -> str:
        command, spec = args[0], args[-1]
        revision, _, path = spec.partition(":")
        before, staged = self.files[path]
        if command == "rev-parse":
            return self.blob + "\n"
        return before if revision == gate._BASE else staged.read_text()  # pyright: ignore[reportPrivateUsage]


def test_b1_a_lagging_stack_that_decodes_an_extended_closed_set_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-version data check: old code that parses a closed set refuses its new values."""
    staged = _staged(tmp_path)
    enum_v1 = "from enum import StrEnum\nclass Level(StrEnum):\n    A = 'a'\n"
    (staged / "adapters/outbound/store.py").write_text(enum_v1 + "    B = 'b'\n")
    function: dict[str, object] = {
        "Resources": {
            "Fn": {
                "Type": "AWS::Lambda::Function",
                "Properties": {
                    "Handler": "adapters.inbound.handler.handler",
                    "Runtime": "python3.14",
                },
            }
        }
    }
    cloudformation = _CloudFormation({"Adlytics-X-dev": function})

    class _Session:
        @staticmethod
        def client(_name: str) -> _CloudFormation:
            return cloudformation

    def changed(*_args: object) -> tuple[str, ...]:
        return ("src/adapters/outbound/store.py",)

    monkeypatch.setattr(gate, "changed_files", changed)
    monkeypatch.setattr(
        gate,
        "_git",
        _FakeGit(
            {"src/adapters/outbound/store.py": (enum_v1, staged / "adapters/outbound/store.py")}
        ),
    )
    pinned = {"src/adapters/outbound/store.py": _BLOB}
    request = gate.AssetScanRequest(
        tmp_path, "sha", staged, "dev", frozenset(), frozenset({"Adlytics-X-dev"}), pinned
    )
    # Positive control: nothing in the closure constructs the enum, so the stack is accepted.
    passed = gate.asset_scan(_Session(), request)
    assert passed["extended_closed_sets"] == {"Level": ["b"]}
    assert (passed["passed"], passed["decoders"]) == (True, [])
    # A decoder the handler never calls: the closure holds it, but no data reaches it.
    helper = staged / "adapters/inbound/helper.py"
    helper.write_text(
        helper.read_text()
        + "from adapters.outbound.store import Level\n"
        + "def decode(value):\n    return Level(value)\n"
    )
    handler = staged / "adapters/inbound/handler.py"
    handler.write_text(handler.read_text() + "def handler(event, context):\n    return event\n")
    unreached = gate.asset_scan(_Session(), request)
    assert unreached["decoders"] == ["adapters/inbound/helper.py"]
    assert unreached["passed"] is True
    # The handler calls the decoder through a module alias: acceptance is refused.
    handler.write_text(
        handler.read_text() + "def handler(event, context):\n    return helper.decode(event)\n"
    )
    refused = gate.asset_scan(_Session(), request)
    assert refused["passed"] is False
    stack = refused["stacks"]["Adlytics-X-dev"]  # type: ignore[index]
    assert stack["data_decoders"] == ["adapters.inbound.handler:handler"]


def test_b8_the_probe_names_the_deployed_sha_and_links_it_to_the_runner(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    files: list[pathlib.Path] = []
    for index, sha in enumerate(("d" * 40, "d" * 40)):
        path = tmp_path / f"assets-{index}.json"
        path.write_text(json.dumps({"stack": f"S{index}", "sha": sha}))
        files.append(path)
    changed: list[str] = []

    def fake_git(*_args: object) -> str:
        return "\n".join(changed)

    monkeypatch.setattr(gate, "_git", fake_git)
    linked = gate.deployed_sha(tmp_path, "r" * 40, files)
    assert (linked["deployed_sha"], linked["passed"]) == ("d" * 40, True)
    changed.append("src/adapters/x.py")
    assert gate.deployed_sha(tmp_path, "r" * 40, files)["passed"] is False
    files[1].write_text(json.dumps({"stack": "S1", "sha": "e" * 40}))
    split = gate.deployed_sha(tmp_path, "r" * 40, files)
    assert (split["deployed_sha"], split["passed"]) == (None, False)


def test_b7_an_unpinned_lagging_file_is_refused() -> None:
    with pytest.raises(gate.ProbeError):
        gate.parse_lagging_files(["src/adapters/config.py"])
    assert gate.parse_lagging_files([f"src/a.py={_BLOB}"]) == {"src/a.py": _BLOB}


def test_p2_m2_every_diff_header_must_name_the_deployed_sha(tmp_path: pathlib.Path) -> None:
    first, second = tmp_path / "a.txt", tmp_path / "b.txt"
    first.write_text("SHA=abc\nStack x\n")
    second.write_text("SHA=abc\n")

    assert gate.diff_header_check([first, second], "abc")["passed"] is True
    assert gate.diff_header_check([first, second], "def")["passed"] is False
    second.write_text("SHA=def\n")
    assert gate.diff_header_check([first, second], "abc")["passed"] is False
    assert gate.diff_header_check([], "abc")["passed"] is False


def test_p2_m4_baseline_graphs_parse_only_hex_pairs() -> None:
    name, digest = "a" * 64, "b" * 64

    assert gate.parse_baseline_graphs([f"{name}={digest}"]) == {name: digest}
    with pytest.raises(gate.ProbeError):
        gate.parse_baseline_graphs([f"{name}:{digest}"])
    with pytest.raises(gate.ProbeError):
        gate.parse_baseline_graphs([f"{'z' * 64}={digest}"])


def test_p2_page_union_counts_duplicates_across_pages() -> None:
    page_one = [{"request_id": "a", "receipt_key": "RECEIPT"}]
    page_two = [{"request_id": "a", "receipt_key": "CONFLICT#x"}, {"request_id": "b"}]

    union = gate.page_union([page_one, page_two])

    assert union == {"keys": ["a#RECEIPT", "a#CONFLICT#x", "b#None"], "duplicates": 0}
    assert gate.page_union([page_one, page_one])["duplicates"] == 1


def test_p2_graph_digest_is_the_p1_canonical_item_digest() -> None:
    item = {"pk": {"S": "GRAPH#x"}, "sk": {"S": "GRAPH"}}
    expected = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()

    assert gate.graph_digest(item) == expected


def test_p2_error_metric_query_keys_on_the_emf_record_not_on_the_array_value() -> None:
    query = gate.error_metric_query("ADLY-EXEC_EXPLAIN-0001")

    assert 'ErrorCode = "ADLY-EXEC_EXPLAIN-0001"' in query
    assert "CloudWatchMetrics" in query
    assert "ispresent(Errors)" not in query


def test_p2_bf5_load_overlap_counts_only_calls_inside_the_lifecycle_window() -> None:
    calls = [
        gate.TimedCall(200, 0.0, 1.0),  # ended before the command started
        gate.TimedCall(200, 1.5, 2.5),  # inside
        gate.TimedCall(429, 2.0, 2.1),  # inside, shed by throttle
        gate.TimedCall(200, 2.9, 3.4),  # straddles the end
        gate.TimedCall(503, 3.5, 3.6),  # after the command
    ]

    summary = gate.load_overlap(calls, (2.0, 3.0))

    assert summary == {
        "calls": 5,
        "statuses": {"200": 3, "429": 1, "503": 1},
        "overlapping": 3,
        "overlapping_ok": 2,
        "unexpected": [],
    }


def test_p2_bf5_any_non_throttle_failure_under_load_is_unexpected() -> None:
    calls = [gate.TimedCall(403, 0.0, 1.0), gate.TimedCall(-1, 0.0, 1.0), gate.TimedCall(200, 0, 1)]

    assert gate.load_overlap(calls, (0.0, 1.0))["unexpected"] == [-1, 403]


_AT = "2026-10-07T08:38:45+00:00"


def _explanation_read(**overrides: object) -> gate.ExplanationRead:
    receipts = [
        {"request_id": "p1-first", "receipt_key": "RECEIPT", "label": "shadow", "attested": True},
        {"request_id": "p1-second", "receipt_key": "RECEIPT", "label": "shadow", "attested": True},
    ]
    values: dict[str, object] = {
        "document": {
            "receipts": receipts,
            "stages": [{"stage": "received", "flow_root_elapsed_ms": 0, "occurred_at": _AT}],
        },
        "traversal": {
            "duplicates": 0,
            "keys": ["p1-first#RECEIPT", "p1-second#RECEIPT"],
            "page_sizes": [1, 1],
        },
        "receipt_census": 2,
        "p1_requests": frozenset({"p1-first", "p1-second"}),
        "manifest_last_modified": _AT,
    }
    values.update(overrides)
    return gate.ExplanationRead(**values)  # pyright: ignore[reportArgumentType]


def test_s3v2_2_the_p2_explanation_invariants_hold_for_a_complete_two_page_read() -> None:
    assert gate.explanation_invariants(_explanation_read()) == {
        "receipts_cover_the_root": True,
        "root_received_elapsed_0": True,
        "two_page_traversal_no_duplicate_or_omission": True,
    }


@pytest.mark.parametrize(
    ("overrides", "failed"),
    [
        ({"receipt_census": 3}, "receipts_cover_the_root"),
        ({"p1_requests": frozenset({"p1-first", "p1-other"})}, "receipts_cover_the_root"),
        ({"manifest_last_modified": "2026-10-07T08:38:46+00:00"}, "root_received_elapsed_0"),
        (
            {"traversal": {"duplicates": 0, "keys": ["p1-first#RECEIPT"], "page_sizes": [1]}},
            "two_page_traversal_no_duplicate_or_omission",
        ),
        (
            {
                "traversal": {
                    "duplicates": 1,
                    "keys": ["p1-first#RECEIPT", "p1-second#RECEIPT"],
                    "page_sizes": [1, 1],
                }
            },
            "two_page_traversal_no_duplicate_or_omission",
        ),
    ],
)
def test_s3v2_2_each_p2_explanation_invariant_fails_on_its_own_defect(
    overrides: dict[str, object], failed: str
) -> None:
    checks = gate.explanation_invariants(_explanation_read(**overrides))

    assert [name for name, passed in checks.items() if not passed] == [failed]


def test_s3v2_2_the_p1_request_ids_are_read_from_the_p1_evidence() -> None:
    p1 = {
        "requests": {
            "first": {"receipt": {"request_id": "p1-first"}},
            "second": {"receipt": {"request_id": "p1-second"}},
        }
    }

    assert gate.p1_request_ids(p1) == frozenset({"p1-first", "p1-second"})


# --- P3 (S3) ------------------------------------------------------------------------------------

from validation import probe_ingestion_traceability_p3 as p3  # noqa: E402


def test_p3_the_s6_sole_writer_form_admits_the_admission_roles_and_five_workers() -> None:
    writers = {
        "Adlytics-Api-dev": [{"roles": [{"Ref": "RunRequestsFunctionServiceRole"}]}],
        "Adlytics-Ingestion-dev": [
            *(
                {"roles": [{"Ref": f"Stage1ExecutionGraph{name}ExecutionRoleDC75639D"}]}
                for name in p3.WORKERS
            ),
            # S6 (V-19): the delivery handler and the registration finalizer admit.
            {"roles": [{"Ref": "IngestionExecutionRoleFictional"}]},
            {"roles": [{"Ref": "ProducerFinalizerExecutionRoleFictional"}]},
        ],
    }
    expected = {
        "Adlytics-Api-dev": 1,
        "Adlytics-Ingestion-dev": len(gate.INGESTION_LIVE_WRITER_PREFIXES),
    }
    assert len(p3.WORKERS) == 5  # S4 adds the release issuer
    assert len(gate.INGESTION_LIVE_WRITER_PREFIXES) == 7
    assert gate.sole_writer_verdict(writers, expected)["passed"] is True
    stray = {
        **writers,
        "Adlytics-Ingestion-dev": [
            *writers["Adlytics-Ingestion-dev"][:6],
            {"roles": [{"Ref": "Stage1TableWriterExecutionRole"}]},
        ],
    }
    assert gate.sole_writer_verdict(stray, expected)["passed"] is False
    # S3V-13: a role whose name merely contains `ExecutionGraph`, standing in for a worker that
    # lost its grant, fails even though the count still reads five.
    swapped = {
        **writers,
        "Adlytics-Ingestion-dev": [
            *writers["Adlytics-Ingestion-dev"][:4],
            *writers["Adlytics-Ingestion-dev"][5:],
            {"roles": [{"Ref": "Stage1ExecutionGraphProjectorExecutionRoleFictional"}]},
        ],
    }
    assert gate.sole_writer_verdict(swapped, expected)["passed"] is False
    assert (
        gate.sole_writer_verdict(
            {**writers, "Adlytics-Publisher-dev": writers["Adlytics-Api-dev"]}, expected
        )["passed"]
        is False
    )


_PLAN = "a" * 64
_STREAM = "2026/10/08/[$LATEST]0123"
_ERROR_AT = "2026-10-08 03:59:52.798"
_BEGIN_CCF = "None, None, None, None, ConditionalCheckFailed"
_CLAIM_TC = "TransactionConflict, TransactionConflict, None, None, None, None, None"


def _plan_coord(operation: str, reasons: str, *, plan: str = _PLAN) -> dict[str, str]:
    return {
        "@timestamp": _ERROR_AT,
        "@logStream": _STREAM,
        "correlation_id": f"WRITE_PLAN#{plan}#" + "b" * 64,
        "error_code": "ADLY-PLAN_COORD-0001",
        "error_context.operation": operation,
        "error_context.provider_message": "Transaction cancelled, please refer cancellation "
        f"reasons for specific reasons [{reasons}]",
    }


def _traceback(
    *frames: str, at: str = "2026-10-08 03:59:52.877", stream: str = _STREAM
) -> dict[str, str]:
    lines = "".join(
        f'  File "/var/task/x.py", line 1, in {frame}\n    call()\n' for frame in frames
    )
    return {
        "@timestamp": at,
        "@logStream": stream,
        "@message": "[ERROR] DomainError: ADLY-PLAN_COORD-0001\nTraceback (most recent call "
        f"last):\n{lines}",
    }


_LEGACY_CLAIM = _traceback(
    "handler", "_process_unit", "_process_legacy_unit", "_step_one_claim", "_claim", "claim_unit"
)
_LEGACY_BEGIN = _traceback(
    "handler", "_process_unit", "_process_legacy_unit", "_execute_statements", "begin_table_attempt"
)


def test_s3v3_a_raised_receive_is_a_finding_that_must_classify() -> None:
    race = _plan_coord("BeginTableAttemptRequest", _BEGIN_CCF)
    assert gate.classify_raised_receives(0, [], _PLAN, [])["passed"] is True
    assert gate.classify_raised_receives(1, [race], _PLAN, [_LEGACY_BEGIN])["passed"] is True
    # A raised receive with no error record behind it is unexplained.
    assert gate.classify_raised_receives(2, [race], _PLAN, [_LEGACY_BEGIN])["passed"] is False
    # The same code on another member (a real lease or fence move) is not the known race.
    lease = {**race, "error_context.provider_message": "[ConditionalCheckFailed, None, None]"}
    verdict = gate.classify_raised_receives(1, [lease], _PLAN, [_LEGACY_BEGIN])
    assert verdict["passed"] is False
    assert verdict["unclassified"] == [lease]


def test_s3i3_a_legacy_sibling_claim_conflict_on_the_probe_plan_classifies() -> None:
    # The two P3 run 4 records (2026-10-08T03:59:52Z), each with its own legacy traceback.
    other = "2026/10/08/[$LATEST]4567"
    records = [
        _plan_coord("ClaimUnitRequest", _CLAIM_TC),
        {
            **_plan_coord(
                "ClaimUnitRequest",
                "None, None, TransactionConflict, TransactionConflict, None, None, None",
            ),
            "@logStream": other,
        },
    ]
    tracebacks = [_LEGACY_CLAIM, {**_LEGACY_CLAIM, "@logStream": other}]

    verdict = gate.classify_raised_receives(2, records, _PLAN, tracebacks)

    assert verdict["passed"] is True
    assert verdict["known_sibling_reservation_race"] == 2
    assert verdict["known_operations"] == ["ClaimUnitRequest"]


@pytest.mark.parametrize(
    ("record", "tracebacks"),
    [
        # Another plan's contention is not the probe's own.
        (_plan_coord("ClaimUnitRequest", _CLAIM_TC, plan="c" * 64), [_LEGACY_CLAIM]),
        # A condition failure on a claim member is a real moved owner, plan, intent or unit.
        (
            _plan_coord(
                "ClaimUnitRequest", "ConditionalCheckFailed, None, None, None, None, None, None"
            ),
            [_LEGACY_CLAIM],
        ),
        (
            _plan_coord("ClaimUnitRequest", "None, None, None, None, None, None, None"),
            [_LEGACY_CLAIM],
        ),
        # Reclaim (8 members) and other member counts are not the observed shape.
        (
            _plan_coord("ClaimUnitRequest", _CLAIM_TC + ", None"),
            [_LEGACY_CLAIM],
        ),
        # A Begin conflict is not admitted: only its META condition failure is known.
        (
            _plan_coord("BeginTableAttemptRequest", "None, None, None, None, TransactionConflict"),
            [_LEGACY_BEGIN],
        ),
        (_plan_coord("FinalizeUnitRequest", _CLAIM_TC), [_LEGACY_CLAIM]),
        (
            {**_plan_coord("ClaimUnitRequest", _CLAIM_TC), "error_code": "ADLY-OTHER-0001"},
            [_LEGACY_CLAIM],
        ),
        # The path: no traceback, a graph-claim traceback, another stream, too late, wrong call.
        (_plan_coord("ClaimUnitRequest", _CLAIM_TC), []),
        (
            _plan_coord("ClaimUnitRequest", _CLAIM_TC),
            [_traceback("handler", "_process_unit", "_graph_claim", "claim_unit")],
        ),
        (
            _plan_coord("ClaimUnitRequest", _CLAIM_TC),
            [{**_LEGACY_CLAIM, "@logStream": "2026/10/08/[$LATEST]other"}],
        ),
        (
            _plan_coord("ClaimUnitRequest", _CLAIM_TC),
            [{**_LEGACY_CLAIM, "@timestamp": "2026-10-08 03:59:55.000"}],
        ),
        (_plan_coord("ClaimUnitRequest", _CLAIM_TC), [_LEGACY_BEGIN]),
    ],
)
def test_s3i3_every_other_raised_receive_stays_unclassified(
    record: dict[str, str], tracebacks: list[dict[str, str]]
) -> None:
    verdict = gate.classify_raised_receives(1, [record], _PLAN, tracebacks)

    assert verdict["passed"] is False
    assert verdict["unclassified"] == [record]


def _plan_state(**overrides: object) -> dict[str, object]:
    units = {"candidate": "attested", "current": "attested", "source_aligned": "attested"}
    state: dict[str, object] = {
        "status": "completed",
        "unit_count": 3,
        "attested_count": 3,
        "failed_count": 0,
        "units": units,
    }
    state.update(overrides)
    return state


def _contention(state: dict[str, object], c4: int = 0, rows: bool = True) -> dict[str, object]:
    classification = gate.classify_raised_receives(
        1, [_plan_coord("ClaimUnitRequest", _CLAIM_TC)], _PLAN, [_LEGACY_CLAIM]
    )
    return gate.contention_verdict(classification, gate.ContentionOutcomes(state, c4, rows))


def test_s3i3_known_contention_is_accepted_only_after_every_outcome_holds() -> None:
    verdict = _contention(_plan_state())

    assert verdict["passed"] is True
    assert verdict["known_contention"] == 1


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        # Paired control: the same conflict, but a unit is left unattested.
        (
            _plan_state(
                attested_count=2,
                units={"candidate": "attested", "current": "writing", "source_aligned": "attested"},
            ),
            (0, True, "every_unit_attested"),
        ),
        # A unit record missing from the plan is not "every unit".
        (
            _plan_state(units={"candidate": "attested", "current": "attested"}),
            (0, True, "every_unit_attested"),
        ),
        (_plan_state(status="admitted"), (0, True, "plan_completed")),
        (_plan_state(failed_count=1), (0, True, "plan_completed")),
        (_plan_state(), (1, True, "c4_zero")),
        (_plan_state(), (0, False, "rows_equal")),
    ],
)
def test_s3i3_the_same_conflict_with_any_bad_outcome_fails(
    state: dict[str, object], outcome: tuple[int, bool, str]
) -> None:
    c4, rows, failed = outcome
    verdict = _contention(state, c4, rows)

    assert verdict["passed"] is False
    checks = cast("dict[str, bool]", verdict["checks"])
    assert [name for name, ok in checks.items() if not ok] == [failed]


def test_s3i3_an_unclassified_record_fails_whatever_the_outcomes() -> None:
    classification = gate.classify_raised_receives(
        1, [_plan_coord("ClaimUnitRequest", _CLAIM_TC)], _PLAN, []
    )

    verdict = gate.contention_verdict(
        classification, gate.ContentionOutcomes(_plan_state(), 0, True)
    )

    assert verdict["passed"] is False


def test_s3i3_no_raised_receive_needs_no_contention_outcome() -> None:
    classification = gate.classify_raised_receives(0, [], _PLAN, [])

    verdict = gate.contention_verdict(classification, gate.ContentionOutcomes({}, 0, True))

    assert verdict["passed"] is True


def test_s3v10_the_run_conflict_verdict_reads_measured_values_not_literals() -> None:
    good = {
        "parallel_claims": 16,
        "committed": 16,
        "run_conflicts": 12,
        "exhausted": 0,
        "max_attempts": 4,
        "bound": 16,
    }
    assert p3.run_conflicts_verdict(good) is True
    assert p3.run_conflicts_verdict({**good, "exhausted": 1, "committed": 15}) is False
    assert p3.run_conflicts_verdict({k: v for k, v in good.items() if k != "max_attempts"}) is False
    assert p3.run_conflicts_verdict({**good, "committed": 15}) is False


def test_p3_epoch_1_work_on_the_live_table_fails_the_zero_send_check() -> None:
    assert p3.census_has_no_graph_work({"RECEIPT": 5, "GRAPH": 2, "MODE": 1, "COMMAND": 3})
    assert not p3.census_has_no_graph_work({"RECEIPT": 5, "DUE/OUTBOX": 1})
    assert not p3.census_has_no_graph_work({"RUN": 1})


def test_p3_every_worker_schedule_must_read_enabled_in_its_s6_form() -> None:
    rules = {f"adlytics-dev-ExecutionGraph{name}Schedule": "ENABLED" for name in p3.WORKERS}
    assert p3.schedule_verdict(rules)["passed"] is True
    rules["adlytics-dev-ExecutionGraphDispatcherSchedule"] = "DISABLED"
    assert p3.schedule_verdict(rules)["passed"] is False
    assert p3.schedule_verdict({})["passed"] is False


def test_p3_enabled_workers_pass_only_with_zero_errors_for_every_worker() -> None:
    functions = [f"adlytics-dev-ExecutionGraph{name}" for name in p3.WORKERS]
    invocations = dict.fromkeys(functions, 30.0)
    clean = p3.worker_errors_verdict(invocations, dict.fromkeys(functions, 0.0))
    assert clean["passed"] is True
    assert clean["invocations"] == invocations
    # Control: one worker erroring (a mode read that fails) is not a mode-gated no-op.
    erroring = {**dict.fromkeys(functions, 0.0), functions[0]: 1.0}
    assert p3.worker_errors_verdict(invocations, erroring)["passed"] is False
    # Control: a worker whose error count was never read cannot pass.
    assert p3.worker_errors_verdict(invocations, {functions[0]: 0.0})["passed"] is False
    assert p3.worker_errors_verdict({}, {})["passed"] is False


def test_p3_v15_iam_verdict_denies_workers_and_allows_exactly_the_probe_set() -> None:
    allow_all = dict.fromkeys(p3._PROBE_ACTIONS, "allowed")  # pyright: ignore[reportPrivateUsage]
    deny_all = dict.fromkeys(p3._PROBE_ACTIONS, "implicitDeny")  # pyright: ignore[reportPrivateUsage]
    mode: dict[str, str] = {
        **deny_all,
        "dynamodb:GetItem": "allowed",
        "dynamodb:ConditionCheckItem": "allowed",
    }
    explicit: dict[str, str] = {
        **deny_all,
        "dynamodb:PutItem": "explicitDeny",
        "dynamodb:UpdateItem": "explicitDeny",
        "dynamodb:DeleteItem": "explicitDeny",
    }
    good = {
        "ExecutionGraphDispatcher#0|probe": deny_all,
        "probe|live": deny_all,
        "probe|probe#0": allow_all,
        "TableWriter|mode_item": mode,
        "TableWriter|other_item": deny_all,
        "ExecutionGraphRelease|operator_only#mode": explicit,
    }
    assert p3.iam_isolation_verdict(good)["passed"] is True
    leaked: dict[str, str] = {**deny_all, "dynamodb:GetItem": "allowed"}
    # An implicit deny is not the explicit Deny: inside a transaction the family grant could
    # still allow the write if the Deny were missing.
    implicit_update: dict[str, str] = {**explicit, "dynamodb:UpdateItem": "implicitDeny"}
    cases: list[tuple[str, dict[str, str]]] = [
        ("ExecutionGraphDispatcher#0|probe", leaked),
        ("probe|live", allow_all),
        ("probe|probe#0", deny_all),
        ("TableWriter|mode_item", deny_all),
        ("TableWriter|other_item", mode),
        ("ExecutionGraphRelease|operator_only#mode", implicit_update),
        ("ExecutionGraphRelease|operator_only#mode", allow_all),
    ]
    for key, value in cases:
        assert p3.iam_isolation_verdict({**good, key: value})["failures"] == [key]


def test_s6_the_requester_reads_the_mode_item_and_is_explicitly_denied_its_writes() -> None:
    deny_all = dict.fromkeys(p3._PROBE_ACTIONS, "implicitDeny")  # pyright: ignore[reportPrivateUsage]
    read: dict[str, str] = {
        **deny_all,
        "dynamodb:GetItem": "allowed",
        "dynamodb:ConditionCheckItem": "allowed",
    }
    denied: dict[str, str] = {
        **read,
        "dynamodb:PutItem": "explicitDeny",
        "dynamodb:UpdateItem": "explicitDeny",
        "dynamodb:DeleteItem": "explicitDeny",
    }
    key = "IngestionFunction|requester_mode_item"
    assert p3.iam_isolation_verdict({key: denied})["passed"] is True
    # An implicit deny on the mode item is not enough: the requester's run-request grant
    # could allow the write.
    assert p3.iam_isolation_verdict({key: read})["failures"] == [key]
    assert p3.iam_isolation_verdict({key: {**denied, "dynamodb:GetItem": "implicitDeny"}})[
        "failures"
    ] == [key]


def _ingestion_template(*roles: str) -> dict[str, object]:
    return {"Resources": {f"{role}ABC123": {"Type": "AWS::IAM::Role"} for role in roles}}


def test_s6_the_finalizer_is_a_required_writer_only_where_it_is_provisioned() -> None:
    workers = [f"Stage1ExecutionGraph{name}ExecutionRole" for name in p3.WORKERS]
    without = gate.provisioned_writer_prefixes(
        _ingestion_template(*workers, "IngestionExecutionRole")
    )
    assert gate.FINALIZER_ROLE_PREFIX not in without
    assert len(without) == len(gate.INGESTION_LIVE_WRITER_PREFIXES) - 1
    with_finalizer = gate.provisioned_writer_prefixes(
        _ingestion_template(*workers, "IngestionExecutionRole", gate.FINALIZER_ROLE_PREFIX)
    )
    assert with_finalizer == gate.INGESTION_LIVE_WRITER_PREFIXES
    writers = {
        "Adlytics-Api-dev": [{"roles": [{"Ref": "RunRequestsFunctionServiceRole"}]}],
        "Adlytics-Ingestion-dev": [
            *({"roles": [{"Ref": f"{worker}DC75639D"}]} for worker in workers),
            {"roles": [{"Ref": "IngestionExecutionRoleFictional"}]},
        ],
    }
    expected = {"Adlytics-Api-dev": 1, "Adlytics-Ingestion-dev": len(without)}
    assert gate.sole_writer_verdict(writers, expected, without)["passed"] is True
    # Where the finalizer is provisioned, a missing finalizer grant still fails.
    assert (
        gate.sole_writer_verdict(
            writers, {**expected, "Adlytics-Ingestion-dev": 7}, with_finalizer
        )["passed"]
        is False
    )


def test_s6_the_explanation_names_exactly_its_own_roots_graphs() -> None:
    root: dict[str, object] = {"graph_ids": ["g1", "g2"]}
    baseline = {"g1", "g2", "other-root"}
    graphs: list[dict[str, object]] = [
        {"graph_id": "g1", "authority_epoch": 0, "label": "shadow"},
        {"graph_id": "g2", "authority_epoch": 0, "label": "shadow"},
    ]
    assert gate.root_graphs_verdict(graphs, root, baseline) is True
    assert gate.root_graphs_verdict(graphs[:1], root, baseline) is False
    assert gate.root_graphs_verdict(graphs, root, {"g1"}) is False
    assert gate.root_graphs_verdict([], {"graph_ids": []}, baseline) is False
    epoch_1 = [{**graphs[0], "authority_epoch": 1}, graphs[1]]
    assert gate.root_graphs_verdict(epoch_1, root, baseline) is False


# --- S6 part 2 (plan B8): the pre-CAS readbacks and P6(e), unit-tested over fakes, not run ---

from dataclasses import dataclass as _dataclass  # noqa: E402
from dataclasses import field as _field  # noqa: E402
from dataclasses import replace as _replace  # noqa: E402
from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402
from typing import Any as _Any  # noqa: E402

from adapters.outbound.dynamodb.operational_status import row_measurement  # noqa: E402
from adapters.outbound.dynamodb.semantic_state import (  # noqa: E402
    CURRENT_RECORD,
    PUBLICATION_RECORD_KEY,
)
from domain.operations.generated_execution_graphs import BOUNDS  # noqa: E402
from domain.operations.graph_projection import project_graph_item  # noqa: E402

_GRAPH = "adlytics-dev-execution-graph"


def _writer_policy(kind: str, role: str, actions: list[str]) -> dict[str, object]:
    return {
        "Type": kind,
        "Properties": {
            "Roles": [{"Ref": role}],
            "PolicyDocument": {
                "Statement": [
                    {"Effect": "Allow", "Action": actions, "Resource": f"arn:x:table/{_GRAPH}"}
                ]
            },
        },
    }


def test_s6p2_table_writers_reads_overflow_managed_policies() -> None:
    template = {
        "Resources": {
            "Overflow": _writer_policy(
                "AWS::IAM::ManagedPolicy", "Stage1TableWriterExecutionRoleX", ["dynamodb:PutItem"]
            ),
            "ReadOnly": _writer_policy(
                "AWS::IAM::ManagedPolicy", "Stage1TableWriterExecutionRoleX", ["dynamodb:GetItem"]
            ),
        }
    }
    found = gate.table_writers(template, [_GRAPH])
    assert [item["policy"] for item in found] == ["Overflow"]
    # Control: a read-only managed policy grants no write.
    assert (
        gate.table_writers({"Resources": {"R": template["Resources"]["ReadOnly"]}}, [_GRAPH]) == []
    )


class _Paginator:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages

    def paginate(self, **_kwargs: object) -> list[dict[str, object]]:
        return self.pages


class _Cloudformation:
    def __init__(self, templates: dict[str, dict[str, object]]) -> None:
        self.templates = templates

    def get_paginator(self, _name: str) -> _Paginator:
        return _Paginator([{"Stacks": [{"StackName": name} for name in self.templates]}])

    def get_template(self, **kwargs: object) -> dict[str, object]:
        return {"TemplateBody": json.dumps(self.templates[str(kwargs["StackName"])])}


class _Session:
    def __init__(self, **clients: object) -> None:
        self.clients = clients

    def client(self, name: str) -> object:
        return self.clients[name]


def _graph_chain_templates(
    *, baker: bool = True, extra_publisher: bool = False
) -> dict[str, dict[str, object]]:
    workers = [f"Stage1ExecutionGraph{name}ExecutionRoleDC75639D" for name in p3.WORKERS]
    ingestion = {
        f"P{index}": _writer_policy("AWS::IAM::Policy", role, ["dynamodb:PutItem"])
        for index, role in enumerate(
            [*workers, "IngestionExecutionRoleF1", "Stage1TableWriterExecutionRoleF2"]
        )
    }
    publisher = (
        {
            "Baker": _writer_policy(
                "AWS::IAM::Policy", "BakerExecutionRoleF3", ["dynamodb:UpdateItem"]
            )
        }
        if baker
        else {}
    )
    if extra_publisher:
        publisher["Enqueue"] = _writer_policy(
            "AWS::IAM::Policy", "EnqueueExecutionRoleF5", ["dynamodb:PutItem"]
        )
    return {
        "Adlytics-Api-dev": {
            "Resources": {
                "R": _writer_policy(
                    "AWS::IAM::Policy", "RunRequestsFunctionServiceRole", ["dynamodb:PutItem"]
                )
            }
        },
        "Adlytics-Ingestion-dev": {"Resources": ingestion},
        "Adlytics-Publisher-dev": {"Resources": publisher},
        "Adlytics-InsightPrecompute-dev": {
            "Resources": {
                "W": _writer_policy(
                    "AWS::IAM::Policy", "PrecomputeServiceRoleF4", ["dynamodb:PutItem"]
                )
            }
        },
    }


def test_s6p2_the_sole_writer_form_learns_the_graph_chain_writers() -> None:
    session = _Session(cloudformation=_Cloudformation(_graph_chain_templates()))
    verdict = gate._p1_sole_writer(session, "dev", {"Adlytics-Api-dev": 1}, graph_chain=True)  # pyright: ignore[reportPrivateUsage]
    assert verdict["passed"] is True
    assert verdict["counts"] == {
        "Adlytics-Api-dev": 1,
        "Adlytics-Ingestion-dev": 7,
        "Adlytics-Publisher-dev": 1,
        "Adlytics-InsightPrecompute-dev": 1,
    }
    # Control: the same deployed writers against the pre-S6-part-2 declaration fail.
    assert (
        gate._p1_sole_writer(session, "dev", {"Adlytics-Api-dev": 1})["passed"]  # pyright: ignore[reportPrivateUsage]
        is False
    )


@pytest.mark.parametrize("change", [{"baker": False}, {"extra_publisher": True}])
def test_s6p2_a_missing_or_stray_consumer_writer_fails(change: dict[str, bool]) -> None:
    session = _Session(cloudformation=_Cloudformation(_graph_chain_templates(**change)))
    verdict = gate._p1_sole_writer(session, "dev", {"Adlytics-Api-dev": 1}, graph_chain=True)  # pyright: ignore[reportPrivateUsage]
    assert verdict["passed"] is False


@guard_reserved_words
class _Scans:
    def __init__(self, pages: list[dict[str, object]]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    def scan(self, **request: object) -> dict[str, object]:
        self.calls.append(dict(request))
        return self.pages[len(self.calls) - 1]


def test_s6p2_the_zero_unstamped_count_scans_every_page_read_only_and_stops_on_nonzero() -> None:
    assert (gate.PUBLICATIONS_RECORD_KEY, gate.PUBLICATIONS_CURRENT) == (
        PUBLICATION_RECORD_KEY,
        CURRENT_RECORD,
    )
    scans = _Scans(
        [
            {"Count": 0, "ScannedCount": 40, "LastEvaluatedKey": {"k": {"S": "x"}}},
            {"Count": 0, "ScannedCount": 2},
        ]
    )
    read = gate.unstamped_current_count(scans, "publications-fixture")
    assert (read["count"], read["scanned"], read["pages"]) == (0, 42, 2)
    assert all(call["Select"] == "COUNT" for call in scans.calls)
    assert scans.calls[1]["ExclusiveStartKey"] == {"k": {"S": "x"}}
    assert "attribute_not_exists(projected_publication_id)" in str(
        scans.calls[0]["FilterExpression"]
    )
    assert gate.zero_unstamped_verdict(read)["passed"] is True
    # Control: one unstamped CURRENT is a stop.
    assert gate.zero_unstamped_verdict({**read, "count": 1})["passed"] is False


def _reads(digest_body: object = "page-body") -> list[gate.ReaderRead]:
    return [
        gate.reader_read(
            surface,
            "2026-09-01/2026-09-07",
            {surface: digest_body},
            publication_ids=["p-1"],
            read_at="t",
        )
        for surface in gate.READER_SURFACES
    ]


def test_s6p2_the_reader_readback_matches_its_baseline_and_counts_refusals() -> None:
    assert gate.reader_readback_verdict(_reads(), _reads(), 0)["passed"] is True
    # Controls: a changed body, a missing surface, or one "head vector changed" each fail.
    assert gate.reader_readback_verdict(_reads(), _reads("changed"), 0)["passed"] is False
    assert gate.reader_readback_verdict(_reads(), _reads()[:2], 0)["passed"] is False
    assert gate.reader_readback_verdict(_reads(), _reads(), 1)["passed"] is False
    with pytest.raises(gate.ProbeError):
        gate.reader_read("agent", "w", {}, publication_ids=[], read_at="t")
    assert "head vector changed" in gate.head_vector_error_query()


def test_s6p2_the_probe_reader_read_records_each_partitions_resolved_publication() -> None:
    @_dataclass
    class _Found:
        publication_id: _Any

    @_dataclass
    class _Partition:
        identity: str

    class _Reader:
        def current(self, partition: _Partition) -> _Found | None:
            if partition.identity == "absent":
                return None
            return _Found(type("Id", (), {"value": f"pub-{partition.identity}"})())

    read = gate.probe_reader_read(
        _Reader(), [_Partition("a"), _Partition("absent")], window="w", read_at="t"
    )
    assert read.publication_ids == ("pub-a",)
    again = gate.probe_reader_read(
        _Reader(), [_Partition("a"), _Partition("absent")], window="w", read_at="t2"
    )
    assert again.digest == read.digest  # the read time is recorded, never digested


def test_s6p2_the_legacy_pointer_readback_is_todays_envelope_with_no_cutover_drop() -> None:
    items = [{"page_variant_key": "campaigns#7d", "revision": "r", "forward_token": 2}]
    reports = [{"result": "unchanged"}]
    assert gate.legacy_pointer_verdict(items, 0, reports)["passed"] is True
    # Controls: a grant-bearing field, a cutover drop, or an unavailable report fails.
    assert (
        gate.legacy_pointer_verdict([{**items[0], "written_at": "x"}], 0, reports)["passed"]
        is False
    )
    assert gate.legacy_pointer_verdict(items, 1, reports)["passed"] is False
    assert gate.legacy_pointer_verdict(items, 0, [{"result": "unavailable"}])["passed"] is False


def test_s6p2_the_footnote_shows_the_window_end_without_a_build_time_or_stale_copy() -> None:
    page = "Campaigns ... Data as of 7 Sep 2026."
    assert gate.footnote_verdict(page, ["bundle.js"], "7 Sep 2026")["passed"] is True
    assert gate.footnote_verdict(page + " Built 8 Sep 2026", [], "7 Sep 2026")["passed"] is False
    assert (
        gate.footnote_verdict(page, ["latest published coverage"], "7 Sep 2026")["passed"] is False
    )
    assert gate.footnote_verdict("Data as of 6 Sep 2026.", [], "7 Sep 2026")["passed"] is False


def test_s6p2_the_footnote_verdict_passes_the_static_sentence_and_fails_each_build_form() -> None:
    # The SHADOW footnote as deployed: its static sentence says "built from", not a build time.
    shipped = (
        "Data as of Oct 6, 2026. Windows anchor to the published coverage this page was built "
        "from, never to today."
    )
    verdict = gate.footnote_verdict(shipped, [], "Oct 6, 2026")
    assert verdict["build_time_shown"] is False
    assert verdict["passed"] is True
    # Controls: both GRAPH forms (`formatIsoInstant` output) are a shown build time.
    built_at = "Data as of Oct 6, 2026, built 2026-10-07 04:05:11 UTC. " + shipped[24:]
    built_before = "Data as of Oct 6, 2026, built before 2026-10-07 04:05:11 UTC. " + shipped[24:]
    for page in (built_at, built_before):
        verdict = gate.footnote_verdict(page, [], "Oct 6, 2026")
        assert verdict["build_time_shown"] is True
        assert verdict["passed"] is False


_GRAPH_PAGE = (
    "Data as of Oct 6, 2026, built 2026-10-14 05:25:34 UTC. Windows anchor to the published "
    "coverage this page was built from, never to today."
)
_GRAPH_READBACK: dict[str, object] = {
    "generation": {"built_at": "2026-10-14T05:25:34Z"},
    "window_end_iso": "2026-10-06",
    "read_at": "2026-10-15T20:10:00.000Z",
}
_FINALIZE_END = "2026-10-15T20:05:00+00:00"


def test_hist8_the_graph_footnote_shows_the_served_window_and_granted_build_time() -> None:
    verdict = gate.graph_footnote_verdict(
        _GRAPH_READBACK, ["bundle.js"], _GRAPH_PAGE, not_before=_FINALIZE_END
    )
    assert verdict["expected"] == "Data as of Oct 6, 2026, built 2026-10-14 05:25:34 UTC."
    assert verdict["passed"] is True
    assert verdict["disposition"] == "PASS"


@pytest.mark.parametrize(
    "case",
    [
        # Stale: the page renders a build time other than the served generation's.
        (
            _GRAPH_READBACK,
            _GRAPH_PAGE.replace("05:25:34", "05:25:33"),
            [],
            "footnote_missing_or_stale",
        ),
        # Missing: no footnote rendered at all.
        (_GRAPH_READBACK, "Campaigns", [], "footnote_missing_or_stale"),
        (_GRAPH_READBACK, _GRAPH_PAGE, ["latest published coverage"], "stale_copy"),
        (
            {**_GRAPH_READBACK, "generation": {"built_before": "2026-10-14T05:25:34Z"}},
            _GRAPH_PAGE,
            [],
            "generation_not_granted",
        ),
        (
            {**_GRAPH_READBACK, "generation": None},
            _GRAPH_PAGE,
            [],
            "generation_not_granted",
        ),
        ({**_GRAPH_READBACK, "window_end_iso": None}, _GRAPH_PAGE, [], "window_end_absent"),
        (
            {**_GRAPH_READBACK, "read_at": "2026-10-15T20:00:00.000Z"},
            _GRAPH_PAGE,
            [],
            "read_before_bake_finalize",
        ),
        ({**_GRAPH_READBACK, "render_failed": {"kind": "exit"}}, _GRAPH_PAGE, [], "render_failed"),
    ],
)
def test_hist8_a_stale_missing_or_early_footnote_is_not_proven(
    case: tuple[dict[str, object], str, list[str], str],
) -> None:
    readback, page, assets, reason = case
    verdict = gate.graph_footnote_verdict(readback, assets, page, not_before=_FINALIZE_END)
    assert reason in cast("list[str]", verdict["reasons"])
    assert verdict["passed"] is False
    assert verdict["disposition"] == "NOT_PROVEN"


_PRESENTATION_REASONS = {
    "presentation_refused",
    "presentation_unauthorized",
    "presentation_failed",
}


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        # A governed refusal: the handler's DOMAIN class (409), e.g. the grant-serving check.
        (
            {"status": 409, "code": "ADLY-PRESENTATION-0002", "error_class": "domain"},
            "presentation_refused",
        ),
        # A governed refusal of the request itself: the VALIDATION class (400).
        (
            {"status": 400, "code": "ADLY-PRESENTATION-0001", "error_class": "validation"},
            "presentation_refused",
        ),
        # The live Reach month 500 (2026-10-18): an InvariantError, a defect, not a refusal.
        (
            {"status": 500, "code": "ADLY-PAGE_CONTEXT-0004", "error_class": "invariant"},
            "presentation_failed",
        ),
        (
            {"status": 503, "code": "ADLY-FICTIONAL-0001", "error_class": "dependency"},
            "presentation_failed",
        ),
        # No JSON body (an edge or gateway error): unclassified, so a failure, never a refusal.
        ({"status": 502, "code": None, "error_class": None}, "presentation_failed"),
        # A domain class on a 5xx is contradictory: the status wins, so it is a failure.
        ({"status": 500, "code": "X", "error_class": "domain"}, "presentation_failed"),
        ({"status": 401, "code": None, "error_class": None}, "presentation_unauthorized"),
    ],
    ids=[
        "domain_409",
        "validation_400",
        "invariant_500",
        "dependency_503",
        "no_body_502",
        "domain_on_500",
        "unauthorized_401",
    ],
)
def test_hist8_a_presentation_error_is_named_by_its_handler_error_class(
    error: dict[str, object], reason: str
) -> None:
    """G2 (phase-2 E5): a 5xx invariant is a defect, not a governed serving refusal."""
    recorded: dict[str, object] = {"path": "/api/presentation/reach", **error}
    readback = {**_GRAPH_READBACK, "presentation_error": recorded}
    assert gate.presentation_error_reason(recorded) == reason
    verdict = gate.graph_footnote_verdict(
        readback, ["bundle.js"], _GRAPH_PAGE, not_before=_FINALIZE_END
    )
    reasons = cast("list[str]", verdict["reasons"])
    # Exactly one presentation outcome is named, beside an otherwise served footnote.
    assert [item for item in reasons if item in _PRESENTATION_REASONS] == [reason]
    assert reasons == [reason]
    assert verdict["disposition"] == "NOT_PROVEN"


def _batch_span(batch: str, **overrides: object) -> dict[str, object]:
    """One batch's root span document: its own trace, its `batch_id` attribute."""
    document: dict[str, object] = {
        "name": gate.GRAPH_UNIT_BATCH_SPAN,
        "traceId": f"trace-{batch}",
        "spanId": f"span-{batch}",
        "attributes": {"batch_id": batch},
    }
    document.update(overrides)
    return document


def _unit_span(batch: str, index: int, **overrides: object) -> dict[str, object]:
    """One unit span document for member `node-<index>`, a child of `batch`'s span (D37)."""
    document: dict[str, object] = {
        "name": gate.GRAPH_UNIT_SPAN,
        "traceId": f"trace-{batch}",
        "spanId": f"unit-{index}",
        "parentSpanId": f"span-{batch}",
        "attributes": {"batch_id": batch, "node_id": f"node-{index}"},
    }
    document.update(overrides)
    return document


def _batch_retrieval(*spans: dict[str, object]) -> dict[str, object]:
    return {"status": "complete", "truncated": False, "spans": list(spans)}


def _spans(units: int = 2, **extra: object) -> dict[str, object]:
    """Return the run's retrieval, unit `i` having run in `batch-a` or `batch-b` alternately."""
    others = [{"name": "ingest.graph_materialize"}]
    others += [{"name": "ingest.graph_publish"}, *[{"name": "graph.post_grant_consume"}] * 2]
    unit_spans = [_unit_span(("batch-a", "batch-b")[index % 2], index) for index in range(units)]
    return {
        "status": "complete",
        "truncated": False,
        "spans": [*others[:1], *unit_spans, *others[1:]],
        **extra,
    }


def test_s6p2_one_span_per_node_by_run_id_and_pending_is_never_a_pass() -> None:
    query = gate.run_span_query(
        "dev",
        "a" * 64,
        window_start=_datetime(2026, 10, 10, tzinfo=_UTC),
        window_end=_datetime(2026, 10, 10, 1, tzinfo=_UTC),
    )
    assert ("run_id", "a" * 64) in tuple(query.keys)
    assert set(query.span_names) == set(gate.GRAPH_CHAIN_SPANS)
    assert gate.span_verdict(_spans(), 2)["passed"] is True
    assert gate.span_verdict(_spans(units=1), 2)["passed"] is False
    assert gate.span_verdict(_spans(status="pending"), 2)["passed"] is False
    assert gate.span_verdict(_spans(truncated=True), 2)["passed"] is False
    # Conversion 9 (slice 7, AR5-1): the set gains the batch span, one per distinct batch_id,
    # retrieved by batch_id (the run's retrieval holds none of it).
    assert gate.GRAPH_UNIT_BATCH_SPAN in gate.GRAPH_CHAIN_SPANS
    with_batch = _span_verdict_for_a({"batch-a": _batch_retrieval(_batch_span("batch-a"))})
    assert cast("dict[str, int]", with_batch["expected"])[gate.GRAPH_UNIT_BATCH_SPAN] == 1
    assert cast("dict[str, int]", with_batch["counts"])[gate.GRAPH_UNIT_BATCH_SPAN] == 1
    assert with_batch["passed"] is True


def _span_verdict_for_a(batch_spans: dict[str, dict[str, object]]) -> dict[str, object]:
    return gate.span_verdict(_spans(), 2, batch_ids=("batch-a",), batch_spans=batch_spans)


def test_slice7_h1_span_counts_are_the_retrieved_batch_spans_not_the_batch_count() -> None:
    """FEAT-009 H1: two retrieved spans for one batch are counted as two, and fail."""
    doubled = _span_verdict_for_a(
        {"batch-a": _batch_retrieval(_batch_span("batch-a"), _batch_span("batch-a"))}
    )
    assert cast("dict[str, int]", doubled["counts"])[gate.GRAPH_UNIT_BATCH_SPAN] == 2
    assert doubled["passed"] is False
    # A target batch with no retrieval counts zero against its expected one.
    absent = _span_verdict_for_a({})
    assert cast("dict[str, int]", absent["counts"])[gate.GRAPH_UNIT_BATCH_SPAN] == 0
    assert absent["passed"] is False
    # A truncated batch retrieval is not a complete count.
    truncated = _span_verdict_for_a(
        {"batch-a": {**_batch_retrieval(_batch_span("batch-a")), "truncated": True}}
    )
    assert truncated["passed"] is False
    # Control: exactly one retrieved span per target batch passes.
    single = _span_verdict_for_a({"batch-a": _batch_retrieval(_batch_span("batch-a"))})
    assert single["passed"] is True


def _projection_rows() -> list[_Any]:
    base = {"root_flow_id": "root-p6e", "authority_epoch": 1}
    items = [
        {
            **base,
            "pk": "RUN#run-p6e",
            "sk": "NODE#publish",
            "item_kind": "node",
            "state": "SUCCEEDED",
            "armed_at": "2026-10-10T00:00:00+00:00",
            "settled_at": "2026-10-10T00:00:05+00:00",
        },
        {
            **base,
            "pk": "EXEC_CLAIM#id-publish",
            "sk": "CLAIM",
            "item_kind": "claim",
            "node": "publish",
            "admission": 1,
            "queue_enqueued_at": "2026-10-10T00:00:01+00:00",
            "admitted_at": "2026-10-10T00:00:02+00:00",
        },
    ]
    return [project_graph_item(item) for item in items]


def test_s6p2_one_measurement_per_terminal_node_and_claim_and_a_missing_one_fails() -> None:
    rows = _projection_rows()
    ids = {measurement.measurement_id for row in rows if (measurement := row_measurement(row))}
    verdict = gate.measurement_coverage(rows, ids.__contains__)
    assert verdict["passed"] is True
    assert verdict["expected"] == {"FlowTransitionLatency": 1, "ActivityAdmissionElapsed": 1}
    # Control: one contribution absent from the status table fails.
    missing = gate.measurement_coverage(rows, lambda measurement_id: measurement_id != min(ids))
    assert missing["passed"] is False and missing["missing"] == [min(ids)]


def _node_row(node: str, *, armed_at: str | None) -> _Any:
    item: dict[str, object] = {
        "root_flow_id": "root-p6e",
        "authority_epoch": 1,
        "pk": "RUN#run-p6e",
        "sk": f"NODE#{node}",
        "item_kind": "node",
        "node": node,
        "state": "SUCCEEDED",
        "settled_at": "2026-10-10T00:00:09+00:00",
    }
    if armed_at is not None:
        item["armed_at"] = armed_at
    return project_graph_item(item)


def test_p6e7_latency_is_owed_per_armed_node_and_unarmed_grant_apply_is_excluded() -> None:
    """P6E-7: `grant_apply` (settled inline, never armed) leaves the latency denominator."""
    rows = [*_projection_rows(), _node_row("grant_apply", armed_at=None)]
    ids = {measurement.measurement_id for row in rows if (measurement := row_measurement(row))}
    verdict = gate.measurement_coverage(rows, ids.__contains__)
    assert verdict["passed"] is True
    assert (verdict["terminal_nodes"], verdict["armed_nodes"]) == (2, 1)
    assert verdict["unarmed_by_design"] == ["RUN#run-p6e/NODE#grant_apply"]
    assert verdict["unmeasured"] == []
    # Paired control: a genuinely armed node whose latency is absent still fails.
    armed = [*rows, _node_row("bake", armed_at="2026-10-10T00:00:06+00:00")]
    bake = row_measurement(armed[-1])
    assert bake is not None
    absent = gate.measurement_coverage(armed, ids.__contains__)
    assert absent["passed"] is False and absent["missing"] == [bake.measurement_id]
    # Control: any other node without `armed_at` is unmeasured, never excluded.
    unarmed = gate.measurement_coverage([*rows, _node_row("bake", armed_at=None)], ids.__contains__)
    assert unarmed["passed"] is False
    assert unarmed["unmeasured"] == ["RUN#run-p6e/NODE#bake"]
    # Control: an armed grant_apply is measured like any node; its absent latency fails.
    granted = [*_projection_rows(), _node_row("grant_apply", armed_at="2026-10-10T00:00:06+00:00")]
    owed = row_measurement(granted[-1])
    assert owed is not None
    late = gate.measurement_coverage(granted, ids.__contains__)
    assert late["passed"] is False and late["missing"] == [owed.measurement_id]


def test_s6p2_the_explanation_dlq_and_flip_order_verdicts_and_their_controls() -> None:
    applied = {
        "stages": [{"stage": "materialized", "status": "APPLIED"}],
        "missing_required_stages": [],
    }
    assert gate.explanation_materialized_verdict(applied)["passed"] is True
    assert (
        gate.explanation_materialized_verdict(
            {**applied, "missing_required_stages": ["materialized"]}
        )["passed"]
        is False
    )
    queues = {
        name: {"visible": count, "in_flight": 0, "latest_move_task": {"status": "COMPLETED"}}
        for name, count in gate.EXPECTED_DLQ_VISIBLE.items()
    }
    assert gate.dlq_verdict({"queues": queues})["passed"] is True
    bake = queues["bake"]
    extra = gate.dlq_verdict({"queues": {**queues, "bake": {**bake, "visible": 1}}})
    assert extra["passed"] is False
    # Controls: the expected visible counts with an in-flight dead letter, or with a move task
    # still running or cancelling, are not a pass.
    in_flight = gate.dlq_verdict({"queues": {**queues, "bake": {**bake, "in_flight": 1}}})
    assert in_flight["passed"] is False
    assert cast("dict[str, object]", in_flight["in_flight"])["bake"] == 1
    for status in ("RUNNING", "CANCELLING"):
        moving = {**bake, "latest_move_task": {"status": status}}
        verdict = gate.dlq_verdict({"queues": {**queues, "bake": moving}})
        assert (verdict["passed"], verdict["active_moves"]) == (False, ["bake"])
    no_task = {name: {**queue, "latest_move_task": None} for name, queue in queues.items()}
    assert gate.dlq_verdict({"queues": no_task})["passed"] is True
    # An unread in-flight count is not zero.
    unread = {name: {"visible": queue["visible"]} for name, queue in queues.items()}
    assert gate.dlq_verdict({"queues": unread})["passed"] is False
    assert (
        gate.flip_order_verdict("2026-10-10T00:00:03+00:00", "2026-10-10T00:00:05+00:00")["passed"]
        is True
    )
    assert (
        gate.flip_order_verdict("2026-10-10T00:00:06+00:00", "2026-10-10T00:00:05+00:00")["passed"]
        is False
    )


def test_s6p2_the_daily_burst_bound_is_the_protocol_ceiling_and_labelled_inferred() -> None:
    assert BOUNDS["protocol_ceiling_seconds"] == gate.DAILY_BURST_CEILING_SECONDS
    duration = gate.serving_duration_seconds(
        "2026-10-10T00:00:00+00:00", "2026-10-10T00:05:00+00:00"
    )
    assert duration["to_completion_upper_bound_seconds"] == 360.0
    within = gate.inferred_serial_bound(10, 360.0, 900.0)
    assert within == {
        "windows_per_cycle": 10,
        "burst_seconds": 4_500.0,
        "ceiling_seconds": 7_200,
        "inferred": True,
        "informational_only": True,
    }
    # The stale serial model has no `passed` field even when it exceeds the ceiling.
    outside = gate.inferred_serial_bound(30, 360.0, 900.0)
    assert outside["burst_seconds"] == 11_700.0
    assert "passed" not in within and "passed" not in outside


_ROOT_CLOCK = "2026-10-10T00:00:00+00:00"


def _observed_run(  # noqa: PLR0913 -- one fixture builder
    index: int, cohort: str, *, direct: bool, roots: tuple[str, ...]
) -> _Any:
    return gate.ObservedCohortRun(
        run_id=f"{cohort}-run-{index:02d}",
        root_flow_id=f"{cohort}-root-{index:02d}",
        cohort_revision=cohort,
        binding_id=f"{cohort}-binding-{index:02d}",
        winner_run_id=f"{cohort}-run-00",
        publication_effect_digest=f"effect-{index}",
        pointer_generation="7",
        root_clock_started_at=_ROOT_CLOCK,
        publish_flipped_at="2026-10-10T00:03:00+00:00",
        publish_settled_at="2026-10-10T00:03:01+00:00",
        terminal_at="2026-10-10T00:20:00+00:00",
        state="SUCCEEDED",
        serving_outcome="complete",
        binding_kind="direct" if direct else "alias",
        execution_attempts=1 if direct else 0,
        contributor_roots=roots if direct else (),
        direct_binding_id=None if direct else f"{cohort}-binding-00",
    )


_FINALIZED_AT = "2026-10-10T00:15:00+00:00"


def _observed_cohort(*cohorts: tuple[str, int]) -> gate.ObservedCohortEvidence:
    """Each `(cohort, size)` is one direct plus `size - 1` exact aliases, in the runtime's shape.

    Only `drain_final_alias_and_activate_next` writes a `SERVING_TRANSITION#`, when a cohort's
    last alias drains: each alias cohort elects the next one listed, the last elects none
    (`NO_ELIGIBLE`), and a direct-only cohort has no transition. The head read follows every
    finalize and names no observed cohort ACTIVE.
    """
    runs: list[_Any] = []
    bakes: list[_Any] = []
    transitions: list[_Any] = []
    chosen = cohorts or (("cohort-a", 3),)
    alias_cohorts = [cohort for cohort, size in chosen if size > 1]
    for cohort, size in chosen:
        roots = tuple(f"{cohort}-root-{index:02d}" for index in range(size))
        runs.extend(
            _observed_run(index, cohort, direct=index == 0, roots=roots) for index in range(size)
        )
        bakes.append(
            gate.ObservedBakeChain(
                cohort,
                f"{cohort}-run-00",
                ("fanned_out",),
                ("graph_bake_finalized",),
                _FINALIZED_AT,
            )
        )
        if size > 1:
            position = alias_cohorts.index(cohort)
            elected = alias_cohorts[position + 1] if position + 1 < len(alias_cohorts) else None
            transitions.append(
                gate.ObservedPromoterTransition(
                    cohort,
                    elected,
                    "request-1",
                    "2026-10-10T00:20:00+00:00",
                    "ELECTED" if elected else "NO_ELIGIBLE",
                )
            )
    return gate.ObservedCohortEvidence(
        tuple(runs),
        tuple(bakes),
        tuple(transitions),
        (gate.ObservedHeadRead("domain-1", "IDLE", "other", "2026-10-10T00:30:00+00:00"),),
        tuple((cohort, "domain-1") for cohort, _ in chosen),
    )


def test_d7_only_the_graph_bake_run_outcome_counts_as_its_success() -> None:
    evidence = _observed_cohort(("single", 1))
    legacy = gate.ObservedCohortEvidence(
        evidence.runs,
        (
            gate.ObservedBakeChain(
                "single", "single-run-00", ("completed",), ("graph_bake_finalized",)
            ),
        ),
        evidence.transitions,
    )
    verdict = gate.observed_cohort_verdict(legacy)
    assert verdict["passed"] is False
    assert "single: not exactly one successful bake.run" in cast("list[str]", verdict["failures"])


def test_d7_an_exact_single_root_and_a_two_wave_cohort_pass() -> None:
    single = gate.observed_cohort_verdict(_observed_cohort(("single", 1)))
    assert single["passed"] is True and (single["direct"], single["aliases"]) == (1, 0)
    waves = gate.observed_cohort_verdict(_observed_cohort(("wave-a", 3), ("wave-b", 2)))
    assert waves["passed"] is True
    assert waves["cohorts"] == ["wave-a", "wave-b"]
    assert (waves["cycle_seconds"], waves["max_root_seconds"]) == (1_200.0, 1_200.0)


def test_d7_two_directs_a_wrong_alias_or_a_second_bake_fail_the_cohort() -> None:
    valid = _observed_cohort(("wave-a", 3))
    assert gate.observed_cohort_verdict(valid)["passed"] is True
    two_directs = _replace(valid.runs[1], binding_kind="direct", execution_attempts=1)
    assert (
        gate.observed_cohort_verdict(
            _replace(valid, runs=(valid.runs[0], two_directs, valid.runs[2]))
        )["passed"]
        is False
    )
    wrong_alias = _replace(valid.runs[1], direct_binding_id="another-binding")
    verdict = gate.observed_cohort_verdict(
        _replace(valid, runs=(valid.runs[0], wrong_alias, valid.runs[2]))
    )
    assert verdict["passed"] is False
    assert any(
        "does not alias the direct" in item for item in cast("list[str]", verdict["failures"])
    )
    two_bakes = _replace(
        valid,
        bakes=(_replace(valid.bakes[0], bake_finalize_outcomes=("graph_bake_finalized",) * 2),),
    )
    assert gate.observed_cohort_verdict(two_bakes)["passed"] is False
    executed_twice = _replace(valid.runs[0], execution_attempts=2)
    assert (
        gate.observed_cohort_verdict(_replace(valid, runs=(executed_twice, *valid.runs[1:])))[
            "passed"
        ]
        is False
    )


def test_d7_contributors_must_equal_the_grouped_roots_and_flip_precede_settlement() -> None:
    valid = _observed_cohort(("wave-a", 3))
    short = _replace(valid.runs[0], contributor_roots=valid.runs[0].contributor_roots[:2])
    assert (
        gate.observed_cohort_verdict(_replace(valid, runs=(short, *valid.runs[1:])))["passed"]
        is False
    )
    late_flip = _replace(valid.runs[1], publish_flipped_at="2026-10-10T00:03:02+00:00")
    assert (
        gate.observed_cohort_verdict(
            _replace(valid, runs=(valid.runs[0], late_flip, valid.runs[2]))
        )["passed"]
        is False
    )
    equal = _replace(valid.runs[1], publish_flipped_at=valid.runs[1].publish_settled_at)
    assert (
        gate.observed_cohort_verdict(_replace(valid, runs=(valid.runs[0], equal, valid.runs[2])))[
            "passed"
        ]
        is True
    )


def test_d7_malformed_naive_duplicate_or_unmatched_evidence_is_refused() -> None:
    valid = _observed_cohort(("wave-a", 2))
    with pytest.raises(gate.ProbeError, match="neither an alias nor direct"):
        gate.observed_cohort_verdict(
            _replace(valid, runs=(_replace(valid.runs[0], binding_kind=""), valid.runs[1]))
        )
    with pytest.raises(gate.ProbeError, match="UTC"):
        gate.observed_cohort_verdict(
            _replace(
                valid,
                runs=(_replace(valid.runs[0], terminal_at="2026-10-10T00:20:00"), valid.runs[1]),
            )
        )
    with pytest.raises(gate.ProbeError, match="twice"):
        gate.observed_cohort_verdict(_replace(valid, runs=(valid.runs[0], valid.runs[0])))
    with pytest.raises(gate.ProbeError, match="names no direct binding"):
        gate.observed_cohort_verdict(
            _replace(valid, runs=(valid.runs[0], _replace(valid.runs[1], direct_binding_id=None)))
        )
    with pytest.raises(gate.ProbeError, match="unexpected cohort"):
        gate.observed_cohort_verdict(
            _replace(valid, bakes=(*valid.bakes, _replace(valid.bakes[0], cohort_revision="ghost")))
        )
    with pytest.raises(gate.ProbeError, match="before its root clock"):
        gate.observed_cohort_verdict(
            _replace(
                valid,
                runs=(
                    _replace(valid.runs[0], terminal_at="2026-10-09T23:59:59+00:00"),
                    valid.runs[1],
                ),
            )
        )
    with pytest.raises(gate.ProbeError, match="no publication_effect_digest"):
        gate.observed_cohort_verdict(
            _replace(
                valid, runs=(_replace(valid.runs[0], publication_effect_digest=""), valid.runs[1])
            )
        )


def test_d7_a_root_or_cycle_over_7200_seconds_fails_at_the_exact_boundary() -> None:
    valid = _observed_cohort(("wave-a", 2))
    late = _replace(valid.runs[0], terminal_at="2026-10-10T02:00:01+00:00")
    verdict = gate.observed_cohort_verdict(_replace(valid, runs=(late, valid.runs[1])))
    assert verdict["passed"] is False and verdict["max_root_seconds"] == 7_201.0
    boundary = _replace(valid.runs[0], terminal_at="2026-10-10T02:00:00+00:00")
    assert gate.observed_cohort_verdict(_replace(valid, runs=(boundary, valid.runs[1])))["passed"]
    # The cycle spans the union: a later root clock cannot hide an over-ceiling cycle.
    early = _replace(valid.runs[1], root_clock_started_at="2026-10-09T23:59:59+00:00")
    cycle = gate.observed_cohort_verdict(_replace(valid, runs=(boundary, early)))
    assert cycle["passed"] is False and cycle["cycle_seconds"] == 7_201.0


def test_d7_a_missing_promoter_transition_fails_the_cohort() -> None:
    valid = _observed_cohort(("wave-a", 2))
    assert gate.observed_cohort_verdict(_replace(valid, transitions=()))["passed"] is False


def _failures(evidence: gate.ObservedCohortEvidence) -> list[str]:
    verdict = gate.observed_cohort_verdict(evidence)
    assert verdict["passed"] is False
    return cast("list[str]", verdict["failures"])


def test_d7_alias_cohorts_need_one_matching_transition_and_direct_only_none() -> None:
    # The x5 shape: a single root, then A electing B, then B electing none.
    valid = _observed_cohort(("single", 1), ("wave-a", 3), ("wave-b", 2))
    assert gate.observed_cohort_verdict(valid)["passed"] is True
    a, b = valid.transitions
    assert (a.outcome, a.elected_cohort_revision, b.outcome) == ("ELECTED", "wave-b", "NO_ELIGIBLE")
    stray = gate.ObservedPromoterTransition("single", None, "r", _FINALIZED_AT, "NO_ELIGIBLE")
    assert "single: direct-only cohort has 1 promoter transitions" in _failures(
        _replace(valid, transitions=(*valid.transitions, stray))
    )
    assert "wave-a: 0 promoter transitions, expected exactly 1" in _failures(
        _replace(valid, transitions=(b,))
    )
    assert "wave-a: 2 promoter transitions, expected exactly 1" in _failures(
        _replace(valid, transitions=(a, a, b))
    )
    mismatched = _replace(a, outcome="NO_ELIGIBLE")
    assert any(
        "does not match its election" in item
        for item in _failures(_replace(valid, transitions=(mismatched, b)))
    )


def test_d7_a_direct_only_cohort_must_be_released_after_one_finalize() -> None:
    valid = _observed_cohort(("single", 1))
    still_active = gate.ObservedHeadRead(
        "domain-1", "ACTIVE", "single", "2026-10-10T00:30:00+00:00"
    )
    assert "single: direct-only cohort is still ACTIVE after its finalize" in _failures(
        _replace(valid, heads=(still_active,))
    )
    early = gate.ObservedHeadRead("domain-1", "IDLE", "other", "2026-10-10T00:14:59+00:00")
    assert "single: the head read precedes the cohort's finalize" in _failures(
        _replace(valid, heads=(early,))
    )
    no_finalize = _replace(valid.bakes[0], bake_finalize_outcomes=(), finalize_ended_at=None)
    missing = _failures(_replace(valid, bakes=(no_finalize,)))
    assert "single: not exactly one successful bake.finalize" in missing
    assert "single: direct-only cohort has no successful finalize instant" in missing
    failed = _replace(valid.bakes[0], bake_finalize_outcomes=("graph_bake_failed",))
    assert "single: not exactly one successful bake.finalize" in _failures(
        _replace(valid, bakes=(failed,))
    )
    assert "single: direct-only cohort has no head read" in _failures(_replace(valid, heads=()))


class _NotVisibleCloudWatch:
    def __init__(self, datapoints: list[dict[str, object]]) -> None:
        self.datapoints = datapoints
        self.requests: list[dict[str, object]] = []

    def get_metric_statistics(self, **request: object) -> dict[str, object]:
        self.requests.append(dict(request))
        return {"Datapoints": self.datapoints}


class _NotVisibleSession:
    def __init__(self, cloudwatch: _NotVisibleCloudWatch) -> None:
        self.cloudwatch = cloudwatch

    def client(self, name: str) -> _Any:
        assert name == "cloudwatch"
        return self.cloudwatch


def test_p6e_reads_delivery_not_visible_maximum_over_the_run_window() -> None:
    cloudwatch = _NotVisibleCloudWatch([{"Maximum": 2.0}, {"Maximum": 7.0}])
    reads = gate.BotoP6eReads(
        _NotVisibleSession(cloudwatch), "dev", "graph", "status", "publications", None
    )
    start = _datetime(2026, 10, 10, tzinfo=_UTC)
    end = start + _timedelta(minutes=20)

    maximum = reads.delivery_not_visible(start, end)

    assert maximum == 7.0
    assert cloudwatch.requests == [
        {
            "Namespace": "AWS/SQS",
            "MetricName": "ApproximateNumberOfMessagesNotVisible",
            "Dimensions": [{"Name": "QueueName", "Value": "adlytics-dev-ingestion"}],
            "StartTime": start,
            "EndTime": end,
            "Period": 60,
            "Statistics": ["Maximum"],
        }
    ]
    assert gate.delivery_not_visible_verdict(maximum)["passed"] is True
    # Paired control: no in-window datapoint is not silently treated as zero.
    empty = gate.BotoP6eReads(
        _NotVisibleSession(_NotVisibleCloudWatch([])),
        "dev",
        "graph",
        "status",
        "publications",
        None,
    )
    assert (
        gate.delivery_not_visible_verdict(empty.delivery_not_visible(start, end))["passed"] is False
    )


def test_s6p2_a_binding_before_publish_settles_fails_the_chronology_check() -> None:
    settled, bound = "2026-10-10T00:03:00+00:00", "2026-10-10T00:06:00+00:00"
    assert gate.serving_chronology_verdict(settled, bound)["passed"] is True
    assert gate.serving_chronology_verdict(settled, settled)["passed"] is True
    # Control: the mirror-image order is malformed evidence, not a shorter interval.
    assert gate.serving_chronology_verdict(bound, settled)["passed"] is False


@_dataclass
class _P6eReads:
    measured_ids: set[str]
    binding_present: bool = True
    bound_at: str = "2026-10-10T00:06:00+00:00"
    unit_b_batch: str | None = "batch-b"
    unit_b_node_id: str | None = "node-1"
    open_members: int = 0
    pointer_grant: bool = True

    def run(self, run_id: str) -> dict[str, object]:
        del run_id
        return {
            "grant_id": "g-1",
            "conflict_unit": "u-1",
            "root_flow_id": "root-p6e",
            "root_clock_started_at": "2026-10-10T00:00:00+00:00",
            "terminal_at": "2026-10-10T00:07:00+00:00",
            "state": "SUCCEEDED",
            "serving_outcome": "complete",
        }

    def nodes(self, run_id: str) -> list[dict[str, object]]:
        del run_id
        return [
            {
                "node": "write_unit:db.a",
                "node_id": "node-0",
                "settled_at": "2026-10-10T00:01:00+00:00",
                "batch_id": "batch-a",
                "lock_key": "TABLE#db.a",
            },
            {
                "node": "write_unit:db.b",
                "node_id": self.unit_b_node_id,
                "settled_at": "2026-10-10T00:01:00+00:00",
                "batch_id": self.unit_b_batch,
                "lock_key": "TABLE#db.b",
            },
            {"node": "grant_apply", "settled_at": "2026-10-10T00:02:00+00:00"},
            {
                "node": "publish",
                "settled_at": "2026-10-10T00:03:00+00:00",
                "effect_digest": "effect-p6e",
            },
        ]

    def binding(self, grant_id: str) -> dict[str, object] | None:
        del grant_id
        return (
            {
                "bound_at": self.bound_at,
                "binding_id": "binding-p6e",
                "binding_kind": "direct",
                "winner_run_id": "run-p6e",
                "cohort_revision": "cohort-p6e",
                "domain": "domain-p6e",
                "execution_attempts": 1,
                "contributor_roots": ["root-p6e"],
            }
            if self.binding_present
            else None
        )

    def pointer(self, conflict_unit: str) -> dict[str, object] | None:
        del conflict_unit
        # The real `GENERATION#<U>/POINTER` shape: a grant, never a `serving_generation`.
        if not self.pointer_grant:
            return {"flipped_at": "2026-10-10T00:02:59+00:00"}
        return {"flipped_at": "2026-10-10T00:02:59+00:00", "grant_id": "g-1"}

    def projection_rows(self, root_flow_id: str) -> list[_Any]:
        del root_flow_id
        return _projection_rows()

    def measured(self, measurement_id: str) -> bool:
        return measurement_id in self.measured_ids

    def spans(
        self, run_id: str, window_start: _datetime, window_end: _datetime
    ) -> dict[str, object]:
        del run_id, window_start, window_end
        return _spans()

    def explanation(self, root_flow_id: str) -> dict[str, object]:
        del root_flow_id
        return {
            "stages": [
                {"stage": "materialized", "status": "APPLIED", "batch_id": None},
                {"stage": "unit_claimed", "status": "APPLIED", "batch_id": "batch-a"},
                {"stage": "unit_attested", "status": "APPLIED", "batch_id": "batch-a"},
            ],
            "missing_required_stages": [],
        }

    def batch_spans(
        self, batch_id: str, window_start: _datetime, window_end: _datetime
    ) -> dict[str, object]:
        del window_start, window_end
        return _batch_retrieval(_batch_span(batch_id))

    def table_lock(self, lock_key: str) -> dict[str, object] | None:
        return {"pk": f"LOCK#{lock_key}", "open_members": self.open_members}

    def batch_records(self, window_start: _datetime, window_end: _datetime) -> dict[str, object]:
        del window_start, window_end
        return {
            "sets": [_set_record("batch-a"), _set_record("batch-b")],
            "metrics": {
                batch: {name: [0.0] for name in gate.BATCH_METRICS}
                for batch in ("batch-a", "batch-b")
            },
            "budget": {"semantic_statement_budget": 0, "semantic_attempt_parked": 0},
        }

    def dlq_status(self) -> dict[str, object]:
        return {
            "queues": {
                name: {"visible": count, "in_flight": 0, "latest_move_task": None}
                for name, count in gate.EXPECTED_DLQ_VISIBLE.items()
            }
        }

    def bake_spans(
        self, run_id: str, window_start: _datetime, window_end: _datetime
    ) -> dict[str, object]:
        del run_id, window_start, window_end
        return {
            "status": "complete",
            "truncated": False,
            "spans": [
                {
                    "name": name,
                    "endTimeUnixNano": "1791590400000000000",
                    "attributes": {"cohort_revision": "cohort-p6e", "outcome": outcome},
                }
                for name, outcome in (
                    ("bake.run", "fanned_out"),
                    ("bake.finalize", "graph_bake_finalized"),
                )
            ],
        }

    def serving_transition(self, domain: str, cohort_revision: str) -> dict[str, object] | None:
        # A direct-only cohort: the runtime writes no transition (no last alias drains).
        assert (domain, cohort_revision) == ("domain-p6e", "cohort-p6e")
        return None

    def serving_head(self, domain: str) -> gate.ObservedHeadRead:
        return gate.ObservedHeadRead(domain, "IDLE", "cohort-next", "2026-10-10T00:30:00+00:00")

    def delivery_not_visible(self, window_start: _datetime, window_end: _datetime) -> float | None:
        del window_start, window_end
        return 2.0


def _p6e(reads: _P6eReads) -> dict[str, object]:
    evidence = gate.gather_p6e(
        reads,
        "run-p6e",
        window_start=_datetime(2026, 10, 10, tzinfo=_UTC),
        window_end=_datetime(2026, 10, 10, 1, tzinfo=_UTC),
        windows_per_cycle=4,
    )
    return gate.p6e_verdict(evidence)


def test_p6e_derives_the_pointer_generation_from_the_pointers_grant() -> None:
    from domain.operations.execution_graph_release import serving_generation  # noqa: PLC0415

    evidence = gate.gather_p6e(
        _P6eReads(set()),
        "run-p6e",
        window_start=_datetime(2026, 10, 10, tzinfo=_UTC),
        window_end=_datetime(2026, 10, 10, 1, tzinfo=_UTC),
        windows_per_cycle=4,
    )
    (run,) = evidence.observed_cohort.runs
    assert run.pointer_generation == serving_generation("g-1")
    # Paired control: a pointer naming no grant proves no generation.
    with pytest.raises(gate.ProbeError, match="pointer carries no grant_id"):
        gate.gather_p6e(
            _P6eReads(set(), pointer_grant=False),
            "run-p6e",
            window_start=_datetime(2026, 10, 10, tzinfo=_UTC),
            window_end=_datetime(2026, 10, 10, 1, tzinfo=_UTC),
            windows_per_cycle=4,
        )


def test_s6p2_p6e_passes_only_when_every_read_passes() -> None:
    ids = {
        measurement.measurement_id
        for row in _projection_rows()
        if (measurement := row_measurement(row))
    }
    verdict = _p6e(_P6eReads(ids))
    assert verdict["passed"] is True
    checks = cast("dict[str, object]", verdict["checks"])
    assert set(checks) == {
        "serving_chronology",
        "batching",
        "spans",
        "measurements",
        "explanation",
        "dlq",
        "flip_order",
        "observed_cohort",
        "delivery_not_visible",
        "completed",
    }
    # Control: one missing measurement fails the whole acceptance.
    assert _p6e(_P6eReads(set()))["passed"] is False
    # Control: malformed serving chronology still fails beside a valid observed cohort interval.
    early = _p6e(_P6eReads(ids, bound_at="2026-10-10T00:02:00+00:00"))
    early_checks = cast("dict[str, dict[str, object]]", early["checks"])
    assert early["passed"] is False
    assert early_checks["serving_chronology"]["passed"] is False
    assert early_checks["observed_cohort"]["passed"] is True
    # Control: a run without its serving binding is a probe error, never a pass.
    with pytest.raises(gate.ProbeError):
        _p6e(_P6eReads(ids, binding_present=False))


def _set_record(batch: str, **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "batch_id": batch,
        "execution_set_id": f"set-{batch}",
        "table": "db.a",
        "statements": 2,
        "rows": 10,
        "execution_ms": 9_000,
        "outcome": "finalized",
        "deadline_cancelled": False,
        "max_partitions_per_merge": 3,
        "members": 1,
    }
    record.update(overrides)
    return record


def _batching(**overrides: object) -> _Any:
    base: dict[str, object] = {
        "batch_ids": ("batch-a",),
        "batch_spans": {"batch-a": _batch_retrieval(_batch_span("batch-a"))},
        "expected_units": (("batch-a", "node-0"),),
        "unit_spans": [_unit_span("batch-a", 0)],
        "metrics": {"batch-a": {name: [0.0] for name in gate.BATCH_METRICS}},
        "set_records": [_set_record("batch-a")],
        "budget_records": {"semantic_statement_budget": 0, "semantic_attempt_parked": 0},
        "table_locks": {"TABLE#db.a": {"open_members": 0}},
        "explanation_stages": [{"stage": "unit_claimed", "batch_id": "batch-a"}],
    }
    base.update(overrides)
    return gate.BatchingEvidence(**cast("dict[str, _Any]", base))


def test_slice7_16_complete_batching_evidence_passes_and_records_the_widest_merge() -> None:
    verdict = gate.batching_verdict(_batching())
    assert (verdict["passed"], verdict["failures"]) == (True, [])
    assert verdict["widest_merge"] == {
        "max_partitions_per_merge": 3,
        "execution_set_id": "set-batch-a",
    }


def test_b1_a_finalized_zero_statement_set_is_invalid() -> None:
    verdict = gate.batching_verdict(_batching(set_records=[_set_record("batch-a", statements=0)]))

    assert verdict == {
        "batches": 1,
        "failures": ["set_record_invalid:batch-a"],
        "rederivation_triggers": [],
        "widest_merge": None,
        "passed": False,
    }


def test_b1_a_complete_one_statement_finalized_set_passes() -> None:
    verdict = gate.batching_verdict(_batching(set_records=[_set_record("batch-a", statements=1)]))

    assert verdict == {
        "batches": 1,
        "failures": [],
        "rederivation_triggers": [],
        "widest_merge": {
            "max_partitions_per_merge": 3,
            "execution_set_id": "set-batch-a",
        },
        "passed": True,
    }


def test_b1_a_slow_finalized_set_still_requires_rederivation() -> None:
    verdict = gate.batching_verdict(
        _batching(
            set_records=[
                _set_record(
                    "batch-a",
                    statements=1,
                    execution_ms=2 * (gate.ALLOWANCE_TRIGGER_MS + 1),
                )
            ]
        )
    )

    assert verdict == {
        "batches": 1,
        "failures": ["allowance_rederivation_required"],
        "rederivation_triggers": ["slow_set:set-batch-a"],
        "widest_merge": {
            "max_partitions_per_merge": 3,
            "execution_set_id": "set-batch-a",
        },
        "passed": False,
    }


@pytest.mark.parametrize(
    ("overrides", "failure"),
    [
        ({"batch_spans": {}}, "batch_span_missing:batch-a"),
        ({"set_records": []}, "set_record_missing:batch-a"),
        ({"table_locks": {"TABLE#db.a": {"open_members": 1}}}, "lock_open:TABLE#db.a"),
        (
            {"budget_records": {"semantic_statement_budget": 0, "semantic_attempt_parked": 1}},
            "allowance_rederivation_required",
        ),
        (
            {"set_records": [_set_record("batch-a", deadline_cancelled=True)]},
            "allowance_rederivation_required",
        ),
        (
            {"set_records": [_set_record("batch-a", execution_ms=3 * 22_194)]},
            "allowance_rederivation_required",
        ),
        (
            {"metrics": {"batch-a": {"GraphUnitBatchSize": [1.0]}}},
            "metric_missing:GraphUnitBatchCommitMs:batch-a",
        ),
        (
            {
                "metrics": {
                    "batch-a": {
                        **{n: [0.0] for n in gate.BATCH_METRICS},
                        "GraphUnitBatchSplits": [2.0],
                    }
                }
            },
            "splits_nonzero:batch-a",
        ),
        (
            {"metrics": {"batch-a": {n: [0.0, 0.0] for n in gate.BATCH_METRICS}}},
            "metric_repeated:GraphUnitBatchSize:batch-a",
        ),
        (
            {"explanation_stages": [{"stage": "unit_claimed", "batch_id": None}]},
            "explanation_batch_missing",
        ),
    ],
)
def test_slice7_16_each_missing_or_triggering_read_fails_the_batching_check(
    overrides: dict[str, object], failure: str
) -> None:
    verdict = gate.batching_verdict(_batching(**overrides))
    assert verdict["passed"] is False
    assert failure in cast("list[str]", verdict["failures"])


def test_slice7_h1_a_repeated_batch_span_is_refused_and_one_span_passes() -> None:
    """FEAT-009 H1 (D37): one batch span per invocation, so two for one batch_id fail."""
    doubled = gate.batching_verdict(
        _batching(
            batch_spans={
                "batch-a": _batch_retrieval(
                    _batch_span("batch-a"), _batch_span("batch-a", spanId="span-again")
                )
            }
        )
    )
    assert (doubled["passed"], doubled["failures"]) == (False, ["batch_span_repeated:batch-a"])
    # Control: the same retrieval with its one span passes.
    single = gate.batching_verdict(
        _batching(batch_spans={"batch-a": _batch_retrieval(_batch_span("batch-a"))})
    )
    assert (single["passed"], single["failures"]) == (True, [])


def test_slice7_h1_a_batch_span_naming_another_batch_never_stands_in() -> None:
    other = gate.batching_verdict(
        _batching(
            batch_spans={
                "batch-a": _batch_retrieval(_batch_span("batch-a", attributes={"batch_id": "z"}))
            }
        )
    )
    assert (other["passed"], other["failures"]) == (False, ["batch_span_missing:batch-a"])
    # Control: the span naming the target batch is found.
    own = gate.batching_verdict(
        _batching(batch_spans={"batch-a": _batch_retrieval(_batch_span("batch-a"))})
    )
    assert (own["passed"], own["failures"]) == (True, [])


def test_slice7_h1_an_explanation_row_naming_another_batch_is_refused() -> None:
    """FEAT-009 H1: a unit-stage row's batch_id must be one of the run's target batches."""
    wrong = gate.batching_verdict(
        _batching(
            explanation_stages=[
                {"stage": "unit_claimed", "batch_id": "batch-a"},
                {"stage": "unit_attested", "batch_id": "batch-z"},
            ]
        )
    )
    assert (wrong["passed"], wrong["failures"]) == (False, ["explanation_batch_foreign:batch-z"])
    # Control: the same rows naming the target batch pass.
    target = gate.batching_verdict(
        _batching(
            explanation_stages=[
                {"stage": "unit_claimed", "batch_id": "batch-a"},
                {"stage": "unit_attested", "batch_id": "batch-a"},
            ]
        )
    )
    assert (target["passed"], target["failures"]) == (True, [])


@pytest.mark.parametrize(
    ("unit", "failures"),
    [
        (_unit_span("batch-a", 0, parentSpanId="span-elsewhere"), ["unit_span_not_nested:unit-0"]),
        (_unit_span("batch-a", 0, parentSpanId=None), ["unit_span_not_nested:unit-0"]),
        (_unit_span("batch-a", 0, traceId="trace-other"), ["unit_span_not_nested:unit-0"]),
        # A foreign span is not the member's span, so the member's own is also missing (J1).
        (
            _unit_span("batch-z", 0),
            ["unit_span_batch_foreign:unit-0", "unit_span_member_missing:batch-a:node-0"],
        ),
    ],
)
def test_slice7_h1_a_unit_span_outside_its_target_batch_span_is_refused(
    unit: dict[str, object], failures: list[str]
) -> None:
    """FEAT-009 H1 (D37): each unit span is a child of its own target batch's one span."""
    verdict = gate.batching_verdict(_batching(unit_spans=[unit]))
    assert (verdict["passed"], verdict["failures"]) == (False, failures)


def _two_members(*unit_spans: dict[str, object]) -> _Any:
    """Batching evidence for one batch holding members node-0 and node-1."""
    return _batching(
        expected_units=(("batch-a", "node-0"), ("batch-a", "node-1")),
        unit_spans=list(unit_spans),
    )


def test_slice7_j1_one_members_two_spans_never_stand_in_for_anothers_absent_span() -> None:
    """FEAT-009 J1 (D37, AR5-1): spans bind to (batch_id, node_id), not to a total count."""
    substituted = gate.batching_verdict(
        _two_members(
            _unit_span("batch-a", 0),
            _unit_span("batch-a", 0, spanId="unit-0-again"),
        )
    )
    assert (substituted["passed"], substituted["failures"]) == (
        False,
        ["unit_span_member_repeated:batch-a:node-0", "unit_span_member_missing:batch-a:node-1"],
    )
    # Control: one correctly nested span per member passes.
    each = gate.batching_verdict(_two_members(_unit_span("batch-a", 0), _unit_span("batch-a", 1)))
    assert (each["passed"], each["failures"]) == (True, [])


def test_slice7_j1_a_target_batch_span_naming_no_expected_member_is_extra() -> None:
    extra = gate.batching_verdict(
        _two_members(
            _unit_span("batch-a", 0),
            _unit_span("batch-a", 1),
            _unit_span("batch-a", 2),
        )
    )
    assert (extra["passed"], extra["failures"]) == (False, ["unit_span_member_extra:unit-2"])
    # A span carrying no node_id names no member, so it is extra and its member is missing.
    unnamed = gate.batching_verdict(
        _two_members(
            _unit_span("batch-a", 0),
            _unit_span("batch-a", 1, attributes={"batch_id": "batch-a"}),
        )
    )
    assert (unnamed["passed"], unnamed["failures"]) == (
        False,
        ["unit_span_member_extra:unit-1", "unit_span_member_missing:batch-a:node-1"],
    )
    # Control: the same two members' own spans, and nothing else, pass.
    exact = gate.batching_verdict(_two_members(_unit_span("batch-a", 1), _unit_span("batch-a", 0)))
    assert (exact["passed"], exact["failures"]) == (True, [])


def test_slice7_j1_a_member_span_under_the_wrong_batch_is_not_that_members_span() -> None:
    """A member's span must name the member's own batch; another target batch never counts."""
    evidence = _batching(
        batch_ids=("batch-a", "batch-b"),
        batch_spans={
            batch: _batch_retrieval(_batch_span(batch)) for batch in ("batch-a", "batch-b")
        },
        expected_units=(("batch-a", "node-0"), ("batch-b", "node-1")),
        unit_spans=[_unit_span("batch-a", 0), _unit_span("batch-a", 1)],
        metrics={
            batch: {name: [0.0] for name in gate.BATCH_METRICS} for batch in ("batch-a", "batch-b")
        },
        set_records=[_set_record("batch-a"), _set_record("batch-b")],
    )
    moved = gate.batching_verdict(evidence)
    assert (moved["passed"], moved["failures"]) == (
        False,
        ["unit_span_member_extra:unit-1", "unit_span_member_missing:batch-b:node-1"],
    )
    # Control: node-1's span under its own batch passes.
    own = gate.batching_verdict(
        _replace(evidence, unit_spans=[_unit_span("batch-a", 0), _unit_span("batch-b", 1)])
    )
    assert (own["passed"], own["failures"]) == (True, [])


def test_slice7_h1_a_nested_unit_span_passes_and_none_at_all_is_refused() -> None:
    nested = gate.batching_verdict(_batching(unit_spans=[_unit_span("batch-a", 0)]))
    assert (nested["passed"], nested["failures"]) == (True, [])
    # No unit span retrieved is never a pass.
    none = gate.batching_verdict(_batching(unit_spans=[]))
    assert (none["passed"], none["failures"]) == (False, ["unit_span_missing"])


def test_slice7_h1_p6e_judges_the_run_unit_spans_against_their_batch_spans() -> None:
    """gather_batching carries the run retrieval's unit spans into the batching verdict."""
    ids = {
        measurement.measurement_id
        for row in _projection_rows()
        if (measurement := row_measurement(row))
    }
    evidence = gate.gather_p6e(
        _P6eReads(ids),
        "run-p6e",
        window_start=_datetime(2026, 10, 10, tzinfo=_UTC),
        window_end=_datetime(2026, 10, 10, 1, tzinfo=_UTC),
        windows_per_cycle=4,
    )
    assert evidence.batching is not None
    assert [span["spanId"] for span in evidence.batching.unit_spans] == ["unit-0", "unit-1"]
    # FEAT-009 J1: the expected members come from the run's write_unit NODE rows.
    assert evidence.batching.expected_units == (("batch-a", "node-0"), ("batch-b", "node-1"))
    assert gate.p6e_verdict(evidence)["passed"] is True


def test_slice7_f6_another_batchs_samples_never_mask_a_missing_target_metric() -> None:
    """FEAT-009 review F6: metrics are judged per target batch, by their `batch_id` property."""
    masked = gate.batching_verdict(
        _batching(
            metrics={
                "batch-a": {"GraphUnitBatchCommitMs": [0.0], "GraphUnitBatchSplits": [0.0]},
                "batch-z": {name: [0.0, 0.0, 0.0] for name in gate.BATCH_METRICS},
            }
        )
    )
    assert masked["passed"] is False
    assert masked["failures"] == ["metric_missing:GraphUnitBatchSize:batch-a"]
    # Control: the target's own complete samples pass beside the unrelated batch's.
    own = gate.batching_verdict(
        _batching(
            metrics={
                "batch-a": {name: [0.0] for name in gate.BATCH_METRICS},
                "batch-z": {name: [0.0, 0.0, 0.0] for name in gate.BATCH_METRICS},
            }
        )
    )
    assert (own["passed"], own["failures"]) == (True, [])


def test_slice7_f6_emf_samples_are_credited_only_to_the_batch_they_name() -> None:
    import io  # noqa: PLC0415
    from contextlib import redirect_stdout  # noqa: PLC0415

    from aws_lambda_powertools.metrics import MetricUnit, single_metric  # noqa: PLC0415

    printed = io.StringIO()
    with redirect_stdout(printed):
        for batch, value in (("batch-a", 3), ("batch-z", 5), (None, 7)):
            with single_metric(
                name="GraphUnitBatchSize", unit=MetricUnit.Count, value=value, namespace="Adlytics"
            ) as metric:
                if batch is not None:
                    metric.add_metadata(key="batch_id", value=batch)
    parsed = gate.parse_batch_records(printed.getvalue().splitlines())
    # The unattributed sample (7) is credited to no batch.
    assert parsed["metrics"] == {
        "batch-a": {"GraphUnitBatchSize": [3.0]},
        "batch-z": {"GraphUnitBatchSize": [5.0]},
    }


class _BatchRecordLogs:
    """A Logs Insights fake answering one complete query with the given rows and statistics."""

    def __init__(
        self, messages: list[str], statistics: object, next_token: str | None = None
    ) -> None:
        self.result: dict[str, object] = {
            "status": "Complete",
            "results": [[{"field": "@message", "value": message}] for message in messages],
        }
        if statistics is not None:
            self.result["statistics"] = statistics
        if next_token is not None:
            self.result["nextToken"] = next_token
        self.started: list[dict[str, object]] = []

    def start_query(self, **request: object) -> dict[str, str]:
        self.started.append(dict(request))
        return {"queryId": "q"}

    def get_query_results(self, **request: object) -> dict[str, object]:
        del request
        return self.result


# The live layout (P6E-2): `report_operation` writes an event's details at the top level.
_SET_LINE = json.dumps(
    {"phase": gate.SET_RECORD_PHASE, "outcome": "succeeded", "batch_id": "batch-a"}
)
_BUDGET_LINE = json.dumps(
    {
        "operation": "semantic_statement_budget",
        "outcome": "insufficient",
        "writer_kind": "graph_unit",
    }
)
# A `graph_unit_set_executed` line as TableWriter wrote it on dev, 2026-10-11T15:23:06.288Z
# (`s6p2/evidence/graph-switch/step4/tw-set-records.json`); only the ARN's account is replaced
# by the documentation placeholder.
_LIVE_SET_LINE = (
    '{"level":"INFO","location":"report_operation:766","message":"operation lifecycle",'
    '"timestamp":"2026-10-11 15:23:06,288+0000","service":"adlytics-table-writer",'
    '"cold_start":true,"function_name":"adlytics-dev-TableWriter",'
    '"function_memory_size":"2048",'
    '"function_arn":"arn:aws:lambda:us-east-1:123456789012:function:adlytics-dev-TableWriter",'
    '"function_request_id":"770e0ad4-9c18-53cc-8d9b-6991301ac3d5",'
    '"operation":"semantic_table_writer","component":"table_writer",'
    '"phase":"graph_unit_set_executed","outcome":"finalized",'
    '"batch_id":"c84fbc86fb7cb46d61825d4b83cedbc1d76ab8aaf77055497ada736435a2fe0f",'
    '"execution_set_id":"7f765f17cdb019c81da473bb9b041c8ac7af960f4254accddb2c79d997833d75",'
    '"table":"adlytics_dev_v2_source_aligned.sa_amc_dsp_impressions","statements":3,'
    '"rows":91,"execution_ms":12637,"deadline_cancelled":false,"max_partitions_per_merge":1,'
    '"members":1,"trace_id":"d6ba1cbdf4751c93bc8e577ec724ea01","span_id":"4bd3f8eb90558821",'
    '"xray_trace_id":"1-6acba9ca-a7c6ab1a8c0fd9781d0e1095"}'
)


def _reported_lines(*events: tuple[str, str, str]) -> list[str]:
    """Emit (operation, outcome, writer_kind) events through the real `report_operation`."""
    import io  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from aws_lambda_powertools import Logger  # noqa: PLC0415

    from adapters.outbound.telemetry.reporting import (  # noqa: PLC0415
        LogSeverity,
        OperationLog,
        report_operation,
    )

    printed = io.StringIO()
    # Powertools keeps the first handler of a service name: once table_writer's module LOGGER has
    # registered "adlytics-table-writer" on stdout, `stream=` is ignored and nothing is captured.
    logger = Logger(service=f"adlytics-table-writer-{uuid.uuid4().hex}", stream=printed)
    for operation, outcome, writer_kind in events:
        report_operation(
            OperationLog(
                operation=operation,
                component="inline_run",
                phase="run",
                outcome=outcome,
                severity=LogSeverity.WARNING,
                details={"writer_kind": writer_kind, "run_id": "run-a"},
            ),
            logger,
        )
    return printed.getvalue().splitlines()


def test_p6e2_the_live_set_line_is_read_as_its_batchs_set_record() -> None:
    """P6E-2: the live line's fields sit at the top level; the old parser dropped the line."""
    parsed = gate.parse_batch_records([_LIVE_SET_LINE])
    # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
    batch = "c84fbc86fb7cb46d61825d4b83cedbc1d76ab8aaf77055497ada736435a2fe0f"
    assert parsed["sets"] == [
        {
            "batch_id": batch,
            "execution_set_id": "7f765f17cdb019c81da473bb9b041c8ac7af960f4254accddb2c79d997833d75",
            "table": "adlytics_dev_v2_source_aligned.sa_amc_dsp_impressions",
            "statements": 3,
            "rows": 91,
            "execution_ms": 12637,
            "deadline_cancelled": False,
            "max_partitions_per_merge": 1,
            "members": 1,
            "outcome": "finalized",
        }
    ]
    verdict = gate.batching_verdict(
        _batching(
            batch_ids=(batch,),
            batch_spans={batch: _batch_retrieval(_batch_span(batch))},
            expected_units=((batch, "node-0"),),
            set_records=cast("list[dict[str, object]]", parsed["sets"]),
        )
    )
    failures = cast("list[str]", verdict["failures"])
    assert not any(failure.startswith("set_record_") for failure in failures)
    # Control: without the line the same batch has no set record.
    missing = gate.batching_verdict(
        _batching(
            batch_ids=(batch,),
            batch_spans={batch: _batch_retrieval(_batch_span(batch))},
            expected_units=((batch, "node-0"),),
            set_records=[],
        )
    )
    assert f"set_record_missing:{batch}" in cast("list[str]", missing["failures"])


def test_fix_review_f1_a_partial_set_line_fails_the_gate_instead_of_clearing_missing() -> None:
    """F1: a line naming only the phase and the target batch proves no timing or outcome."""
    partial = json.dumps({"phase": gate.SET_RECORD_PHASE, "batch_id": "batch-a"})
    parsed = gate.parse_batch_records([partial])
    verdict = gate.batching_verdict(
        _batching(set_records=cast("list[dict[str, object]]", parsed["sets"]))
    )
    failures = cast("list[str]", verdict["failures"])
    assert verdict["passed"] is False
    assert "set_record_invalid:batch-a" in failures
    assert "set_record_missing:batch-a" not in failures
    assert verdict["widest_merge"] is None  # the partial record is never judged
    # Control: a complete record for the same batch passes.
    assert gate.batching_verdict(_batching())["passed"] is True


@pytest.mark.parametrize(
    "overrides",
    [
        # Without `batch_id` a record names no batch, so the batch is `set_record_missing`.
        *({name: None} for name in (*gate.SET_RECORD_FIELDS[1:], "outcome")),
        {"statements": "3"},
        {"execution_ms": True},
        {"rows": -1},
        {"max_partitions_per_merge": 1.5},
        {"deadline_cancelled": 0},
        {"execution_set_id": ""},
    ],
)
def test_fix_review_f1_a_missing_or_mistyped_set_field_is_set_record_invalid(
    overrides: dict[str, object],
) -> None:
    """F1: every field `_record_set` writes is required with its written type."""
    record = {k: v for k, v in {**_set_record("batch-a"), **overrides}.items() if v is not None}
    verdict = gate.batching_verdict(_batching(set_records=[record]))
    assert "set_record_invalid:batch-a" in cast("list[str]", verdict["failures"])
    # Control: one invalid record beside a complete one still fails the batch (fail-closed).
    mixed = gate.batching_verdict(_batching(set_records=[_set_record("batch-a"), record]))
    assert "set_record_invalid:batch-a" in cast("list[str]", mixed["failures"])


def test_p6e2_reported_budget_and_park_lines_are_counted_for_graph_units_only() -> None:
    """P6E-2: the old parser counted 0 whatever the log held, a fail-open re-derivation read."""
    graph = gate.parse_batch_records(
        _reported_lines(
            ("semantic_statement_budget", "insufficient", "graph_unit"),
            ("semantic_attempt_parked", "parked", "graph_unit"),
            ("semantic_attempt_parked", "parked", "graph_unit"),
        )
    )
    assert graph["budget"] == {"semantic_statement_budget": 1, "semantic_attempt_parked": 2}
    # Control: the same lines from another writer kind are not graph-unit sets (D43).
    other = gate.parse_batch_records(
        _reported_lines(
            ("semantic_statement_budget", "insufficient", "transitional_delivery"),
            ("semantic_attempt_parked", "parked", "transitional_delivery"),
        )
    )
    assert other["budget"] == {"semantic_statement_budget": 0, "semantic_attempt_parked": 0}


def _read_batch_records(logs: _BatchRecordLogs) -> dict[str, object]:
    from datetime import UTC, datetime  # noqa: PLC0415

    return gate.batch_records_from_logs(
        logs,
        "/aws/lambda/adlytics-dev-TableWriter",
        window_start=datetime(2025, 1, 1, tzinfo=UTC),
        window_end=datetime(2025, 1, 1, 1, tzinfo=UTC),
    )


def test_slice7_g2_a_result_that_omits_matched_records_is_refused() -> None:
    """FEAT-009 G2: the target's set record arrives but the budget record was cut away."""
    logs = _BatchRecordLogs([_SET_LINE], {"recordsMatched": 2.0})
    with pytest.raises(gate.ProbeError, match=r"^the batch record query returned 1 of 2 matched"):
        _read_batch_records(logs)
    # Control: the same read with every match returned counts the budget record it carries.
    complete = _read_batch_records(
        _BatchRecordLogs([_SET_LINE, _BUDGET_LINE], {"recordsMatched": 2.0})
    )
    assert complete["budget"] == {"semantic_statement_budget": 1, "semantic_attempt_parked": 0}
    assert complete["sets"] == [{"batch_id": "batch-a", "outcome": "succeeded"}]


def test_slice7_g2_a_result_at_the_row_limit_is_refused_and_one_below_is_read() -> None:
    at_limit = _BatchRecordLogs(
        [_SET_LINE] * gate.BATCH_RECORD_LIMIT, {"recordsMatched": float(gate.BATCH_RECORD_LIMIT)}
    )
    with pytest.raises(gate.ProbeError, match=r"^the batch record query reached its 10000-row"):
        _read_batch_records(at_limit)
    # The query asks for the same limit it is judged against.
    assert at_limit.started[0]["limit"] == gate.BATCH_RECORD_LIMIT
    below = gate.BATCH_RECORD_LIMIT - 1
    read = _read_batch_records(_BatchRecordLogs([_SET_LINE] * below, {"recordsMatched": below}))
    assert len(cast("list[object]", read["sets"])) == below


def test_slice7_g2_a_further_page_or_missing_statistics_is_refused() -> None:
    with pytest.raises(gate.ProbeError, match=r"^the batch record query reached its 10000-row"):
        _read_batch_records(_BatchRecordLogs([_SET_LINE], {"recordsMatched": 1.0}, "next"))
    with pytest.raises(
        gate.ProbeError, match=r"^the batch record query reported no recordsMatched"
    ):
        _read_batch_records(_BatchRecordLogs([_SET_LINE], None))
    # Control: one page whose statistics match its one row is read.
    read = _read_batch_records(_BatchRecordLogs([_SET_LINE], {"recordsMatched": 1.0}))
    assert read["sets"] == [{"batch_id": "batch-a", "outcome": "succeeded"}]


def test_slice7_16_a_mean_at_the_trigger_passes_and_one_ms_above_fails() -> None:
    at = gate.batching_verdict(
        _batching(set_records=[_set_record("batch-a", execution_ms=3 * 22_193)])
    )
    assert at["passed"] is True
    above = gate.batching_verdict(
        _batching(set_records=[_set_record("batch-a", execution_ms=3 * 22_193 + 3)])
    )
    assert above["passed"] is False


def test_slice7_16_p6e_fails_when_batching_fails_or_a_unit_has_no_batch() -> None:
    ids = {
        measurement.measurement_id
        for row in _projection_rows()
        if (measurement := row_measurement(row))
    }
    held = _p6e(_P6eReads(ids, open_members=1))
    assert held["passed"] is False
    assert cast("dict[str, dict[str, object]]", held["checks"])["batching"]["passed"] is False
    with pytest.raises(gate.ProbeError, match="carries no batch_id"):
        _p6e(_P6eReads(ids, unit_b_batch=None))
    # FEAT-009 J1: a unit row with no node_id has no member identity to bind a span to.
    with pytest.raises(gate.ProbeError, match=r"^the unit write_unit:db\.b carries no node_id$"):
        _p6e(_P6eReads(ids, unit_b_node_id=None))
    # Control: the same reads with both identities present pass.
    assert _p6e(_P6eReads(ids))["passed"] is True


@guard_reserved_words
@_dataclass
class _BatchScans:
    counts: tuple[int, ...]
    pages: int = 1
    requests: list[dict[str, _Any]] = _field(default_factory=list[dict[str, _Any]])

    def scan(self, **request: _Any) -> dict[str, _Any]:
        self.requests.append(dict(request))
        index = sum(1 for item in self.requests if "ExclusiveStartKey" not in item) - 1
        page = sum(
            1 for item in self.requests if item["FilterExpression"] == request["FilterExpression"]
        )
        found: dict[str, _Any] = {"Count": self.counts[index] if page == self.pages else 0}
        if page < self.pages:
            found["LastEvaluatedKey"] = {"pk": {"S": f"page-{page}"}}
        return found


def _readback(scans: _BatchScans) -> dict[str, object]:
    return gate.batching_readback_verdict(gate.batching_readback_counts(scans, "graph"))


def test_slice7_16_the_pre_cas_batching_readback_passes_only_at_zero() -> None:
    assert _readback(_BatchScans((0, 0)))["passed"] is True
    for counts in ((1, 0), (0, 1)):
        assert _readback(_BatchScans(counts))["passed"] is False
    scans = _BatchScans((0, 0))
    gate.batching_readback_counts(scans, "graph")
    assert all(item["Select"] == "COUNT" and item["ConsistentRead"] for item in scans.requests)


def test_slice7_16_a_count_on_a_later_page_is_never_read_as_zero() -> None:
    paged = _BatchScans((1, 0), pages=3)
    read = gate.batching_readback_counts(paged, "graph")
    assert (read["table_locks"], gate.batching_readback_verdict(read)["passed"]) == (1, False)
    assert len(paged.requests) == 6  # every page of both scans was read


# --- D7 criterion 1: RuntimeAdmissionRetirementV1 ----------------------------------------------

from collections.abc import Callable as _Callable  # noqa: E402
from collections.abc import Mapping as _Mapping  # noqa: E402
from collections.abc import Sequence as _Sequence  # noqa: E402

# Legacy names are taken from the committed roster at run time, so this file holds no legacy
# literal outside the two function-qualified census/inertness helpers below.

_ROSTER = gate.load_retirement_roster()
_ROSTER_JSON = json.loads(gate.RETIREMENT_ROSTER_PATH.read_text())


def _retirement_tree(tmp_path: pathlib.Path, extra: dict[str, str]) -> pathlib.Path:
    """Write a minimal clean tree with every mandatory root, plus `extra` files."""
    files = {
        "src/domain/operations/generated_execution_graphs.py": "BOUNDS = {}\n",
        "src/adapters/clean.py": "def ok() -> int:\n    return 1\n",
        "infra/lib/generated/execution-graph-protocol.ts": "export const X = 1;\n",
        "infra/lib/stacks/clean.ts": "const keys = ['SERVING_DOMAIN#*'];\n",
        "infra/test/support/graph-call-inventory.ts": "export const ROWS = [];\n",
        "validation/probe_ingestion_traceability_gate.py": "VALUE = 1\n",
        "tests/unit/test_probe_ingestion_traceability_gate.py": "VALUE = 2\n",
        "infra/lib/README.md": "notes\n",
        **extra,
    }
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return tmp_path


def _forbidden(tmp_path: pathlib.Path, relative: str, text: str) -> dict[str, object]:
    tree = _retirement_tree(tmp_path, {relative: text})
    return gate.runtime_admission_retirement(tree).document()


def test_d7_retirement_passes_on_this_repository_and_lists_every_root() -> None:
    document = gate.runtime_admission_retirement(_ROOT).document()
    assert document["passed"] is True, document["forbidden_hits"]
    scanned = cast("list[str]", document["scanned_files"])
    for relative in gate.RETIREMENT_MANDATORY_FILES:
        assert relative in scanned
    assert document["auxiliary_roots"] == list(gate.RETIREMENT_AUXILIARY_ROOTS)
    owners = {
        f"{hit['path']}::{hit['qualname']}"
        for hit in cast("list[dict[str, object]]", document["allowlisted_hits"])
    }
    assert owners == set(_ROSTER.allowlist)


def test_d7_a_clean_tree_passes(tmp_path: pathlib.Path) -> None:
    assert _forbidden(tmp_path, "src/adapters/other.py", "VALUE = 3\n")["passed"] is True


@pytest.mark.parametrize(
    ("kind", "build"),
    [
        ("literal", lambda: f"KEY = {_ROSTER.key_prefixes[0] + 'x'!r}\n"),
        ("type", lambda: f"def f(value: {_ROSTER_JSON['types'][0]}) -> None: ...\n"),
        ("field", lambda: f"class Row:\n    {_ROSTER_JSON['fields'][0]}: str\n"),
        ("method", lambda: f"def f(store):\n    return store.{_ROSTER_JSON['methods'][0]}()\n"),
        ("function", lambda: f"def {_ROSTER_JSON['functions'][0]}():\n    return 1\n"),
        ("state", lambda: f"STATE = {_ROSTER_JSON['states'][0]!r}\n"),
        (
            "aliased_import",
            lambda: f"from m import {_ROSTER_JSON['functions'][1]} as hidden\nhidden()\n",
        ),
        (
            "type",
            lambda: (
                f"class {_ROSTER_JSON['identity_type']}:\n"
                f"    {_ROSTER_JSON['identity_day_members'][0]}: str\n"
            ),
        ),
    ],
)
def test_d7_each_legacy_python_class_fails_and_its_paired_control_passes(
    tmp_path: pathlib.Path, kind: str, build: _Any
) -> None:
    document = _forbidden(tmp_path, "src/adapters/legacy.py", build())
    assert document["passed"] is False
    hits = cast("list[dict[str, object]]", document["forbidden_hits"])
    assert kind in {hit["kind"] for hit in hits}
    assert all(hit["path"] == "src/adapters/legacy.py" for hit in hits)
    # Paired control: the same file with the name renamed passes.
    clean = tmp_path / "clean"
    assert _forbidden(clean, "src/adapters/legacy.py", "VALUE = 'renamed'\n")["passed"] is True


def test_d7_an_aliased_call_is_followed_to_its_retired_original(tmp_path: pathlib.Path) -> None:
    name = _ROSTER_JSON["methods"][1]
    document = _forbidden(
        tmp_path, "src/adapters/legacy.py", f"from m import {name} as read\nread()\nread()\n"
    )
    kinds = [hit["kind"] for hit in cast("list[dict[str, object]]", document["forbidden_hits"])]
    assert kinds.count("aliased_call") == 2


def test_d7_a_typescript_iam_prefix_or_renamed_call_fails(tmp_path: pathlib.Path) -> None:
    prefix = _ROSTER.key_prefixes[0]
    iam = _forbidden(
        tmp_path / "iam", "infra/lib/stacks/legacy.ts", f"const keys = ['{prefix}*'];\n"
    )
    assert {hit["kind"] for hit in cast("list[dict[str, object]]", iam["forbidden_hits"])} == {
        "iam_prefix"
    }
    method = _ROSTER_JSON["methods"][0]
    renamed = _forbidden(
        tmp_path / "call",
        "infra/test/support/graph-call-inventory.ts",
        f"import {{ {method} as r }} from './x';\nexport const ROWS = [r()];\n",
    )
    assert renamed["passed"] is False
    # A comment naming the prefix is not code and passes.
    comment = _forbidden(tmp_path / "comment", "infra/lib/stacks/legacy.ts", f"// {prefix}\n")
    assert comment["passed"] is True


def test_d7_a_legacy_literal_in_an_auxiliary_root_passes_only_in_its_owner(
    tmp_path: pathlib.Path,
) -> None:
    literal = repr(_ROSTER.key_prefixes[1])
    owner = gate.RETIREMENT_AUXILIARY_ROOTS[0]
    allowed = _forbidden(
        tmp_path / "allowed",
        owner,
        f"def gather_legacy_serving_census():\n    return {literal}\n",
    )
    assert allowed["passed"] is True
    assert len(cast("list[object]", allowed["allowlisted_hits"])) == 1
    other = _forbidden(tmp_path / "other", owner, f"def elsewhere():\n    return {literal}\n")
    assert other["passed"] is False
    # Function-qualified: the owner name in the *other* auxiliary root is not allowlisted.
    crossed = _forbidden(
        tmp_path / "crossed",
        gate.RETIREMENT_AUXILIARY_ROOTS[1],
        f"def gather_legacy_serving_census():\n    return {literal}\n",
    )
    assert crossed["passed"] is False
    # An allowlisted owner may hold the literal but may not call a retired method.
    method = _ROSTER_JSON["methods"][0]
    called = _forbidden(
        tmp_path / "called",
        owner,
        f"def gather_legacy_serving_census(store):\n    return store.{method}()\n",
    )
    assert called["passed"] is False


def test_d7_unknown_files_parse_failures_and_inventory_drift_are_not_proven(
    tmp_path: pathlib.Path,
) -> None:
    unknown = _forbidden(tmp_path / "unknown", "src/adapters/blob.bin", "x")
    assert unknown["passed"] is False and unknown["unknown_files"] == ["src/adapters/blob.bin"]
    broken = _forbidden(tmp_path / "broken", "src/adapters/broken.py", "def (:\n")
    assert broken["passed"] is False and broken["parse_failures"]
    tree = _retirement_tree(tmp_path / "drift", {})
    (tree / gate.RETIREMENT_CALL_INVENTORY).unlink()
    drift = gate.runtime_admission_retirement(tree).document()
    assert drift["passed"] is False
    assert drift["inventory_drift"] == [gate.RETIREMENT_CALL_INVENTORY]


def test_d7_a_malformed_retirement_roster_is_refused(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({**_ROSTER_JSON, "fields": []}))
    with pytest.raises(gate.ProbeError, match="no fields"):
        gate.load_retirement_roster(path)
    path.write_text(json.dumps({**_ROSTER_JSON, "schema_version": "v0"}))
    with pytest.raises(gate.ProbeError, match="wrong schema"):
        gate.load_retirement_roster(path)


# --- D7 criterion 2: census, inertness, preservation, fence ------------------------------------


def _legacy_serving_census_rows() -> list[dict[str, object]]:
    """Raw scan items: one day reservation, one sort-key wait under DUE#, and decoys."""
    return [
        {"pk": {"S": "SERVING_DAY#2026-10-06"}, "sk": {"S": "RESERVATION"}, "state": {"S": "OPEN"}},
        {
            "pk": {"S": "DUE#03"},
            "sk": {"S": "CAPACITY_WAIT#w-1"},
            "state": {"S": "WAITING"},
            "rollover_count": {"N": "0"},
            "run_ids": {"L": [{"S": "run-1"}]},
        },
        # Decoys: a day partition with another sort key and a wait-looking partition key.
        {"pk": {"S": "SERVING_DAY#2026-10-06"}, "sk": {"S": "TELEMETRY"}},
        {"pk": {"S": "CAPACITY_WAIT#w-2"}, "sk": {"S": "ROW"}},
    ]


def _legacy_serving_inertness_rows() -> dict[str, dict[str, object]]:
    """Return an obligation owning a retired day field, and one naming a wait key."""
    return {
        "retired_field": {
            "pk": {"S": "DUE#03"},
            "sk": {"S": "ARM#run-9"},
            "serving_day": {"S": "2026-10-06"},
        },
        "wait_reference": {
            "pk": {"S": "DUE#03"},
            "sk": {"S": "COALESCE#c-1"},
            "wait": {"S": "CAPACITY_WAIT#w-1"},
        },
    }


_HEAD_IDLE: dict[str, object] = {
    "pk": {"S": "SERVING_DOMAIN#d-1"},
    "sk": {"S": "HEAD"},
    "state": {"S": "IDLE"},
}
_OBLIGATION: dict[str, object] = {"pk": {"S": "DUE#01"}, "sk": {"S": "ARM#run-1"}}


@guard_reserved_words
@_dataclass
class _CensusDynamo:
    pages: list[list[dict[str, object]]]
    table_arns: list[str] = _field(default_factory=lambda: ["arn:table/graph"])
    repeat_token: bool = False
    items: dict[tuple[str, str], dict[str, object]] = _field(
        default_factory=dict[tuple[str, str], dict[str, object]]
    )
    described: int = 0

    def describe_table(self, **request: object) -> dict[str, object]:
        arn = self.table_arns[min(self.described, len(self.table_arns) - 1)]
        self.described += 1
        return {"Table": {"TableArn": arn, "TableName": request["TableName"]}}

    def scan(self, **request: object) -> dict[str, object]:
        assert request["ConsistentRead"] is True
        start = request.get("ExclusiveStartKey")
        index = 0 if start is None else int(cast("dict[str, int]", start)["page"])
        page: dict[str, object] = {"Items": self.pages[index]}
        if index + 1 < len(self.pages):
            page["LastEvaluatedKey"] = {"page": 1 if self.repeat_token else index + 1}
        return page

    def get_item(self, **request: object) -> dict[str, object]:
        assert request["ConsistentRead"] is True
        key = cast("dict[str, dict[str, str]]", request["Key"])
        found = self.items.get((key["pk"]["S"], key["sk"]["S"]))
        return {} if found is None else {"Item": found}


@_dataclass
class _CensusSession:
    dynamodb: _CensusDynamo
    arns: list[str] = _field(default_factory=lambda: ["arn:caller/operator"])
    calls: int = 0

    def client(self, name: str) -> _Any:
        if name == "sts":
            session = self

            class _Sts:
                def get_caller_identity(self) -> dict[str, str]:
                    arn = session.arns[min(session.calls, len(session.arns) - 1)]
                    session.calls += 1
                    return {"Arn": arn}

            return _Sts()
        assert name == "dynamodb"
        return self.dynamodb


def _census(pages: list[list[dict[str, object]]], **options: _Any) -> _Any:
    return gate.gather_legacy_serving_census(
        _CensusSession(_CensusDynamo(pages, **options)), "graph", region="us-east-1"
    )


def test_d7_the_census_selects_exactly_and_hashes_one_canonical_encoding() -> None:
    rows = _legacy_serving_census_rows()
    census = _census([[rows[3], rows[1]], [rows[2], rows[0], _HEAD_IDLE, _OBLIGATION]])
    assert [gate._raw_key(row) for row in census.rows] == [  # pyright: ignore[reportPrivateUsage]
        gate._raw_key(rows[1]),  # pyright: ignore[reportPrivateUsage]
        gate._raw_key(rows[0]),  # pyright: ignore[reportPrivateUsage]
    ]
    assert (census.page_count, census.scanned_count, census.wait_row_count) == (2, 6, 1)
    assert dict(census.day_state_counts) == {"OPEN": 1}
    assert dict(census.wait_state_counts) == {"WAITING": 1}
    encoded = json.dumps(
        list(census.rows), sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    assert census.sha256 == hashlib.sha256(encoded.encode()).hexdigest()
    assert census.canonical_length == len(encoded.encode())
    # The same rows in another scan order and page split give byte-identical evidence.
    again = _census([[_OBLIGATION, rows[0]], [rows[1]], [rows[2], rows[3], _HEAD_IDLE]])
    assert gate.legacy_serving_census_verdict(census, again)["passed"] is True
    assert len(census.authority_rows) == 2


def test_d7_a_malformed_key_cycle_or_identity_change_cannot_produce_a_census() -> None:
    rows = _legacy_serving_census_rows()
    with pytest.raises(gate.LegacyCensusError, match="malformed key") as malformed:
        _census([[{"pk": {"N": "1"}, "sk": {"S": "x"}}]])
    assert malformed.value.partial["invalid"] is True
    with pytest.raises(gate.LegacyCensusError, match="token repeated"):
        _census([[rows[0]], [rows[1]], [rows[2]]], repeat_token=True)
    with pytest.raises(gate.LegacyCensusError, match="identity changed"):
        _census([[rows[0]]], table_arns=["arn:table/graph", "arn:table/other"])


def _inert(
    census_rows: _Sequence[_Mapping[str, object]],
    authority: _Mapping[tuple[str, str], _Mapping[str, object]],
) -> _Any:
    dynamo = _CensusDynamo(
        [[dict(row) for row in census_rows]],
        items={key: dict(value) for key, value in authority.items()},
    )
    session = _CensusSession(dynamo)
    census = gate.gather_legacy_serving_census(session, "graph", region="us-east-1")
    return gate.legacy_serving_inertness_verdict(
        census, gate.read_legacy_serving_authority(session, census)
    )


def test_d7_inertness_passes_only_without_waits_and_with_idle_heads() -> None:
    day = _legacy_serving_census_rows()[0]
    keys = {("SERVING_DOMAIN#d-1", "HEAD"): _HEAD_IDLE, ("DUE#01", "ARM#run-1"): _OBLIGATION}
    passed = _inert([day, _HEAD_IDLE, _OBLIGATION], keys)
    assert passed.passed is True and passed.reasons == ()
    # An unchanged wait owning an open run fails.
    wait = _legacy_serving_census_rows()[1]
    assert _inert([day, wait, _HEAD_IDLE, _OBLIGATION], keys).passed is False
    # An unchanged day row paired with an ACTIVE head fails.
    active = {**_HEAD_IDLE, "state": {"S": "ACTIVE"}}
    verdict = _inert([day, active], {("SERVING_DOMAIN#d-1", "HEAD"): active})
    assert verdict.passed is False and any("head_not_idle" in item for item in verdict.reasons)


def test_d7_inertness_refuses_disappeared_changed_unknown_or_dependent_authority() -> None:
    day = _legacy_serving_census_rows()[0]
    gone = _inert([day, _HEAD_IDLE], {})
    assert any("authority_row_disappeared" in item for item in gone.reasons)
    changed = {**_HEAD_IDLE, "version": {"N": "2"}}
    moved = _inert([day, _HEAD_IDLE], {("SERVING_DOMAIN#d-1", "HEAD"): changed})
    assert any("authority_row_changed" in item for item in moved.reasons)
    unknown = {**_HEAD_IDLE, "state": {"S": "PARKED"}}
    odd = _inert([day, unknown], {("SERVING_DOMAIN#d-1", "HEAD"): unknown})
    assert any("head_state_unknown" in item for item in odd.reasons)
    missing = {"pk": {"S": "SERVING_DOMAIN#d-1"}, "sk": {"S": "HEAD"}}
    absent = _inert([day, missing], {("SERVING_DOMAIN#d-1", "HEAD"): missing})
    assert any("head_state_missing" in item for item in absent.reasons)
    for name, row in _legacy_serving_inertness_rows().items():
        key = gate._raw_key(row)  # pyright: ignore[reportPrivateUsage]
        verdict = _inert([day, row], {key: row})
        expected = (
            "retired_authority_dependency" if name == "retired_field" else "references_wait_key"
        )
        assert any(expected in item for item in verdict.reasons), name


def test_d7_postdeploy_preservation_gates_bytes_not_provenance() -> None:
    rows = _legacy_serving_census_rows()
    baseline = _census([[rows[0], rows[1]]])
    later = _census([[rows[0], rows[1], _OBLIGATION], [_HEAD_IDLE]])
    assert later.page_count != baseline.page_count
    assert gate.legacy_serving_census_verdict(baseline, later)["passed"] is True
    altered = {**rows[0], "state": {"S": "CLOSED"}}
    drifted = _census([[altered, rows[1]]])
    verdict = gate.legacy_serving_census_verdict(baseline, drifted)
    assert verdict["passed"] is False and "sha256" in cast("list[str]", verdict["differences"])
    other_caller = gate.gather_legacy_serving_census(
        _CensusSession(_CensusDynamo([[rows[0], rows[1]]]), arns=["arn:caller/else"]),
        "graph",
        region="us-east-1",
    )
    assert gate.legacy_serving_census_verdict(baseline, other_caller)["passed"] is False
    # The artifact round-trips through its JSON document unchanged.
    restored = gate._census_from_document(  # pyright: ignore[reportPrivateUsage]
        json.loads(json.dumps(baseline.document()))
    )
    assert gate.legacy_serving_census_verdict(baseline, restored)["passed"] is True


def test_d7_cutover_needs_two_matching_censuses_around_a_passing_inertness_read() -> None:
    day = _legacy_serving_census_rows()[0]
    keys = {("SERVING_DOMAIN#d-1", "HEAD"): _HEAD_IDLE}
    session = _CensusSession(_CensusDynamo([[day, _HEAD_IDLE]], items=keys))
    census_a = gate.gather_legacy_serving_census(session, "graph", region="us-east-1")
    inert = gate.legacy_serving_inertness_verdict(
        census_a, gate.read_legacy_serving_authority(session, census_a)
    )
    census_b = gate.gather_legacy_serving_census(session, "graph", region="us-east-1")
    assert gate.legacy_serving_cutover_verdict(census_a, inert, census_b)["passed"] is True
    changed = _census([[{**day, "state": {"S": "CLOSED"}}, _HEAD_IDLE]])
    assert gate.legacy_serving_cutover_verdict(census_a, inert, changed)["passed"] is False
    failing = _replace(inert, reasons=("head_not_idle:x",))
    assert gate.legacy_serving_cutover_verdict(census_a, failing, census_b)["passed"] is False


def _fence_readback(**overrides: object) -> dict[str, object]:
    readback: dict[str, object] = {
        "caller": "arn:caller/operator",
        "table": "graph",
        "fence": {
            "state": "SET",
            "migration_id": "exec-1",
            "owner": "arn:caller/operator",
            "revision": 12,
        },
        "receipt": {"command_id": "exec-1"},
        "mode": "GRAPH",
        "mode_revision": 12,
        "hold": True,
    }
    readback.update(overrides)
    return readback


def test_d7_the_fence_readback_must_match_held_graph_12_and_its_execution() -> None:
    assert gate.fence_readback_verdict(_fence_readback(), execution_id="exec-1")["passed"] is True
    for changed in (
        {"hold": False},
        {"mode": "LEGACY"},
        {"mode_revision": 11},
        {"caller": "arn:caller/else"},
    ):
        assert (
            gate.fence_readback_verdict(_fence_readback(**changed), execution_id="exec-1")["passed"]
            is False
        ), changed
    assert gate.fence_readback_verdict(_fence_readback(), execution_id="exec-2")["passed"] is False


def test_d7_cutover_waits_900_seconds_from_the_later_of_deploy_and_readback() -> None:
    deployed, readback = "2026-10-15T10:00:00+00:00", "2026-10-15T10:05:00+00:00"
    assert (
        gate.fence_wait_verdict(deployed, readback, "2026-10-15T10:20:00+00:00")["passed"] is True
    )
    early = gate.fence_wait_verdict(deployed, readback, "2026-10-15T10:19:59+00:00")
    assert early["passed"] is False and early["t0"] == readback
    with pytest.raises(gate.ProbeError, match="UTC"):
        gate.fence_wait_verdict(deployed, "2026-10-15T10:05:00", "2026-10-15T10:20:00+00:00")


# --- D7 criterion 6: trace completeness -----------------------------------------------------------


def _span(name: str, span_id: str, parent: str | None, **attributes: object) -> dict[str, object]:
    return {
        "name": name,
        "spanId": span_id,
        "parentSpanId": parent,
        "endTimeUnixNano": "1",
        "attributes": {
            "run_id": "run-a",
            "task_id": "task",
            "cohort_revision": "c-a",
            **attributes,
        },
    }


def _trace() -> gate.TraceCompletenessEvidence:
    spans = [
        _span("bake.run", "r", None, outcome="completed"),
        _span("bake.source_pin", "sp", "r"),
        _span("bake.fan_out", "fo", "r"),
        _span("bake.task", "t1", "x", page="p", variant="1d"),
        _span("bake.phase", "ph1", "t1", phase="plan"),
        _span("bake.phase", "ph3", "t1", phase="query"),
        _span("bake.phase", "ph4", "t1", phase="render"),
        _span("bake.phase", "ph2", "t1", phase="write"),
        _span("bake.finalize", "fa", "y", outcome="graph_bake_precompute_armed"),
        _span("bake.finalize", "f", "y", outcome="graph_bake_finalized"),
        _span("precompute.run", "pr", "z"),
        _span("insight.precompute.plan", "pl", "pr"),
        _span("insight.model.call", "m", "pr"),
        _span("artifact.write", "w", "t1", bytes=120),
        _span("athena.query", "q", "t1", query_id="q-1", bytes_scanned=4096, state="SUCCEEDED"),
        {
            "name": "execution_graph.promoter_pass",
            "spanId": "pp",
            "endTimeUnixNano": "1",
            "attributes": {"aws_request_id": "req-1", "outcome": "completed"},
        },
        {
            "name": "execution_graph.promoter_domain",
            "spanId": "pd",
            "parentSpanId": "pp",
            "endTimeUnixNano": "1",
            "attributes": {
                "aws_request_id": "req-1",
                "domain": "d-1",
                "cohort_revision": "c-a",
                "outcome": "elected",
            },
        },
    ]
    expected = gate.TraceCohortExpectation(
        cohort_revision="c-a",
        run_id="run-a",
        bake_task_keys=("p#1d",),
        task_writes=(("task", True),),
        artifact_writes=1,
        artifact_bytes=(120,),
        athena_queries=1,
        athena_succeeded_ids=frozenset({"q-1"}),
        athena_bytes_scanned=4096,
        model_requests=1,
    )
    return gate.TraceCompletenessEvidence(
        (expected,), (("req-1", "d-1", "c-a"),), tuple(spans), True
    )


def _without(
    evidence: gate.TraceCompletenessEvidence, span_id: str
) -> gate.TraceCompletenessEvidence:
    return _replace(evidence, spans=tuple(s for s in evidence.spans if s.get("spanId") != span_id))


def _edited(
    evidence: gate.TraceCompletenessEvidence,
    span_id: str,
    change: _Callable[[dict[str, object]], dict[str, object]],
) -> gate.TraceCompletenessEvidence:
    return _replace(
        evidence,
        spans=tuple(change(dict(s)) if s.get("spanId") == span_id else s for s in evidence.spans),
    )


def _attrs(span: dict[str, object], **changes: object) -> dict[str, object]:
    attributes = {**cast("dict[str, object]", span["attributes"]), **changes}
    return {**span, "attributes": {k: v for k, v in attributes.items() if v is not None}}


def test_d7_trace_completeness_passes_with_every_producer_class() -> None:
    verdict = gate.trace_completeness_verdict(_trace())
    assert verdict["passed"] is True, verdict["failures"]


@pytest.mark.parametrize(
    "span_id",
    ["sp", "fo", "pl", "m", "q", "w", "f", "fa", "t1", "ph1", "ph3", "ph4", "pd", "pp", "r", "pr"],
)
def test_d7_removing_any_producer_span_fails_trace_completeness(span_id: str) -> None:
    assert gate.trace_completeness_verdict(_without(_trace(), span_id))["passed"] is False


def test_d7_trace_correlation_parent_bytes_statistics_and_terminal_controls_fail() -> None:
    trace = _trace()
    for field_name in ("run_id", "task_id"):
        removed = _edited(trace, "q", lambda s, f=field_name: _attrs(s, **{f: None}))
        assert gate.trace_completeness_verdict(removed)["passed"] is False, field_name
    no_cohort = _edited(trace, "m", lambda s: _attrs(s, cohort_revision=None))
    assert gate.trace_completeness_verdict(no_cohort)["passed"] is False
    wrong_parent = _edited(trace, "sp", lambda s: {**s, "parentSpanId": "elsewhere"})
    assert gate.trace_completeness_verdict(wrong_parent)["passed"] is False
    wrong_domain_parent = _edited(trace, "pd", lambda s: {**s, "parentSpanId": "elsewhere"})
    assert gate.trace_completeness_verdict(wrong_domain_parent)["passed"] is False
    wrong_bytes = _edited(trace, "w", lambda s: _attrs(s, bytes=121))
    assert gate.trace_completeness_verdict(wrong_bytes)["passed"] is False
    wrong_scan = _edited(trace, "q", lambda s: _attrs(s, bytes_scanned=1))
    assert gate.trace_completeness_verdict(wrong_scan)["passed"] is False
    nonterminal = _edited(trace, "pl", lambda s: {**s, "endTimeUnixNano": None})
    assert gate.trace_completeness_verdict(nonterminal)["passed"] is False
    duplicate = _replace(
        trace,
        spans=(*trace.spans, _span("bake.finalize", "f2", "y", outcome="graph_bake_finalized")),
    )
    assert gate.trace_completeness_verdict(duplicate)["passed"] is False
    incomplete = _replace(trace, retrieval_complete=False)
    assert gate.trace_completeness_verdict(incomplete)["passed"] is False


# --- D7 criterion 7: Athena admission configuration ----------------------------------------------


def _admission(**overrides: object) -> dict[str, object]:
    receipt: dict[str, object] = {
        "source": "explicit_env",
        "configured": 8,
        "synthesized": {"Adlytics-Api-dev": 8, "Adlytics-Ingestion-dev": 8},
        "deployed": {"api": 8},
        "readback": {"api": 8},
        "static_rows": dict(gate.ATHENA_STATIC_CALLER_ROWS),
        "active_dml_quota": 200,
        "chat_reserve": 20,
        "code_sha": "abc",
        "environment": "dev",
        "workgroup": "interactive",
    }
    receipt.update(overrides)
    return receipt


def _measurement(value: int) -> tuple[dict[str, object], bytes]:
    document: dict[str, object] = {
        "schema_version": "athena-admission-measurement-v1",
        "code_sha": "abc",
        "environment": "dev",
        "workgroup": "interactive",
        "active_dml_quota": 200,
        "tested_value": value,
        "conclusion": "replacement_admitted",
        "static_rows": dict(gate.ATHENA_STATIC_CALLER_ROWS),
        "chat_reserve": 20,
    }
    return document, json.dumps(document, sort_keys=True).encode()


def test_d7_athena_admission_passes_only_at_explicit_8_or_a_proven_replacement() -> None:
    assert sum(gate.ATHENA_STATIC_CALLER_ROWS.values()) == 41
    assert gate.athena_admission_config_verdict(_admission())["passed"] is True
    absent = gate.athena_admission_config_verdict(_admission(configured=None))
    assert absent["passed"] is False and "absent" in cast("list[str]", absent["reasons"])
    fallback = gate.athena_admission_config_verdict(_admission(source="default"))
    assert "implicit_fallback" in cast("list[str]", fallback["reasons"])
    non_eight = {
        "configured": 6,
        "synthesized": {"s": 6},
        "deployed": {"d": 6},
        "readback": {"r": 6},
    }
    unproven = gate.athena_admission_config_verdict(_admission(**non_eight))
    assert "unproven_non_8" in cast("list[str]", unproven["reasons"])
    document, raw = _measurement(6)
    proven = gate.athena_admission_config_verdict(
        _admission(**non_eight, measurement_sha256=hashlib.sha256(raw).hexdigest()), document, raw
    )
    assert proven["passed"] is True
    stale = gate.athena_admission_config_verdict(
        _admission(**non_eight, measurement_sha256="0" * 64), document, raw
    )
    assert stale["passed"] is False
    mismatch = gate.athena_admission_config_verdict(_admission(readback={"api": 7}))
    assert "deployed_readback_mismatch" in cast("list[str]", mismatch["reasons"])
    changed_row = gate.athena_admission_config_verdict(
        _admission(static_rows={**gate.ATHENA_STATIC_CALLER_ROWS, "baker": 21})
    )
    assert "static_rows_or_reserve_changed" in cast("list[str]", changed_row["reasons"])


# --- D7 criterion 8: paired governed chat ------------------------------------------------------

_PROMPTS = ("p1", "p2", "p3", "p4", "p5", "outside_bounded_campaign")
_BAKE = ("2026-10-15T10:10:00+00:00", "2026-10-15T10:30:00+00:00")


def _chat(
    phase: str, latencies: tuple[int, ...], *, start: str, **overrides: object
) -> dict[str, object]:
    begin = _datetime.fromisoformat(start)
    rows: list[dict[str, object]] = []
    for prompt, latency in zip(_PROMPTS, latencies, strict=True):
        row: dict[str, object] = {
            "prompt_id": prompt,
            "request_started_at": begin.isoformat(),
            "request_ended_at": (begin + _timedelta(milliseconds=latency)).isoformat(),
            "latency_ms": latency,
            "terminal_class": "answer",
            "governed_answer": True,
            "governed_generation": "gen-7",
            "http_status": 200,
            "auth_outcome": "authenticated",
            "quota_refused": False,
            "throttled": False,
        }
        if phase == "during_bake":
            row["head_active_receipt"] = {"state": "ACTIVE", "cohort_revision": "c-a"}
        rows.append(row)
        begin += _timedelta(seconds=10)
    sample: dict[str, object] = {
        "schema_version": "chat-latency-sample-v1",
        "phase": phase,
        "acceptance_execution_id": "exec-1",
        "environment": "dev",
        "account": "000000000000",
        "origin": "https://app.invalid",
        "persona_id": "probe-a",
        "prompt_manifest_path": "validation/fixtures/chat-bake-acceptance-v2.json",
        "prompt_manifest_sha256": "f" * 64,
        "sample_count": 6,
        "accepted_generation": "gen-7",
        "generation_unit": "b" * 64,
        "sample_started_at": start,
        "sample_ended_at": rows[-1]["request_ended_at"],
        "preflight_completed_at": "2026-10-15T09:00:00+00:00",
        "first_root_launch_at": "2026-10-15T10:00:00+00:00",
        "target_bake_run_id": "run-a",
        "target_cohort_revision": "c-a",
        "bake_run_started_at": _BAKE[0],
        "bake_finalize_ended_at": _BAKE[1],
        "outside_list_item": "campaign:bulk-estate-1000",
        "outside_list_absent_from_bounded_list": True,
        "outside_list_present_in_generation": True,
        "rows": rows,
    }
    sample.update(overrides)
    return sample


def _paired(
    during: tuple[int, ...] = (1000,) * 5 + (1100,), **overrides: object
) -> dict[str, object]:
    baseline = _chat("paired_no_bake", (900,) * 5 + (1000,), start="2026-10-15T09:10:00+00:00")
    load = _chat("during_bake", during, start="2026-10-15T10:15:00+00:00", **overrides)
    return gate.paired_bake_chat_verdict(
        baseline, load, "gen-7", "campaign:bulk-estate-1000", _BAKE
    )


def test_d7_paired_chat_refuses_samples_on_different_generation_units() -> None:
    assert _paired()["passed"] is True
    other = _paired(generation_unit="c" * 64)
    assert other["passed"] is False
    assert "identity_mismatch" in cast("list[str]", other["reasons"])


def test_d7_nearest_rank_p95_is_exact() -> None:
    assert gate.nearest_rank_p95([5, 1, 4, 2, 3, 6]) == 6
    assert gate.nearest_rank_p95(list(range(1, 21))) == 19
    assert gate.nearest_rank_p95([7]) == 7


def test_d7_paired_chat_passes_at_exact_110_percent_and_fails_one_unit_over() -> None:
    assert _paired()["passed"] is True
    over = _paired((1000,) * 5 + (1101,))
    assert over["passed"] is False and "p95_over_110_percent" in cast("list[str]", over["reasons"])


def test_d7_paired_chat_overlap_boundary_is_inclusive_and_a_gap_is_not_overlap() -> None:
    touching = _chat("during_bake", (1000,) * 6, start="2026-10-15T10:30:00+00:00")
    baseline = _chat("paired_no_bake", (1000,) * 6, start="2026-10-15T09:10:00+00:00")
    verdict = gate.paired_bake_chat_verdict(
        baseline, touching, "gen-7", "campaign:bulk-estate-1000", _BAKE
    )
    assert "no_bake_overlap" not in cast("list[str]", verdict["reasons"])
    after = _chat("during_bake", (1000,) * 6, start="2026-10-15T10:30:00.001000+00:00")
    gap = gate.paired_bake_chat_verdict(
        baseline, after, "gen-7", "campaign:bulk-estate-1000", _BAKE
    )
    assert "no_bake_overlap" in cast("list[str]", gap["reasons"])


def test_d7_paired_chat_temporal_and_identity_mismatches_are_not_proven() -> None:
    late_baseline = _chat("paired_no_bake", (900,) * 6, start="2026-10-15T09:59:59+00:00")
    load = _chat("during_bake", (900,) * 6, start="2026-10-15T10:15:00+00:00")
    reversed_bracket = gate.paired_bake_chat_verdict(
        late_baseline, load, "gen-7", "campaign:bulk-estate-1000", _BAKE
    )
    assert "baseline_outside_preflight_launch" in cast("list[str]", reversed_bracket["reasons"])
    baseline = _chat("paired_no_bake", (900,) * 6, start="2026-10-15T09:10:00+00:00")
    reused = gate.paired_bake_chat_verdict(
        baseline,
        load,
        "gen-7",
        "campaign:bulk-estate-1000",
        _BAKE,
        used_execution_ids=frozenset({"exec-1"}),
    )
    assert "execution_id_reused" in cast("list[str]", reused["reasons"])
    other_interval = gate.paired_bake_chat_verdict(
        baseline,
        load,
        "gen-7",
        "campaign:bulk-estate-1000",
        (_BAKE[0], "2026-10-15T10:31:00+00:00"),
    )
    assert "bake_interval_mismatch" in cast("list[str]", other_interval["reasons"])
    no_finalize = _chat(
        "during_bake", (900,) * 6, start="2026-10-15T10:15:00+00:00", bake_finalize_ended_at=None
    )
    missing = gate.paired_bake_chat_verdict(
        baseline, no_finalize, "gen-7", "campaign:bulk-estate-1000", _BAKE
    )
    assert missing["passed"] is False and missing["disposition"] == "NOT_PROVEN"
    inactive = _paired(
        rows=[
            {**row, "head_active_receipt": {"state": "IDLE"}}
            for row in cast("list[dict[str, object]]", load["rows"])
        ]
    )
    assert "during_launch_not_active" in cast("list[str]", inactive["reasons"])
    throttled_rows = [dict(row) for row in cast("list[dict[str, object]]", load["rows"])]
    throttled_rows[0]["throttled"] = True
    assert _paired(rows=throttled_rows)["passed"] is False


# --- D7 criterion 9: the closed historical migration matrix ----------------------------------


def _matrix_evidence(matrix: _Any) -> dict[str, dict[str, object]]:
    return {
        str(row["criterion_id"]): {"execution_id": "exec-1", "passed": True}
        for row in matrix["rows"]
        if row["status"] != "retired"
    }


def test_d7_the_committed_matrix_matches_the_closed_row_set_and_passes() -> None:
    matrix = gate.load_hold_clear_matrix()
    rows = cast("list[dict[str, object]]", matrix["rows"])
    assert [row["criterion_id"] for row in rows] == list(gate.HOLD_CLEAR_MATRIX_IDS)
    assert {row["criterion_id"] for row in rows if row["status"] == "retired"} == {
        "HIST-13",
        "HIST-20",
    }
    verdict = gate.hold_clear_matrix_verdict(
        matrix, _matrix_evidence(matrix), execution_id="exec-1"
    )
    assert verdict["passed"] is True, verdict["reasons"]


def test_d7_matrix_duplicates_gaps_unknowns_and_failing_evidence_keep_the_hold() -> None:
    matrix = gate.load_hold_clear_matrix()
    rows = cast("list[dict[str, object]]", matrix["rows"])
    evidence = _matrix_evidence(matrix)

    def verdict(**changes: _Any) -> list[str]:
        document = {**matrix, **changes.pop("matrix", {})}
        found = changes.pop("evidence", evidence)
        result = gate.hold_clear_matrix_verdict(document, found, execution_id="exec-1")
        assert result["passed"] is False
        return cast("list[str]", result["reasons"])

    assert "duplicate:HIST-1" in verdict(matrix={"rows": [*rows, rows[0]]})
    assert "missing:HIST-5" in verdict(
        matrix={"rows": [r for r in rows if r["criterion_id"] != "HIST-5"]}
    )
    assert "unknown:HIST-99" in verdict(
        matrix={"rows": [*rows, {**rows[0], "criterion_id": "HIST-99"}]}
    )
    assert "unknown_status:HIST-1" in verdict(
        matrix={"rows": [{**rows[0], "status": "deferred"}, *rows[1:]]}
    )
    assert "evidence_absent:HIST-5" in verdict(
        evidence={k: v for k, v in evidence.items() if k != "HIST-5"}
    )
    assert "evidence_failed:HIST-4" in verdict(
        evidence={**evidence, "HIST-4": {"execution_id": "exec-1", "passed": False}}
    )
    assert "evidence_other_execution:HIST-4" in verdict(
        evidence={**evidence, "HIST-4": {"execution_id": "exec-0", "passed": True}}
    )
    assert "retired_row_executed:HIST-13" in verdict(
        evidence={**evidence, "HIST-13": {"execution_id": "exec-1", "passed": True}}
    )
    retired = [{**r, "reason": ""} if r["criterion_id"] == "HIST-20" else r for r in rows]
    assert "invalid_retirement_reason:HIST-20" in verdict(matrix={"rows": retired})
