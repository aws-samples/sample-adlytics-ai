# SPDX-License-Identifier: MIT-0
"""Readback of the owned evaluation result group's custody controls (ADR-079, observability §3.7).

The enablement gate enables a family only after every control IAM on application roles does not
close reads back as holding: the group exists under its ARN with the configured retention,
deletion protection, no associated key, no data-protection policy, and no log transformer; its
only subscription filter is the result ingress's; no account-level subscription-filter,
data-protection, or transformer policy exists; no delivery destination and no pending or
running export task names it; and no Logs resource policy, account- or resource-scoped, allows
anyone to write to it. The evaluation service writes through its execution role, so any
resource-policy write grant is a second writer.
A transformer rewrites events at ingestion, before subscription delivery, so one could edit a
verdict's label, score, or configuration id without any write to the group.

Every check fails closed. An account-level policy of either type fails regardless of its
selection criteria; a resource-policy statement that uses `NotAction`, `NotResource`, a policy
variable, or omits `Resource` counts as reaching the group; a document that cannot be parsed
counts as a grant. A readback that cannot finish raises rather than returning no findings.

The finding rules are pure functions over response shapes, unit-tested without an SDK stand-in.
Nothing here reads a log event.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Final, cast

from botocore.exceptions import BotoCoreError, ClientError

from adapters.outbound.aws_errors import aws_failure_context
from domain.evaluation.gate import GateAssertion, GateFinding, ResultGroupCustodyV1
from domain.shared_kernel.errors import DependencyError, ErrorCode
from ports.outbound.evaluation_gate import ResultGroupReadbackPort

if TYPE_CHECKING:  # pragma: no cover - typing only
    from types_boto3_logs.client import CloudWatchLogsClient

__all__ = [
    "EVALUATION_GATE_READBACK_FAILED",
    "CloudWatchResultGroupReadback",
    "account_policy_findings",
    "data_protection_findings",
    "delivery_destination_findings",
    "export_task_findings",
    "grants_group_write",
    "iam_pattern_matches",
    "log_group_findings",
    "resource_policy_findings",
    "subscription_findings",
    "transformer_findings",
]

EVALUATION_GATE_READBACK_FAILED: Final = ErrorCode("EVALUATION", 10)
_MAX_PAGES: Final = 50
_WRITE_ACTIONS: Final = ("logs:putlogevents", "logs:createlogstream")
_PROBE_STREAM: Final = "adlytics-gate-probe"
"""A stream name no pattern names on purpose: it stands for every stream in the group."""
_ACTIVE_EXPORTS: Final = ("PENDING", "RUNNING")
_NOT_FOUND: Final = "ResourceNotFoundException"
_WILDCARD_SUFFIX: Final = ":*"

type _Item = Mapping[str, Any]


def _bare_arn(arn: str) -> str:
    return arn.removesuffix(_WILDCARD_SUFFIX)


def iam_pattern_matches(pattern: str, value: str) -> bool:
    """Whether an IAM `*`/`?` wildcard pattern matches a value; a policy variable always does."""
    if "${" in pattern:
        return True  # a variable resolves per request, so it can name any group
    expression = "".join(
        ".*" if character == "*" else "." if character == "?" else re.escape(character)
        for character in pattern
    )
    return re.fullmatch(expression, value, flags=re.DOTALL) is not None


def _texts(value: object) -> tuple[str, ...] | None:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list) and all(
        isinstance(item, str) for item in cast("list[object]", value)
    ):
        return tuple(cast("list[str]", value))
    return None


def _writes(statement: Mapping[str, object]) -> bool:
    if "NotAction" in statement:
        return True
    actions = _texts(statement.get("Action"))
    if actions is None:
        return True
    return any(
        iam_pattern_matches(action.lower(), target)
        for action in actions
        for target in _WRITE_ACTIONS
    )


def _reaches(statement: Mapping[str, object], group_arn: str) -> bool:
    if "NotResource" in statement or "Resource" not in statement:
        return True
    resources = _texts(statement.get("Resource"))
    if resources is None:
        return True
    targets = (
        group_arn,
        f"{group_arn}{_WILDCARD_SUFFIX}",
        f"{group_arn}:log-stream:{_PROBE_STREAM}",
    )
    return any(
        iam_pattern_matches(resource, target) for resource in resources for target in targets
    )


def grants_group_write(document: str, group_arn: str) -> bool:
    """Whether a Logs resource-policy document allows any principal to write to the group."""
    try:
        parsed: object = json.loads(document)
    except ValueError:
        return True
    if not isinstance(parsed, Mapping):
        return True
    statements: object = cast("Mapping[str, object]", parsed).get("Statement")
    if isinstance(statements, Mapping):
        statements = [cast("object", statements)]
    if not isinstance(statements, list):
        return True
    for statement in cast("list[object]", statements):
        if not isinstance(statement, Mapping):
            return True
        typed = cast("Mapping[str, object]", statement)
        if typed.get("Effect") != "Allow":
            continue
        if _writes(typed) and _reaches(typed, _bare_arn(group_arn)):
            return True
    return False


def log_group_findings(custody: ResultGroupCustodyV1, group: _Item) -> tuple[GateFinding, ...]:
    """Assert the described group's identity, retention, protection, and encryption."""
    arn = group.get("logGroupArn") or _bare_arn(str(group.get("arn", "")))
    checks = (
        (GateAssertion.GROUP_ARN, arn == custody.result_log_group_arn),
        (
            GateAssertion.GROUP_RETENTION,
            group.get("retentionInDays") == custody.result_retention_days,
        ),
        (GateAssertion.GROUP_DELETION_PROTECTION, group.get("deletionProtectionEnabled") is True),
        (GateAssertion.GROUP_KEY, not group.get("kmsKeyId")),
        (GateAssertion.GROUP_DATA_PROTECTION, group.get("dataProtectionStatus") != "ACTIVATED"),
    )
    return tuple(GateFinding(assertion) for assertion, holds in checks if not holds)


