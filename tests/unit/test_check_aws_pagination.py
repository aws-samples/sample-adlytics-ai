# SPDX-License-Identifier: MIT-0
"""The pagination gate refuses a first-page-only AWS read and accepts each paginated form.

Each refusal compares the whole finding list, and each accepted form asserts an empty one, so a
rule that stopped refusing and a rule that started refusing compliant code both fail here. The
operation set is botocore's own, read offline from the locked SDK.
"""

import importlib.util
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from types import ModuleType
from typing import Any

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-134")]

ROOT = Path(__file__).resolve().parents[2]
REL = "src/x.py"
ONE_PAGE = (
    "reads one page; use get_paginator, loop on its input token, or justify an exact-target "
    "call with '# aws-pagination: <reason>'"
)
STALE = "aws-pagination justification is attached to no call it can excuse"
Positions = frozenset[frozenset[str]]


@pytest.fixture(scope="module")
def checker() -> ModuleType:
    """Load the hyphenated script as a module so `findings` is called directly."""
    spec = importlib.util.spec_from_file_location(
        "check_aws_pagination", ROOT / "scripts" / "check-aws-pagination.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def operations(checker: ModuleType) -> dict[str, Positions]:
    return checker.paginated_operations()


@pytest.fixture(scope="module")
def judge(checker: ModuleType, operations: dict[str, Positions]) -> Callable[[str], Any]:
    """Judge one source text against the real model set; returns the gate's Result."""

    def run(source: str) -> Any:
        return checker.findings(REL, dedent(source), operations)

    return run


def test_the_models_say_list_stack_resources_pages_and_describe_stack_resources_does_not(
    operations: dict[str, Positions],
) -> None:
    assert operations["list_stack_resources"] == frozenset({frozenset({"NextToken"})})
    assert "describe_stack_resources" not in operations
    # A composite position stays whole: S3 resumes a version listing at a key and a version id.
    assert operations["list_object_versions"] == frozenset(
        {frozenset({"KeyMarker", "VersionIdMarker"})}
    )
    # Positions from different services stay apart: Glacier's one marker, S3's two.
    assert operations["list_multipart_uploads"] == frozenset(
        {frozenset({"marker"}), frozenset({"KeyMarker", "UploadIdMarker"})}
    )
    assert "describe_continuous_backups" not in operations
    # A name pages if any service pages it: Redshift Data's DescribeTable does, DynamoDB's not.
    assert "describe_table" in operations


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            """
            def f(cloudformation):
                return cloudformation.describe_stack_resources(StackName="s")
            """,
            [
                f"{REL}:3: describe_stack_resources stops at 100 resources with no continuation "
                "token; use adapters.outbound.cloudformation_stack.stack_resource_summaries"
            ],
            id="describe-stack-resources",
        ),
        pytest.param(
            """
            def f(cloudformation):
                # aws-pagination: a reason cannot excuse it
                return cloudformation.describe_stack_resources(StackName="s")
            """,
            [
                f"{REL}:4: describe_stack_resources stops at 100 resources with no continuation "
                "token; use adapters.outbound.cloudformation_stack.stack_resource_summaries",
                f"{REL}:3: {STALE}",
            ],
            id="describe-stack-resources-justified",
        ),
        pytest.param(
            """
            def f(s3, bucket, key_marker):
                return (
                    s3.list_object_versions(Bucket=bucket)
                    if key_marker is None
                    else s3.list_object_versions(Bucket=bucket, KeyMarker=key_marker)
                )
            """,
            [
                f"{REL}:4: list_object_versions {ONE_PAGE}",
                f"{REL}:6: list_object_versions {ONE_PAGE}",
            ],
            id="part-of-a-composite-position",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                request = {"Bucket": bucket}
                while True:
                    page = s3.list_object_versions(**request)
                    request["KeyMarker"] = page["NextKeyMarker"]
            """,
            [f"{REL}:5: list_object_versions {ONE_PAGE}"],
            id="part-of-a-composite-position-in-a-request",
        ),
        pytest.param(
            """
            def f(cloudformation):
                return cloudformation.list_stack_resources(StackName="s")
            """,
            [f"{REL}:3: list_stack_resources {ONE_PAGE}"],
            id="bare-list-stack-resources",
        ),
        pytest.param(
            """
            def f(logs, name):
                return logs.describe_log_groups(logGroupNamePrefix=name)["logGroups"]
            """,
            [f"{REL}:3: describe_log_groups {ONE_PAGE}"],
            id="bare-describe-log-groups-prefix",
        ),
        pytest.param(
            """
            def f(logs, name):
                return logs.describe_log_groups(logGroupNamePrefix=name, limit=50)
            """,
            [f"{REL}:3: describe_log_groups {ONE_PAGE}"],
            id="a-limit-is-not-a-token",
        ),
        pytest.param(
            """
            def f(stacks, name):
                return stacks.describe_stacks(StackName=name)  # aws-pagination:
            """,
            [f"{REL}:3: describe_stacks carries an aws-pagination justification with no reason"],
            id="justification-without-reason",
        ),
        pytest.param(
            """
            def f(dynamodb, name):
                # aws-pagination: one table
                return dynamodb.describe_continuous_backups(TableName=name)
            """,
            [f"{REL}:3: {STALE}"],
            id="stale-justification-on-a-non-paginated-call",
        ),
        pytest.param(
            """
            def f(cloudformation):
                # aws-pagination: stale once the read paged
                return cloudformation.get_paginator("list_stack_resources")
            """,
            [f"{REL}:3: {STALE}"],
            id="stale-justification-on-a-paginator",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                return s3.list_objects_v2(Bucket=bucket)

            def g(s3, bucket):
                request = {"Bucket": bucket}
                while True:
                    page = s3.list_objects_v2(**request)
                    request["ContinuationToken"] = page["NextContinuationToken"]
            """,
            [f"{REL}:3: list_objects_v2 {ONE_PAGE}"],
            id="a-token-call-in-another-function-is-not-a-sibling",
        ),
        pytest.param(
            """
            def f(s3):
                def list_objects_v2(**kwargs):
                    return kwargs

                token = None
                while True:
                    token = list_objects_v2(ContinuationToken=token)
                    page = s3.list_objects_v2(Bucket="bucket")
            """,
            [f"{REL}:9: list_objects_v2 {ONE_PAGE}"],
            id="a-bare-token-call-is-not-a-sibling",
        ),
        pytest.param(
            """
            def f(s3, token):
                return s3.list_objects_v2(Bucket="b", ContinuationToken=token)
            """,
            [f"{REL}:3: list_objects_v2 {ONE_PAGE}"],
            id="a-token-call-outside-a-loop",
        ),
        pytest.param(
            """
            def f(s3):
                request = {"Bucket": "b"}
                page = s3.list_objects_v2(**request)
                if False:
                    request["ContinuationToken"] = "never"
                return page
            """,
            [f"{REL}:4: list_objects_v2 {ONE_PAGE}"],
            id="a-request-token-assigned-outside-a-loop",
        ),
        pytest.param(
            """
            def f(s3):
                request = {"Bucket": "b"}
                while True:
                    page = s3.list_objects_v2(**request)
                    if False:
                        request["ContinuationToken"] = page["NextContinuationToken"]
            """,
            [f"{REL}:5: list_objects_v2 {ONE_PAGE}"],
            id="an-unreachable-request-token-in-a-loop",
        ),
        pytest.param(
            """
            def f(s3, token):
                request = {"Bucket": "b"}
                while True:
                    page = s3.list_objects_v2(**request)
                    request["ContinuationToken"] = token
            """,
            [f"{REL}:5: list_objects_v2 {ONE_PAGE}"],
            id="an-unrelated-request-token-in-a-loop",
        ),
        pytest.param(
            """
            def f(s3, tokens):
                for token in tokens:
                    s3.list_objects_v2(Bucket="b", ContinuationToken=token)
            """,
            [f"{REL}:4: list_objects_v2 {ONE_PAGE}"],
            id="a-token-keyword-not-from-the-response",
        ),
        pytest.param(
            """
            def f(s3, other):
                while True:
                    page = s3.list_objects_v2(Bucket="b", ContinuationToken=other.token())
                    request = other.list_objects_v2(Bucket="o", ContinuationToken=page["Next"])
            """,
            [f"{REL}:4: list_objects_v2 {ONE_PAGE}", f"{REL}:5: list_objects_v2 {ONE_PAGE}"],
            id="a-token-from-another-calls-response",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                first = s3.list_objects_v2(Bucket=bucket)
                token = first.get("NextContinuationToken")
                while token:
                    page = s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    token = page.get("NextContinuationToken")
            """,
            [f"{REL}:3: list_objects_v2 {ONE_PAGE}"],
            id="a-same-method-call-outside-the-loop-is-not-covered",
        ),
        pytest.param(
            """
            def f(primary, secondary):
                token = None
                while True:
                    page = primary.list_objects_v2(Bucket="a", ContinuationToken=token)
                    unrelated = secondary.list_objects_v2(Bucket="b")
                    token = page.get("NextContinuationToken")
                    if not token:
                        return unrelated
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="a-different-receivers-same-operation-call-in-the-loop-is-not-covered",
        ),
        pytest.param(
            """
            def f(primary, secondary, bucket):
                token = None
                while True:
                    page = (
                        secondary.list_objects_v2(Bucket=bucket)
                        if token is None
                        else primary.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="a-different-receivers-first-page-branch-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket, seed):
                token = None
                while True:
                    page = (
                        (s3.list_objects_v2(Bucket=bucket), seed)[1]
                        if token is None
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return page
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="a-first-page-call-discarded-inside-the-assigned-expression-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket=bucket)
                        if token is None
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    other = s3.list_objects_v2(Bucket="other")
                    token = page.get("NextContinuationToken")
                    if not token:
                        return other
            """,
            [f"{REL}:10: list_objects_v2 {ONE_PAGE}"],
            id="a-same-receiver-call-beside-the-chain-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, use_other):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket="unrelated")
                        if use_other
                        else s3.list_objects_v2(Bucket="chain", ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="another-request-under-a-test-that-is-not-the-token-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket="other")
                        if token is None
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="another-request-in-the-first-page-branch-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket, use_other):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket=bucket)
                        if use_other
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="the-same-request-under-a-test-that-is-not-the-token-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket=bucket)
                        if token
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:6: list_objects_v2 {ONE_PAGE}"],
            id="a-first-page-call-taken-while-the-token-is-present-is-not-covered",
        ),
        pytest.param(
            """
            def f(primary, secondary, bucket):
                token = None
                client = secondary
                while True:
                    if token:
                        client = primary
                        page = client.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    else:
                        client = secondary
                        page = client.list_objects_v2(Bucket=bucket)
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:11: list_objects_v2 {ONE_PAGE}"],
            id="a-receiver-rebound-in-the-loop-is-not-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = None
                while True:
                    if token:
                        request_bucket = bucket
                        page = s3.list_objects_v2(Bucket=request_bucket, ContinuationToken=token)
                    else:
                        request_bucket = "other"
                        page = s3.list_objects_v2(Bucket=request_bucket)
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            [f"{REL}:10: list_objects_v2 {ONE_PAGE}"],
            id="a-request-value-rebound-in-the-loop-is-not-covered",
        ),
        pytest.param(
            """
            class Reader:
                def f(self, primary, secondary, bucket):
                    token = None
                    while True:
                        if token:
                            self.s3 = primary
                            page = self.s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                        else:
                            self.s3 = secondary
                            page = self.s3.list_objects_v2(Bucket=bucket)
                        token = page.get("NextContinuationToken")
                        if not token:
                            return
            """,
            [f"{REL}:11: list_objects_v2 {ONE_PAGE}"],
            id="an-attribute-receiver-rebound-in-the-loop-is-not-covered",
        ),
        pytest.param(
            """
            def f(stacks, logs, name):
                stack = stacks.describe_stacks(StackName=name)  # aws-pagination: one exact stack
                return stack, logs.describe_log_groups(logGroupNamePrefix=name)
            """,
            [f"{REL}:4: describe_log_groups {ONE_PAGE}"],
            id="a-trailing-marker-does-not-justify-the-next-line",
        ),
    ],
)
def test_refused_forms(judge: Callable[[str], Any], source: str, expected: list[str]) -> None:
    assert judge(source).findings == expected


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
            def f(cloudformation, name):
                paginator = cloudformation.get_paginator("list_stack_resources")
                pages = paginator.paginate(StackName=name)
                return [item for page in pages for item in page["StackResourceSummaries"]]
            """,
            id="get-paginator",
        ),
        pytest.param(
            """
            def f(s3, bucket, prefix):
                key_marker = version_marker = None
                while True:
                    page = (
                        s3.list_object_versions(Bucket=bucket, Prefix=prefix)
                        if key_marker is None
                        else s3.list_object_versions(
                            Bucket=bucket,
                            Prefix=prefix,
                            KeyMarker=key_marker,
                            VersionIdMarker=version_marker,
                        )
                    )
                    key_marker = page.get("NextKeyMarker")
                    version_marker = page.get("NextVersionIdMarker")
                    if not key_marker:
                        return
            """,
            id="manual-composite-marker-loop",
        ),
        pytest.param(
            """
            def f(primary, bucket):
                token = None
                while True:
                    page = (
                        primary.list_objects_v2(Bucket=bucket)
                        if token is None
                        else primary.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return
            """,
            id="a-same-receivers-first-page-branch-is-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket, seed):
                token = None
                while True:
                    page = (
                        s3.list_objects_v2(Bucket=bucket)
                        if token is None
                        else s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    )
                    token = page.get("NextContinuationToken")
                    if not token:
                        return page
            """,
            id="a-first-page-call-selected-as-the-assigned-value-is-covered",
        ),
        pytest.param(
            """
            class Reader:
                def f(self, bucket):
                    token = None
                    while True:
                        if token:
                            page = self.s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                        else:
                            page = self.s3.list_objects_v2(Bucket=bucket)
                        token = page.get("NextContinuationToken")
                        if not token:
                            return
            """,
            id="a-same-receivers-first-page-statement-branch-is-covered",
        ),
        pytest.param(
            """
            def f(primary, secondary, bucket):
                token = None
                client = primary
                while True:
                    seen = 0
                    if token:
                        page = client.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    else:
                        page = client.list_objects_v2(Bucket=bucket)
                    seen += len(page.get("Contents", []))
                    token = page.get("NextContinuationToken")
                    if not token:
                        return seen
            """,
            id="a-receiver-bound-before-the-loop-with-other-names-rebound-is-covered",
        ),
        pytest.param(
            """
            class Reader:
                def f(self, bucket):
                    token = None
                    while True:
                        if token:
                            page = self.s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                        else:
                            page = self.s3.list_objects_v2(Bucket=bucket)
                        self.pages = page
                        token = page.get("NextContinuationToken")
                        if not token:
                            return
            """,
            id="a-stable-attribute-receiver-beside-another-attribute-write-is-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = ""
                while True:
                    if not token:
                        page = s3.list_objects_v2(Bucket=bucket)
                    else:
                        page = s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    token = page.get("NextContinuationToken", "")
                    if token == "":
                        return
            """,
            id="a-first-page-branch-under-a-negated-token-test-is-covered",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                request = {"Bucket": bucket}
                while True:
                    page = s3.list_object_versions(**request)
                    request["KeyMarker"] = page["NextKeyMarker"]
                    request["VersionIdMarker"] = page["NextVersionIdMarker"]
            """,
            id="manual-composite-request-loop",
        ),
        pytest.param(
            """
            def f(s3, bucket, version_marker):
                request = {"Bucket": bucket}
                while True:
                    page = s3.list_object_versions(**request, VersionIdMarker=version_marker)
                    request["KeyMarker"] = page["NextKeyMarker"]
                    version_marker = page["NextVersionIdMarker"]
            """,
            id="composite-position-split-across-request-and-keyword",
        ),
        pytest.param(
            """
            def f(cognito, pool):
                request = {"UserPoolId": pool}
                while True:
                    response = cognito.list_users(**request)
                    if not response.get("PaginationToken"):
                        return
                    request["PaginationToken"] = response["PaginationToken"]
            """,
            id="manual-request-dict-loop",
        ),
        pytest.param(
            """
            def f(cognito, pool):
                token = None
                while True:
                    request = {"UserPoolId": pool}
                    if token is not None:
                        request["NextToken"] = token
                    response = cognito.list_users(**request)
                    next_token = response.get("NextToken")
                    if not next_token:
                        return
                    token = str(next_token)
            """,
            id="a-token-derived-through-names",
        ),
        pytest.param(
            """
            def f(s3, bucket):
                token = ""
                for _ in range(10):
                    page = s3.list_objects_v2(Bucket=bucket, ContinuationToken=token)
                    if not (token := page.get("NextContinuationToken", "")):
                        return
                raise RuntimeError("page cap")
            """,
            id="a-for-loop-with-a-walrus-feed",
        ),
        pytest.param(
            """
            def f(stacks, name):
                # aws-pagination: an exact StackName returns one stack
                return stacks.describe_stacks(StackName=name)
            """,
            id="justified-describe-stacks",
        ),
        pytest.param(
            """
            def f(dynamodb, name):
                return dynamodb.describe_continuous_backups(TableName=name)
            """,
            id="non-paginated-describe",
        ),
        pytest.param(
            """
            def describe_stacks():
                return describe_stacks()
            """,
            id="a-bare-function-call-is-not-a-client-call",
        ),
    ],
)
def test_accepted_forms(judge: Callable[[str], Any], source: str) -> None:
    assert judge(source).findings == []


def test_a_justified_site_is_listed_with_its_reason(judge: Callable[[str], Any]) -> None:
    result = judge(
        """
        def f(stacks, name):
            return stacks.describe_stacks(StackName=name)  # aws-pagination: one exact stack
        """
    )
    assert result.findings == []
    assert result.justified == [f"{REL}:3: describe_stacks — one exact stack"]
