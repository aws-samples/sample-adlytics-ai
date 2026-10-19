# SPDX-License-Identifier: MIT-0
"""Attest deployed campaign preset parents and every immutable format-4 sibling."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, NoReturn

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from types_boto3_ssm.client import SSMClient

from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
from adapters.outbound.s3.preset_artifacts import (
    S3PresetArtifactBodyStore,
    S3PresetArtifactCustodyStore,
)
from domain.presentation.campaign_attribution import SafeCampaignDisplaySignature
from domain.presentation.campaign_custody import (
    campaign_custody_manifest_from_payload,
    custody_rows_align,
)
from domain.presentation.campaign_portfolio import (
    CampaignPortfolioRow,
    CampaignPortfolioSort,
    CampaignPortfolioSortField,
    campaign_portfolio_artifact_from_payload,
    campaign_portfolio_segment_from_payload,
    order_campaign_portfolio_rows,
)
from domain.presentation.preset_artifacts import (
    PresetArtifactPage,
    PresetArtifactSiblingReadRequest,
    PresetArtifactVariant,
)

_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"
_PUBLISHER_STACK: Final = "Adlytics-Publisher-dev"
_STORE_STACK: Final = "Adlytics-PublisherArtifactStore-dev"
_ACCOUNT_DIGITS: Final = 12
_VARIANTS: Final = tuple(PresetArtifactVariant(value) for value in ("1d", "7d", "14d"))


class ProbeError(RuntimeError):
    """A bounded live artifact attestation failure safe to print."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        del message
        raise ProbeError("invalid explicit target arguments")


@dataclass(frozen=True, slots=True)
class ProbeRequest:
    """Every explicit deployment target used by the read-only probe."""

    profile: str
    environment: str
    region: str
    expected_account: str
    publisher_stack: str
    store_stack: str


@dataclass(frozen=True, slots=True)
class VariantResult:
    """Bounded counts from one fully attested variant."""

    variant: str
    forward_token: int
    total_campaigns: int
    parent_rows: int
    segment_count: int
    sibling_pairs: int


def _non_blank(value: str) -> str:
    selected = value.strip()
    if not selected:
        raise argparse.ArgumentTypeError("value must not be blank")
    return selected


def _profile(value: str) -> str:
    selected = _non_blank(value)
    if selected.casefold() == "default":
        raise argparse.ArgumentTypeError("an explicit nondefault profile is required")
    return selected


def _account(value: str) -> str:
    selected = _non_blank(value)
    if len(selected) != _ACCOUNT_DIGITS or not selected.isdigit():
        raise argparse.ArgumentTypeError("expected account must be 12 digits")
    return selected


def _arguments() -> ProbeRequest:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=_profile)
    parser.add_argument("--environment", required=True, type=_non_blank)
    parser.add_argument("--region", required=True, type=_non_blank)
    parser.add_argument("--expected-account", required=True, type=_account)
    parser.add_argument("--publisher-stack", required=True, type=_non_blank)
    parser.add_argument("--store-stack", required=True, type=_non_blank)
    args = parser.parse_args()
    return ProbeRequest(
        args.profile,
        args.environment,
        args.region,
        args.expected_account,
        args.publisher_stack,
        args.store_stack,
    )


def _validate_target(request: ProbeRequest) -> None:
    if request.environment != _ENVIRONMENT or request.region != _REGION:
        raise ProbeError("probe accepts only the governed dev target")
    if request.publisher_stack != _PUBLISHER_STACK or request.store_stack != _STORE_STACK:
        raise ProbeError("probe requires the exact publisher dev stacks")


@dataclass(frozen=True, slots=True)
class _ArtifactStores:
    """Typed deployed stores used by one variant attestation."""

    pointers: DynamoPresetArtifactPointerStore
    bodies: S3PresetArtifactBodyStore
    custody: S3PresetArtifactCustodyStore