def data_protection_findings(document: str) -> tuple[GateFinding, ...]:
    """Fail custody on a group-level data-protection policy of any content."""
    return (GateFinding(GateAssertion.GROUP_DATA_PROTECTION),) if document.strip() else ()


def transformer_findings(transformer_config: Iterable[_Item]) -> tuple[GateFinding, ...]:
    """Fail custody on a group-level transformer with any processor."""
    has_processor = any(True for _ in transformer_config)
    return (GateFinding(GateAssertion.GROUP_TRANSFORMER),) if has_processor else ()


def subscription_findings(
    custody: ResultGroupCustodyV1, filters: Iterable[_Item]
) -> tuple[GateFinding, ...]:
    """Exactly one subscription filter, the result ingress's, delivering to the ingress."""
    listed = list(filters)
    holds = (
        len(listed) == 1
        and listed[0].get("filterName") == custody.ingress_subscription_filter_name
        and listed[0].get("destinationArn") == custody.ingress_function_arn
    )
    return () if holds else (GateFinding(GateAssertion.SUBSCRIPTION_FILTERS),)


def account_policy_findings(
    policies: Iterable[_Item], assertion: GateAssertion
) -> tuple[GateFinding, ...]:
    """Any account-level policy of the type fails custody, whatever its selection criteria."""
    return (GateFinding(assertion),) if any(True for _ in policies) else ()


def delivery_destination_findings(
    custody: ResultGroupCustodyV1, destinations: Iterable[_Item]
) -> tuple[GateFinding, ...]:
    """No delivery destination may name the group."""
    for destination in destinations:
        configuration = cast("_Item", destination.get("deliveryDestinationConfiguration", {}))
        target = str(configuration.get("destinationResourceArn", ""))
        if _bare_arn(target) == custody.result_log_group_arn:
            return (GateFinding(GateAssertion.DELIVERY_DESTINATION),)
    return ()


def export_task_findings(
    custody: ResultGroupCustodyV1, tasks: Iterable[_Item]
) -> tuple[GateFinding, ...]:
    """No pending or running export task may read the group."""
    if any(task.get("logGroupName") == custody.result_log_group_name for task in tasks):
        return (GateFinding(GateAssertion.EXPORT_TASK),)
    return ()


def resource_policy_findings(
    custody: ResultGroupCustodyV1, policies: Iterable[_Item]
) -> tuple[GateFinding, ...]:
    """No Logs resource policy may allow anyone to write to the group."""
    for policy in policies:
        if grants_group_write(str(policy.get("policyDocument", "")), custody.result_log_group_arn):
            return (GateFinding(GateAssertion.RESOURCE_POLICY_WRITE),)
    return ()


def _translated(error: ClientError | BotoCoreError, operation: str) -> DependencyError:
    return DependencyError(
        EVALUATION_GATE_READBACK_FAILED,
        f"{operation} failed.",
        context={"operation": operation, **aws_failure_context(error)},
    )


def _not_found(error: ClientError | BotoCoreError) -> bool:
    return isinstance(error, ClientError) and (
        error.response.get("Error", {}).get("Code") == _NOT_FOUND
    )


