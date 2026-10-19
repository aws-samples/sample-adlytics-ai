# SPDX-License-Identifier: MIT-0
"""Governed display labels for the dimension values a measurement answer releases.

The conversational path renders answer rows as public text, so every canonical dimension
value in a released row should read as its governed display name: `amazon_dsp` as
"Amazon DSP", `amazon_us` as "United States", and a campaign key as the campaign's name.
This resolver asks the entity catalog's public-label projection for exactly the distinct
values one answer will show and returns them as the domain's `DimensionDisplayLabels`.

**Never a refusal.** A value the projection cannot label passes through unchanged when it is
already public vocabulary (the media taxonomy and marketplace keys are declared, readable
identifiers), and becomes a deterministic placeholder when it is a protected identifier that
must not reach public text (a campaign key). A label longer than the label bound also becomes a
placeholder, because the bounded public-text validator would otherwise fail the turn after the
data was fetched (the failure ADR-066 diagnosed). A label's wording is never inspected: public
text renders inertly in the browser (ADR-082).
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from domain.conversation.contract_common import require_text
from domain.measurement.answers import DimensionDisplayLabel, DimensionDisplayLabels
from domain.semantics.analytical_resolution import (
    AuthorizedAnalyticalScope,
    EntityContext,
)
from domain.semantics.audience_segments import (
    SEGMENT_FACT_NAMESPACES,
    SEGMENT_ID_COLUMN,
    SEGMENT_NAMESPACE_COLUMN,
    SegmentLabel,
    SegmentNamespace,
    unnamed_segment_label,
)
from domain.shared_kernel.values import DateRange
from ports.outbound.entity_catalog import (
    MAX_PUBLIC_LABEL_ITEMS,
    AnalyticalEntityCatalogPort,
    PublicEntityLabelItem,
    PublicEntityLabelOutcome,
    PublicEntityLabelRequest,
    PublicEntityLabelResolution,
)
from ports.outbound.segment_labels import SegmentLabelPort, SegmentLabelRequest

_LABELLABLE_COLUMNS = frozenset(
    {
        "campaign_key",
        "marketplace_key",
        "channel_key",
        "platform_key",
        "ecosystem_key",
        "channel_group_key",
        "ad_product_key",
    }
)
"""Grain columns the public-label projection can name.