def _attest_variant(  # noqa: PLR0912 - each branch is an independent attestation gate
    variant: PresetArtifactVariant,
    stores: _ArtifactStores,
) -> VariantResult:
    page = PresetArtifactPage("campaigns")
    pointer = stores.pointers.current(page, variant)
    if pointer is None or pointer.custody_digest is None:
        raise ProbeError("campaign preset pointer or custody is absent")
    body = stores.bodies.read(pointer)
    custody = stores.custody.read(pointer)
    try:
        parent = campaign_portfolio_artifact_from_payload(json.loads(body.payload_json))
        manifest = campaign_custody_manifest_from_payload(json.loads(custody.payload_json))
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ProbeError("campaign parent or custody payload is malformed") from error
    pagination = parent.pagination
    if (
        manifest.body_digest != body.digest
        or pagination is None
        or pagination.segment_index != 0
        or not custody_rows_align(manifest, tuple(row.signature for row in parent.rows))
    ):
        raise ProbeError("campaign parent pagination or custody alignment failed")
    descriptors = {item.identity: item for item in manifest.descriptors}
    if len(descriptors) != len(manifest.descriptors):
        raise ProbeError("campaign sibling descriptors repeat an identity")
    expected_identities = {
        (field.value, direction, segment_index)
        for field in CampaignPortfolioSortField
        for direction in ("asc", "desc")
        for segment_index in range(pagination.segment_count)
        if not (
            field is CampaignPortfolioSortField.SPEND
            and direction == "desc"
            and segment_index == 0
        )
    }
    observed_identities = {
        (
            descriptor.identity.ordering.rsplit("-", maxsplit=1)[0],
            descriptor.identity.ordering.rsplit("-", maxsplit=1)[1],
            descriptor.identity.segment_index,
        )
        for descriptor in manifest.descriptors
    }
    if observed_identities != expected_identities:
        raise ProbeError("campaign sibling descriptor index is incomplete")
    segments: dict[str, dict[int, tuple[CampaignPortfolioRow, ...]]] = {
        f"{field.value}-{direction}": {}
        for field in CampaignPortfolioSortField
        for direction in ("asc", "desc")
    }
    segments["spend-desc"][0] = tuple(parent.rows)
    for descriptor in manifest.descriptors:
        request = PresetArtifactSiblingReadRequest(
            pointer.page,
            pointer.variant,
            pointer.revision,
            descriptor,
        )
        public = stores.bodies.read_sibling(request)
        private = stores.custody.read_sibling(request)
        try:
            segment = campaign_portfolio_segment_from_payload(
                json.loads(public.payload_json), parent
            )
            segment_manifest = campaign_custody_manifest_from_payload(
                json.loads(private.payload_json)
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise ProbeError("campaign sibling payload is malformed") from error
        if (
            segment.pagination is None
            or segment.pagination.segment_index != descriptor.identity.segment_index
            or segment.pagination.segment_count != pagination.segment_count
            or segment_manifest.descriptors
            or segment_manifest.body_digest != public.digest
            or not custody_rows_align(
                segment_manifest,
                tuple(row.signature for row in segment.rows),
            )
        ):
            raise ProbeError("campaign sibling pagination or custody alignment failed")
        segments[descriptor.identity.ordering][descriptor.identity.segment_index] = tuple(
            segment.rows
        )
    baseline_signatures: set[SafeCampaignDisplaySignature] | None = None
    for field in CampaignPortfolioSortField:
        for direction in ("asc", "desc"):
            ordering = f"{field.value}-{direction}"
            selected = segments[ordering]
            if set(selected) != set(range(pagination.segment_count)):
                raise ProbeError("campaign ordering has a missing segment")
            rows = tuple(
                row
                for segment_index in range(pagination.segment_count)
                for row in selected[segment_index]
            )
            signatures = {row.signature for row in rows}
            if len(rows) != parent.total_count or len(signatures) != parent.total_count:
                raise ProbeError("campaign ordering loses or duplicates a row")
            if baseline_signatures is None:
                baseline_signatures = signatures
            elif signatures != baseline_signatures:
                raise ProbeError("campaign orderings cover different populations")
            expected = order_campaign_portfolio_rows(
                rows, CampaignPortfolioSort(field, direction)
            )
            if rows != expected:
                raise ProbeError("campaign ordering is not deterministic")
    return VariantResult(
        variant.value,
        pointer.forward_token,
        parent.total_count,
        len(parent.rows),
        pagination.segment_count,
        len(manifest.descriptors),
    )


def _parameter(client: SSMClient, name: str) -> str:
    response = client.get_parameter(Name=name)
    value = str(response.get("Parameter", {}).get("Value", "")).strip()
    if not value:
        raise ProbeError("a required publisher parameter resolved empty")
    return value


def _run(request: ProbeRequest) -> tuple[VariantResult, ...]:
    _validate_target(request)
    session = boto3.Session(profile_name=request.profile, region_name=request.region)
    identity = session.client("sts").get_caller_identity()
    if str(identity.get("Account", "")) != request.expected_account:
        raise ProbeError("explicit profile account differs from expected account")
    cloudformation = session.client("cloudformation")
    for stack_name in (request.publisher_stack, request.store_stack):
        # aws-pagination: an exact StackName returns one stack; the count is checked below.
        response = cloudformation.describe_stacks(StackName=stack_name)
        if len(response.get("Stacks", [])) != 1:
            raise ProbeError("an exact publisher stack is unavailable")
    ssm = session.client("ssm")
    pointer_table = _parameter(
        ssm,
        f"/adlytics/{request.environment}/publisher/artifact-pointer-table-name",
    )
    bucket = _parameter(
        ssm,
        f"/adlytics/{request.environment}/publisher/artifact-bucket-name",
    )
    dynamodb = session.client("dynamodb")
    s3 = session.client("s3")
    stores = _ArtifactStores(
        DynamoPresetArtifactPointerStore(pointer_table, dynamodb),
        S3PresetArtifactBodyStore(bucket, s3),
        S3PresetArtifactCustodyStore(bucket, s3),
    )
    return tuple(_attest_variant(variant, stores) for variant in _VARIANTS)


def main() -> int:
    """Run the bounded read-only probe without printing artifact identities."""
    try:
        results = _run(_arguments())
    except ProbeError as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", "unknown"))
        print(f"FAILED: AWS request returned {code}", file=sys.stderr)
        return 1
    except Exception as error:
        print(f"FAILED: unexpected {type(error).__name__}", file=sys.stderr)
        return 1
    print(
        "PASS: deployed campaign format-4 artifact store accepted "
        + " ".join(
            f"{item.variant}[token={item.forward_token},total={item.total_campaigns},"
            f"parent={item.parent_rows},segments={item.segment_count},"
            f"siblings={item.sibling_pairs}]"
            for item in results
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
