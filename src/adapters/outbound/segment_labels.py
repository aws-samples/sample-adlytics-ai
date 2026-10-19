# SPDX-License-Identifier: MIT-0
"""Read segment names and providers from CURRENT `dim_audience_segment` publications.

Each namespace has its own writer (pack 9 for `amc_dsp_behavior`, pack 13 for `amc_asi`), so
each has its own publication scope and history, and a request reads only its namespace's
writer: an ID is never looked up in the other namespace (ADR-103 point 6). An ID is unique only
within its segment marketplace, so only the requested marketplace's scope and its
`segment_marketplace_key` rows are read. A dimension
publication carries the whole folded SCD2 history (write discipline §2.1), so the latest CURRENT
publication of a scope is read and the version valid on the requested day is chosen.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time

from aws_lambda_powertools import Logger

from adapters.outbound.entity_catalog import LATEST_ONLY, publication_order
from adapters.outbound.publication_selection import PublicationScopeRequest, publication_scopes_for
from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from domain.data_contracts.contract import DataContract
from domain.semantics.audience_segments import (
    SegmentLabel,
    SegmentNamespace,
    SegmentProvider,
)
from domain.semantics.commerce_identity import CommerceIdentityBridge
from domain.semantics.source_pack import SourcePack
from domain.shared_kernel.errors import DomainError, ValidationError
from ports.outbound.canonical import CanonicalDatasetReadPort
from ports.outbound.segment_labels import SegmentLabelPort, SegmentLabelRequest
from ports.outbound.semantic_state import CurrentPublicationPort

__all__ = ["NAMESPACE_WRITERS", "BakePinnedSegmentLabels", "CanonicalSegmentLabels"]

NAMESPACE_WRITERS: Mapping[SegmentNamespace, str] = {
    SegmentNamespace.AMC_DSP_BEHAVIOR: "amc-dsp-segment-names",
    SegmentNamespace.AMC_ASI: "amc-segment-metadata",
}
"""The one contract writing each namespace of `dim_audience_segment`."""


@dataclass(frozen=True, slots=True)
class CanonicalSegmentLabels(SegmentLabelPort):
    """Label segments from the namespace writer's latest CURRENT dimension history."""

    current: CurrentPublicationPort
    canonical: CanonicalDatasetReadPort
    packs: tuple[SourcePack, ...]
    contracts: tuple[DataContract, ...]
    bridge: CommerceIdentityBridge

    def labels(self, request: SegmentLabelRequest) -> Mapping[str, SegmentLabel]:
        """Return each requested segment's version valid on the day, by ID."""
        wanted = frozenset(request.segment_ids)
        if not wanted:
            return {}
        writer = NAMESPACE_WRITERS[request.namespace]
        pack = next(item for item in self.packs if item.contract_id == writer)
        contract = next(item for item in self.contracts if item.contract_id == writer)
        moment = datetime.combine(request.on, time.min, tzinfo=UTC)
        try:
            # The requested marketplace's scopes only: a marketplace with no names publication
            # names nothing, rather than borrowing another marketplace's.
            scopes = publication_scopes_for(
                PublicationScopeRequest(
                    pack=pack,
                    contract=contract,
                    bridge=self.bridge,
                    filters={"marketplace_key": request.marketplace_key},
                )
            )
        except ValueError:
            return {}  # outside the governed scope: no segment is named
        found: dict[str, set[SegmentLabel]] = {}
        for scope in scopes:
            current = self.current.current_for_scope(scope, horizon=LATEST_ONLY)
            if not current:
                continue
            for raw in self.canonical.rows(max(current, key=publication_order)):
                row = dict(raw)
                segment_id = str(row["segment_id"])
                if (
                    row["segment_namespace"] != request.namespace.value
                    or row.get("segment_marketplace_key") != scope.population_key
                    or segment_id not in wanted
                ):
                    continue
                valid_from, valid_to = row["valid_from"], row["valid_to"]
                if not (
                    isinstance(valid_from, datetime)
                    and isinstance(valid_to, datetime)
                    and valid_from <= moment < valid_to
                ):
                    continue
                name, provider = row.get("segment_name"), row.get("provider")
                level_1, path = row.get("category_level_1"), row.get("category_path")
                found.setdefault(segment_id, set()).add(
                    SegmentLabel(
                        namespace=request.namespace,
                        segment_id=segment_id,
                        name=name if isinstance(name, str) else None,
                        provider=None if provider is None else SegmentProvider(str(provider)),
                        category_level_1=level_1 if isinstance(level_1, str) else None,
                        category_path=path if isinstance(path, str) else None,
                    )
                )
        # The key makes one version per ID, marketplace and day; two can't be told apart.
        return {key: next(iter(items)) for key, items in found.items() if len(items) == 1}


_PROBE_LOG = Logger(service="adlytics-preset-artifact-baker")
"""A suppressed probe refusal keeps its code here, so a later manifest failure has a cause."""

_PIN_PROBE_SEGMENT = "segment:pin-probe-0001"
"""A fictional segment ID a bake's empty-answer pass labels, so the label read is pinned."""


@dataclass(frozen=True, slots=True)
class BakePinnedSegmentLabels(SegmentLabelPort):
    """Segment labels for a Baker that read the label scopes even when no segment is wanted.

    A GRAPH bake's pin and capture passes answer every query empty, so no segment is requested
    and a plain port reads no CURRENT scope; the task's real render then labels real segments
    with an unpinned read. The scopes depend on the namespace and marketplace, not the IDs, so
    probing one fictional ID on an empty request reads the same scopes and labels nothing.
    """

    labels_port: SegmentLabelPort

    def labels(self, request: SegmentLabelRequest) -> Mapping[str, SegmentLabel]:
        """Return the labels; on an empty request, read the scopes and return none."""
        if not request.segment_ids:
            # The probe only pins: a designed refusal on it names nothing and never fails the
            # pin pass. A dependency failure still re-raises, as DB21 has it.
            try:
                self.labels_port.labels(replace(request, segment_ids=(_PIN_PROBE_SEGMENT,)))
            except (DomainError, ValidationError) as refusal:
                report_operation(
                    OperationLog(
                        operation="graph_bake",
                        component="bake_pin_probe",
                        phase="source_pin",
                        outcome="probe_refused",
                        severity=LogSeverity.WARNING,
                        details={"probe": "segment_labels", "error_code": str(refusal.code)},
                    ),
                    _PROBE_LOG,
                )
            return {}
        return self.labels_port.labels(request)