`amazon_asin` is deliberately absent: its labels are per-marketplace product titles, so the
projection requires a marketplace context this resolver does not have. An ASIN is itself a
readable public product identifier, so it passes through unchanged until title labelling
ships with its context plumbing.
"""

_PROTECTED_COLUMNS = frozenset({"campaign_key"})
"""Columns whose values are protected canonical identifiers that must never reach public
text. An unresolved value in one of these becomes a placeholder rather than passing through,
because the public-text validator rightly refuses the canonical key."""

_MAX_LABEL_LENGTH = 120
"""Longest display label a claim sentence stays readable with. A longer or unsafe governed
name falls back to the placeholder rather than truncated text, so a released label is always
exactly the governed name or visibly no name at all."""


class DimensionLabelResolver:
    """Resolve governed public display labels for one answer's dimension values."""

    def __init__(
        self,
        catalog: AnalyticalEntityCatalogPort,
        scope: AuthorizedAnalyticalScope,
        segments: SegmentLabelPort | None = None,
    ) -> None:
        self._catalog = catalog
        self._scope = scope
        self._segments = segments

    def resolve(  # noqa: PLR0913 -- the fact names a segment row's namespace (ADR-103)
        self,
        rows: tuple[dict[str, str | int | Decimal | None], ...],
        columns: tuple[str, ...],
        window: DateRange,
        *,
        fact: str | None = None,
        marketplace: str | None = None,
        namespace: str | None = None,
    ) -> DimensionDisplayLabels:
        """Label every distinct labellable dimension value the given rows carry.

        One projection call per column, deliberately: the public-label contract downgrades
        any duplicate resolved label in a batch to ambiguous, and a channel and its ad
        product legitimately share a display name ("Amazon DSP"), so mixing columns in one
        batch would erase correct labels. Within a single column a duplicate is a genuine
        ambiguity and the placeholder is the honest rendering.
        """
        entries: list[DimensionDisplayLabel] = []
        for column in sorted(set(columns) & _LABELLABLE_COLUMNS):
            values = sorted(
                {
                    cell.strip()
                    for row in rows
                    if isinstance(cell := row.get(column), str) and cell.strip()
                }
            )
            if not values:
                continue
            resolutions = self._resolutions(column, values, window)
            placeholders = 0
            for value, resolution in zip(values, resolutions, strict=True):
                label = self._safe_label(resolution)
                if label is None:
                    if column not in _PROTECTED_COLUMNS:
                        continue
                    placeholders += 1
                    word = column.removesuffix("_key").replace("_", " ")
                    label = f"Unnamed {word} {placeholders}"
                entries.append(DimensionDisplayLabel(column, value, label))
        if SEGMENT_ID_COLUMN in columns:
            entries.extend(
                self._segment_labels(
                    rows, window, fact=fact, marketplace=marketplace, namespace=namespace
                )
            )
        return DimensionDisplayLabels(tuple(entries))

    def _segment_labels(  # noqa: PLR0913 -- a segment is named in its namespace and marketplace
        self,
        rows: tuple[dict[str, str | int | Decimal | None], ...],
        window: DateRange,
        *,
        fact: str | None,
        marketplace: str | None,
        namespace: str | None = None,
    ) -> tuple[DimensionDisplayLabel, ...]:
        """Name each segment by its governed name and provider, in its namespace and marketplace.

        A row's `segment_namespace` names its namespace; a row without one takes its fact's only
        namespace (`SEGMENT_FACT_NAMESPACES`), or the answer's pinned `namespace` on a fact holding
        both (C2, whose profile path pins one). A segment of none stays its ID, never another
        namespace's name (ADR-103 point 6). A row's `marketplace_key` names its marketplace; a
        row without one takes the answer's marketplace, and with neither the segment stays its
        ID, because an ID is unique only within its marketplace. A segment with no governed name
        reads by its ID with "name not available yet", and its provider is never guessed.
        """
        default = None if fact is None else SEGMENT_FACT_NAMESPACES.get(fact)
        if default is None and namespace is not None:
            default = SegmentNamespace(namespace)
        wanted: dict[tuple[SegmentNamespace, str | None], set[str]] = {}
        for row in rows:
            value = row.get(SEGMENT_ID_COLUMN)
            raw = row.get(SEGMENT_NAMESPACE_COLUMN)
            namespace = SegmentNamespace(str(raw)) if raw is not None else default
            cell = row.get("marketplace_key")
            market = cell.strip() if isinstance(cell, str) and cell.strip() else marketplace
            if isinstance(value, str) and value.strip() and namespace is not None:
                wanted.setdefault((namespace, market), set()).add(value)
        if len(wanted) > 1:
            # One column can carry one label per value; two namespaces or marketplaces sharing an
            # ID would be labelled as one segment, so none is labelled.
            return ()
        found: list[DimensionDisplayLabel] = []
        for (namespace, market), values in wanted.items():
            ordered = tuple(sorted(values))
            labels: Mapping[str, SegmentLabel] = (
                {}
                if self._segments is None or market is None
                else self._segments.labels(
                    SegmentLabelRequest(namespace, market, ordered, window.end)
                )
            )
            found.extend(
                DimensionDisplayLabel(
                    SEGMENT_ID_COLUMN,
                    value,
                    labels[value].display if value in labels else unnamed_segment_label(value),
                )
                for value in ordered
            )
        return tuple(found)

    def public_names(
        self, column: str, values: tuple[str, ...], window: DateRange
    ) -> dict[str, str]:
        """Return each value's governed public label; one the projection can't name is left out.

        No placeholder: a caller that must name every value (a campaign roster) refuses on an
        omitted one, because a placeholder is numbered per answer and could name another value.
        """
        ordered = sorted({value.strip() for value in values if value.strip()})
        if column not in _LABELLABLE_COLUMNS or not ordered:
            return {}
        return {
            value: label
            for value, resolution in zip(
                ordered, self._resolutions(column, ordered, window), strict=True
            )
            if (label := self._safe_label(resolution)) is not None
        }

    def _resolutions(
        self,
        column: str,
        values: list[str],
        window: DateRange,
    ) -> tuple[PublicEntityLabelResolution, ...]:
        resolutions: list[PublicEntityLabelResolution] = []
        for start in range(0, len(values), MAX_PUBLIC_LABEL_ITEMS):
            chunk = values[start : start + MAX_PUBLIC_LABEL_ITEMS]
            result = self._catalog.public_labels(
                PublicEntityLabelRequest(
                    scope=self._scope,
                    items=tuple(PublicEntityLabelItem(((column, value),)) for value in chunk),
                    context=EntityContext(),
                    window=window,
                )
            )
            resolutions.extend(result.resolutions)
        return tuple(resolutions)

    @staticmethod
    def _safe_label(resolution: PublicEntityLabelResolution) -> str | None:
        """Accept only a resolved label that is bounded, nonblank public text."""
        if resolution.outcome is not PublicEntityLabelOutcome.RESOLVED:
            return None
        label = resolution.display_label
        if label is None:  # pragma: no cover - resolution DTO invariant
            return None
        try:
            require_text(label, "dimension display label", maximum=_MAX_LABEL_LENGTH)
        except ValueError:
            return None
        return label
