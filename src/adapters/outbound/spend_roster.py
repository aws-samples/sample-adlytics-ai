# SPDX-License-Identifier: MIT-0
"""Read a grain set's roster from CURRENT spend publications (plan D4, `ports.outbound.rosters`).

The roster is the keys with `ad_spend > 0` summed over exactly the period's days in
`fct_ad_exposure`, for one marketplace and platform (design §4.5), under the figure's own media
filters (`RosterRequest.filters`). It is planned through the
same planner every figure uses and bound through the publication selector, which binds every
day of the period or refuses: a period with any day lacking a spend publication gives no
roster, never a partial one. A withheld or truncated spend read is refused for the same reason.

A campaign key is a protected canonical identifier that never reaches public text, so a
campaign roster names each entry by its governed public label (`names`, the same resolver an
answer's rows are labelled with). A runtime without one, or a campaign the resolver can't name,
refuses the campaign roster rather than naming a campaign by its key or a placeholder.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from aws_lambda_powertools import Logger

from adapters.outbound.telemetry.reporting import LogSeverity, OperationLog, report_operation
from domain.measurement.figures import measure
from domain.measurement.planning import FactShape, PlanRequest, plan
from domain.measurement.rosters import (
    ROSTER_REFUSED,
    Roster,
    RosterEntry,
    RosterKind,
    roster_column,
)
from domain.operations.bake_refusals import IsolatedRefusal, IsolatedRefusalReason, RefusalScope
from domain.semantics.identity import AdProductKey
from domain.shared_kernel.errors import DomainError, ValidationError
from ports.outbound.publication_selection import (
    PublicationSelectionPort,
    PublicationSelectionRequest,
)
from ports.outbound.query import QueryPort, QueryRequest, RowCeiling, row_ceiling

if TYPE_CHECKING:  # pragma: no cover
    from datetime import datetime

    from domain.catalog.capability import Catalog
    from domain.data_contracts.contract import DataContract
    from domain.semantics.media_taxonomy import MediaTaxonomy
    from domain.shared_kernel.values import DateRange
    from ports.outbound.rosters import RosterRequest

__all__ = [
    "SPEND_FACT",
    "SPEND_METRIC",
    "BakePinnedNames",
    "PublicNames",
    "SpendRosterReader",
    "ad_product_labels",
]

SPEND_METRIC = "ad_spend"
SPEND_FACT = "fct_ad_exposure"
_MARKETPLACE = "marketplace_key"
_PLATFORM = "platform_key"


class PublicNames(Protocol):
    """Governed public labels for dimension values (`DimensionLabelResolver` is one)."""

    def public_names(
        self, column: str, values: tuple[str, ...], window: DateRange
    ) -> Mapping[str, str]:
        """Return each value's governed public name, leaving out (never placeholding) the rest."""
        ...


_PROBE_LOG = Logger(service="adlytics-preset-artifact-baker")
"""A suppressed probe refusal keeps its code here, so a later manifest failure has a cause."""

_PIN_PROBE_KEY = "campaign:pin-probe-0001"
"""A fictional key a bake's empty-answer pass names, so the label read is pinned; never shown."""


@dataclass(frozen=True, slots=True)
class BakePinnedNames:
    """Names for a Baker roster that read the label scopes even when no key has spend.

    A GRAPH bake's pin and capture passes answer every query empty, so a campaign roster has
    no keys there and a plain resolver reads no CURRENT label scope. The task's real render then
    names real keys and its label read is unpinned. Probing one fictional key on an empty call
    reads the same scopes (they depend on the column, not the keys) and returns nothing.
    """

    names: PublicNames

    def public_names(
        self, column: str, values: tuple[str, ...], window: DateRange
    ) -> Mapping[str, str]:
        """Return the governed names; on an empty call, read the scopes and return none."""
        if not values:
            # The probe only pins: a designed refusal never fails the pin pass (DB21 keeps
            # a dependency failure re-raising).
            try:
                self.names.public_names(column, (_PIN_PROBE_KEY,), window)
            except (DomainError, ValidationError) as refusal:
                report_operation(
                    OperationLog(
                        operation="graph_bake",
                        component="bake_pin_probe",
                        phase="source_pin",
                        outcome="probe_refused",
                        severity=LogSeverity.WARNING,
                        details={"probe": "campaign_names", "error_code": str(refusal.code)},
                    ),
                    _PROBE_LOG,
                )
            return {}
        return self.names.public_names(column, values, window)