class CloudWatchResultGroupReadback(ResultGroupReadbackPort):
    """Read back every custody control on one result group with one Logs client."""

    def __init__(self, client: CloudWatchLogsClient) -> None:
        self._client = client

    def _paged(
        self,
        operation: str,
        call: Callable[..., Any],
        member: str,
        **request: object,
    ) -> list[_Item]:
        items: list[_Item] = []
        token: str | None = None
        for _ in range(_MAX_PAGES):
            arguments = {**request, **({"nextToken": token} if token else {})}
            try:
                response = cast("_Item", call(**arguments))
            except (ClientError, BotoCoreError) as error:
                raise _translated(error, operation) from error
            items.extend(cast("list[_Item]", response.get(member, [])))
            token = cast("str | None", response.get("nextToken"))
            if not token:
                return items
        raise DependencyError(
            EVALUATION_GATE_READBACK_FAILED,
            f"{operation} did not finish within {_MAX_PAGES} pages.",
            context={"operation": operation},
        )

    def _group(self, custody: ResultGroupCustodyV1) -> _Item | None:
        try:
            # aws-pagination: one exact log group identifier names at most one group.
            response = self._client.describe_log_groups(
                logGroupIdentifiers=[custody.result_log_group_name]
            )
        except (ClientError, BotoCoreError) as error:
            if _not_found(error):
                return None
            raise _translated(error, "DescribeLogGroups") from error
        return next(
            (
                cast("_Item", group)
                for group in response.get("logGroups", [])
                if group.get("logGroupName") == custody.result_log_group_name
            ),
            None,
        )

    def _data_protection(self, custody: ResultGroupCustodyV1) -> str:
        try:
            response = self._client.get_data_protection_policy(
                logGroupIdentifier=custody.result_log_group_name
            )
        except (ClientError, BotoCoreError) as error:
            if _not_found(error):
                return ""
            raise _translated(error, "GetDataProtectionPolicy") from error
        return response.get("policyDocument", "")

    def _transformer(self, custody: ResultGroupCustodyV1) -> list[_Item]:
        try:
            response = self._client.get_transformer(
                logGroupIdentifier=custody.result_log_group_name
            )
        except (ClientError, BotoCoreError) as error:
            if _not_found(error):
                return []
            raise _translated(error, "GetTransformer") from error
        return cast("list[_Item]", response.get("transformerConfig", []))

    def findings(self, custody: ResultGroupCustodyV1) -> tuple[GateFinding, ...]:
        """Return every failed custody assertion on the custody value's result group."""
        group = self._group(custody)
        if group is None:
            return (GateFinding(GateAssertion.GROUP_EXISTS),)
        client = self._client
        filters = self._paged(
            "DescribeSubscriptionFilters",
            client.describe_subscription_filters,
            "subscriptionFilters",
            logGroupName=custody.result_log_group_name,
        )
        subscription_policies = self._paged(
            "DescribeAccountPolicies",
            client.describe_account_policies,
            "accountPolicies",
            policyType="SUBSCRIPTION_FILTER_POLICY",
        )
        data_protection_policies = self._paged(
            "DescribeAccountPolicies",
            client.describe_account_policies,
            "accountPolicies",
            policyType="DATA_PROTECTION_POLICY",
        )
        transformer_policies = self._paged(
            "DescribeAccountPolicies",
            client.describe_account_policies,
            "accountPolicies",
            policyType="TRANSFORMER_POLICY",
        )
        destinations = self._paged(
            "DescribeDeliveryDestinations",
            client.describe_delivery_destinations,
            "deliveryDestinations",
        )
        exports = [
            task
            for status in _ACTIVE_EXPORTS
            for task in self._paged(
                "DescribeExportTasks",
                client.describe_export_tasks,
                "exportTasks",
                statusCode=status,
            )
        ]
        policies = [
            *self._paged(
                "DescribeResourcePolicies",
                client.describe_resource_policies,
                "resourcePolicies",
                policyScope="ACCOUNT",
            ),
            *self._paged(
                "DescribeResourcePolicies",
                client.describe_resource_policies,
                "resourcePolicies",
                resourceArn=custody.result_log_group_arn,
                policyScope="RESOURCE",
            ),
        ]
        return (
            *log_group_findings(custody, group),
            *data_protection_findings(self._data_protection(custody)),
            *transformer_findings(self._transformer(custody)),
            *subscription_findings(custody, filters),
            *account_policy_findings(
                subscription_policies, GateAssertion.ACCOUNT_SUBSCRIPTION_POLICY
            ),
            *account_policy_findings(
                data_protection_policies, GateAssertion.ACCOUNT_DATA_PROTECTION_POLICY
            ),
            *account_policy_findings(
                transformer_policies, GateAssertion.ACCOUNT_TRANSFORMER_POLICY
            ),
            *delivery_destination_findings(custody, destinations),
            *export_task_findings(custody, exports),
            *resource_policy_findings(custody, policies),
        )
