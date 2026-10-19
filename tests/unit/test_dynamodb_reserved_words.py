# SPDX-License-Identifier: MIT-0
"""Gate: no DynamoDB expression under src/ uses a reserved word as a bare attribute name.

DynamoDB rejects such a request with `ValidationException` before any condition runs, so a
whole transaction never commits. The rehearsal found the class live twice on 2026-10-15
(three stage-1 writers, and the final runtime's 51-action `drain_final_alias_and_activate_next`)
while every unit test passed against fakes that did not model the rule. Business outcome: the
batch-to-batch serving handoff, and every other graph write, can commit in DynamoDB at all.
`FakeDynamo` now enforces the same rule on every request the real expression builders produce;
this file statically scans every expression fragment and holds the paired controls.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast

import pytest
from botocore.exceptions import ClientError
from dynamodb_reserved_words import (
    RESERVED_WORDS,
    RESERVED_WORDS_PATH,
    guard_reserved_words,
    refuse_reserved,
    request_refusal,
    scan_source,
    scan_tree,
    unaliased_reserved,
    unguarded_fakes,
)
from execution_graph_support import FakeDynamo

from adapters.outbound.dynamodb.graph_transactions import MemberSpec, check

pytestmark = pytest.mark.unit
"""Without it `make verify` (`pytest -m unit`) would deselect this gate (Opus review, HIGH)."""

_ROOT = RESERVED_WORDS_PATH.parents[3]


def test_the_committed_list_is_the_official_one() -> None:
    text = RESERVED_WORDS_PATH.read_text(encoding="utf-8")
    assert "docs.aws.amazon.com/amazondynamodb/latest/developerguide/ReservedWords.html" in text
    assert len(RESERVED_WORDS) == 573
    assert {"DOMAIN", "STATE", "STATUS", "NAME", "COUNT", "DATA"} <= RESERVED_WORDS
    assert hashlib.sha256(text.encode()).hexdigest() == (
        # pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
        "44726c16f79870092bcd12e4327dc7e003b1d5a0e6e076018d0c1745e17d38df"
    )


def test_no_src_expression_uses_an_unaliased_reserved_word() -> None:
    hits = scan_tree(_ROOT)
    assert hits == [], "\n".join(f"{h.path}:{h.line} {h.word!r} in {h.fragment!r}" for h in hits)


@pytest.mark.parametrize(
    ("expression", "found"),
    [
        ("grant_id = :grant AND domain = :domain", ["domain"]),
        ("SET #state = :to, status = :s", ["status"]),
        ("attribute_not_exists(pk) AND Name = :n", ["Name"]),
        ("#domain = :domain AND #state = :s", []),
        ("attribute_exists(pk) AND begins_with(sk, :p)", []),
        ("SET revision = revision + :one REMOVE lease_owner", []),
        ("size(members) > :zero", []),
    ],
)
def test_bare_reserved_names_are_found_and_aliases_functions_grammar_are_not(
    expression: str, found: list[str]
) -> None:
    assert unaliased_reserved(expression) == found


def test_the_static_scan_reports_an_injected_bare_domain_and_not_its_alias() -> None:
    bare = 'X = check(MemberSpec("", KEY, "S", "grant_id = :g AND domain = :domain"))\n'
    aliased = bare.replace(" domain = ", " #domain = ")
    built = 'X = f"SET {name} = :v, domain = :domain"\n'
    assert [hit.word for hit in scan_source("bare.py", bare)] == ["domain"]
    assert scan_source("aliased.py", aliased) == []
    assert [hit.word for hit in scan_source("fstring.py", built)] == ["domain"]
    docstring = '"""Explains that `domain = :domain` must be aliased."""\n'
    assert scan_source("doc.py", docstring) == []


def test_the_fake_refuses_a_bare_reserved_word_as_dynamodb_does() -> None:
    fake = FakeDynamo()
    key = cast("Any", {"pk": {"S": "COMPLETE#r"}, "sk": {"S": "COMPLETE"}})
    fake.items[("COMPLETE#r", "COMPLETE")] = {**key, "domain": {"S": "d"}}
    bare = check(MemberSpec("t", key, "S", "domain = :domain", {":domain": {"S": "d"}}))
    with pytest.raises(ClientError) as refused:
        fake.transact_write_items(TransactItems=[cast("Any", bare.item)])
    error = cast("dict[str, Any]", refused.value.response)["Error"]
    assert error["Code"] == "ValidationException"
    assert "reserved keyword: domain" in error["Message"]
    assert fake.transactions == []
    # Paired control: the shared alias table names `#domain`, and the same check commits.
    aliased = check(MemberSpec("t", key, "S", "#domain = :domain", {":domain": {"S": "d"}}))
    body = cast("dict[str, Any]", aliased.item)["ConditionCheck"]
    assert body["ExpressionAttributeNames"] == {"#domain": "domain"}
    fake.transact_write_items(TransactItems=[cast("Any", aliased.item)])
    assert len(fake.transactions) == 1
    assert request_refusal({"KeyConditionExpression": "pk = :pk AND name = :n"}) is not None


@pytest.mark.parametrize(
    "source",
    [
        'X = "grant_id = :g" + " AND domain" + " = :domain"\n',
        'def more() -> str:\n    return " AND domain = :domain"\n',
        'def more() -> str:\n    return ", status = :status"\n',
        'X = get(ProjectionExpression="pk, domain")\n',
        'X = {"TableName": T, "ProjectionExpression": "pk, sk, " + "status"}\n',
    ],
    ids=[
        "split-concatenation",
        "helper-and",
        "helper-set-list",
        "projection-kw",
        "projection-dict",
    ],
)
def test_split_helper_and_projection_fragments_are_found(source: str) -> None:
    assert [hit.word for hit in scan_source("split.py", source)] in (["domain"], ["status"])
    aliased = source.replace("domain", "#domain").replace("status", "#status")
    assert scan_source("aliased.py", aliased) == []


def test_a_runtime_assembled_fragment_is_refused_by_the_fake_path() -> None:
    # Static reach ends where a helper builds the word itself; the fake refuses the final request.
    word = "".join(("dom", "ain"))
    assert scan_source("built.py", 'X = "".join(("dom", "ain"))\n') == []
    assert request_refusal({"ConditionExpression": f"grant_id = :g AND {word} = :d"}) is not None


def test_every_dynamodb_fake_is_guarded() -> None:
    # A fake that skips the guard would let the class past every test that uses it (review #3).
    assert unguarded_fakes(_ROOT) == []


def test_the_guard_refuses_before_the_fake_runs_and_covers_every_request_shape() -> None:
    @guard_reserved_words
    class _Spy:
        def __init__(self) -> None:
            self.calls = 0

        def transact_get_items(self, **_: object) -> None:
            self.calls += 1

        def batch_get_item(self, **_: object) -> None:
            self.calls += 1

        def scan(self, **_: object) -> None:
            self.calls += 1

    spy = _Spy()
    for operation, request in (
        ("transact_get_items", {"TransactItems": [{"Get": {"ProjectionExpression": "pk, key"}}]}),
        ("batch_get_item", {"RequestItems": {"t": {"ProjectionExpression": "audit, expiry, key"}}}),
        ("scan", {"FilterExpression": "status = :s"}),
    ):
        with pytest.raises(ClientError, match="reserved keyword"):
            getattr(spy, operation)(**request)
    assert spy.calls == 0
    spy.scan(FilterExpression="#status = :s")
    assert spy.calls == 1
    refuse_reserved("query", {"KeyConditionExpression": "pk = :pk"})