@dataclass(frozen=True, slots=True)
class SpendRosterReader:
    """Plan, bind and read one period's spend per key; keep the keys above zero."""

    catalog: Catalog
    contracts: tuple[DataContract, ...]
    publications: PublicationSelectionPort
    queries: QueryPort
    labels: Mapping[str, str]
    """Governed names by key, in taxonomy order (ad products); a key without one shows itself."""
    clock: Callable[[], datetime]
    names: PublicNames | None = None
    """Campaign names by key; without it a campaign roster is refused, never named by key."""

    def roster(self, request: RosterRequest) -> Roster:
        """Return the keys with spend over every day of the period, in taxonomy order."""
        column = roster_column(request.kind)
        label = request.period.start.isoformat()
        try:
            logical = plan(
                self.catalog,
                PlanRequest(
                    SPEND_METRIC,
                    (request.kind.value,),
                    request.period,
                    {
                        _MARKETPLACE: request.marketplace_key,
                        _PLATFORM: request.platform_key,
                        **dict(request.filters),
                    },
                ),
                FactShape.for_fact(SPEND_FACT, self.contracts),
            )
            selection = self.publications.select(
                PublicationSelectionRequest((logical,), self.contracts, self.clock())
            )
        except DomainError as error:
            raise DomainError(
                ROSTER_REFUSED,
                f"Which {_noun(request.kind)} had ad spend from {label} to "
                f"{request.period.end.isoformat()} can't be read: {error.description}",
                context={"roster": request.kind.value, "reason": str(error.code)},
            ) from error
        result = self.queries.run(
            QueryRequest(
                plan=selection.plans[0],
                max_rows=row_ceiling(request.ceiling),
                ceiling=request.ceiling,
            )
        )
        if result.truncated and request.ceiling is not RowCeiling.NARRATION:
            # ADR-142: a bake's spend roster cut at the bake width refuses the page it feeds;
            # chat's read keeps its ROSTER_REFUSED answer below.
            raise IsolatedRefusal(
                IsolatedRefusalReason.ROWS_OVER_CEILING,
                RefusalScope.PAGE,
                max_rows=row_ceiling(request.ceiling),
            )
        if (
            result.truncated
            or result.suppression.rows_suppressed
            or not selection.coverage.covers_all((request.period,))
        ):
            raise DomainError(
                ROSTER_REFUSED,
                f"Which {_noun(request.kind)} had ad spend from {label} to "
                f"{request.period.end.isoformat()} can't be read in full, so no breakdown "
                "against it is answered.",
                context={"roster": request.kind.value},
            )
        spent = {
            dict(figure.dimensions)[column]
            for figure in measure(logical, result.rows).figures
            if Decimal(figure.value) > 0
        }
        if request.kind is RosterKind.CAMPAIGN:
            entries = self._campaigns(request, sorted(spent))
        else:
            ordered = (
                *(key for key in self.labels if key in spent),
                *sorted(key for key in spent if key not in self.labels),
            )
            entries = tuple(RosterEntry(key, self.labels.get(key, key)) for key in ordered)
        return Roster(
            kind=request.kind,
            period=request.period,
            marketplace_key=request.marketplace_key,
            platform_key=request.platform_key,
            entries=entries,
            filters=request.filters,
        )

    def _campaigns(self, request: RosterRequest, keys: list[str]) -> tuple[RosterEntry, ...]:
        """Name each campaign by its governed public label, ordered by label then key."""
        if self.names is None:
            raise DomainError(
                ROSTER_REFUSED,
                "Campaign names can't be read in this runtime, so no per-campaign breakdown "
                "is answered.",
                context={"roster": request.kind.value},
            )
        named = self.names.public_names(roster_column(request.kind), tuple(keys), request.period)
        unnamed = [key for key in keys if key not in named]
        if unnamed:
            # Only governed names count: a placeholder an answer's rows would carry ("Unnamed
            # campaign 1") is numbered per answer, so the roster's could name another campaign.
            raise DomainError(
                ROSTER_REFUSED,
                f"{len(unnamed)} campaign(s) with ad spend have no public name, so no "
                "per-campaign breakdown is answered.",
                context={"roster": request.kind.value},
            )
        ordered = sorted(keys, key=lambda item: (named[item], item))
        return tuple(RosterEntry(key, named[key]) for key in ordered)


def ad_product_labels(taxonomy: MediaTaxonomy) -> dict[str, str]:
    """Return every governed ad product's name by key, in taxonomy order."""
    return {
        term.key.value: term.name for term in taxonomy.terms if isinstance(term.key, AdProductKey)
    }


def _noun(kind: RosterKind) -> str:
    return "ad products" if kind is RosterKind.AD_PRODUCT else "campaigns"
