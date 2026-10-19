# SPDX-License-Identifier: MIT-0
"""Governed report-lens vocabulary shared by resolution, loading, and answers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

__all__ = [
    "LENS_DEFAULTS",
    "REPORT_LENSES",
    "ReportLens",
    "ReportLensDefinition",
    "defaults_for",
    "report_lenses_for",
]


@dataclass(frozen=True, slots=True)
class ReportLensDefinition:
    """Allowed values, default, and source datasets for one report lens."""

    name: str
    values: tuple[str, ...]
    default: str
    source_datasets: frozenset[str]

    def __post_init__(self) -> None:
        if not self.name or not self.values or self.default not in self.values:
            raise ValueError("a report lens requires a name, values, and one allowed default.")
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"{self.name}: report-lens values must be unique.")
        if not self.source_datasets:
            raise ValueError(f"{self.name}: a report lens requires an applicable source dataset.")


@dataclass(frozen=True, slots=True)
class ReportLens:
    """One validated report-lens selection in the shared governed vocabulary."""

    name: str
    value: str

    def __post_init__(self) -> None:
        definition = REPORT_LENSES.get(self.name)
        if definition is None or self.value not in definition.values:
            raise ValueError(f"unknown report-lens selection {self.name}={self.value}.")


REPORT_LENSES: Mapping[str, ReportLensDefinition] = MappingProxyType(
    {
        "distributor_view": ReportLensDefinition(
            name="distributor_view",
            values=("MANUFACTURING", "SOURCING"),
            default="MANUFACTURING",
            source_datasets=frozenset({"GET_VENDOR_SALES_REPORT"}),
        ),
        "selling_program": ReportLensDefinition(
            name="selling_program",
            values=("RETAIL", "BUSINESS", "FRESH"),
            default="RETAIL",
            source_datasets=frozenset({"GET_VENDOR_SALES_REPORT"}),
        ),
        "report_period": ReportLensDefinition(
            name="report_period",
            values=("DAY",),
            default="DAY",
            source_datasets=frozenset({"GET_VENDOR_SALES_REPORT", "GET_VENDOR_TRAFFIC_REPORT"}),
        ),
    }
)
"""The one governed report-lens vocabulary.

Every entry selects a view of the same population. Population selectors such as marketplace and
channel must never be added. Dataset applicability lets resolution default exactly the lenses a
metric's declared sources use rather than attaching Vendor options to unrelated AMC metrics.
"""

LENS_DEFAULTS: Mapping[str, str] = MappingProxyType(
    {name: definition.default for name, definition in REPORT_LENSES.items()}
)
"""Read-only report-lens defaults, derived from the shared vocabulary."""


def defaults_for(unaccounted: tuple[str, ...]) -> dict[str, str]:
    """Return governed defaults only for report lenses the caller left open."""
    return {column: LENS_DEFAULTS[column] for column in unaccounted if column in LENS_DEFAULTS}


def report_lenses_for(
    sources: tuple[tuple[str, str], ...],
) -> tuple[ReportLensDefinition, ...]:
    """Return report lenses applicable to the metric's declared source datasets."""
    datasets = {dataset for _system, dataset in sources}
    return tuple(
        definition
        for definition in REPORT_LENSES.values()
        if definition.source_datasets.intersection(datasets)
    )
