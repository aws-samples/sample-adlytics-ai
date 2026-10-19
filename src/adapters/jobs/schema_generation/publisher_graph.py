# SPDX-License-Identifier: MIT-0
"""Load the declarative one-owner graph for generated canonical datasets.

The YAML uses a list rather than a mapping so duplicate dataset entries survive parsing and reach
the domain validator. A YAML mapping would overwrite one owner with another before the graph could
prove that the declaration was ambiguous.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from domain.semantics.publication import (
    CanonicalDatasetKey,
    CanonicalPublisherBinding,
    CanonicalPublisherKey,
    PublicationGraph,
)

__all__ = ["PublisherGraphLoadError", "load_publication_graph"]

GRAPH_PATH = Path("contracts/shared/canonical-publishers.yaml")


class PublisherGraphLoadError(ValueError):
    """The publisher declaration cannot construct the complete canonical graph."""


type Mapping = dict[str, object]


def load_publication_graph(
    root: Path,
    expected_datasets: frozenset[CanonicalDatasetKey],
) -> PublicationGraph:
    """Parse the strict publisher declaration and delegate graph completeness to the domain."""
    path = root / GRAPH_PATH
    parsed = cast("object", yaml.safe_load(path.read_text()))
    if not isinstance(parsed, dict):
        raise PublisherGraphLoadError(f"{path}: expected a mapping at the top level.")
    document = cast("Mapping", parsed)
    unknown = set(document) - {"version", "publishers"}
    if unknown:
        raise PublisherGraphLoadError(f"{path}: unknown keys: {', '.join(sorted(unknown))}.")
    version = document.get("version")
    if isinstance(version, bool) or version != 1:
        raise PublisherGraphLoadError(f"{path}: version must be integer 1.")
    raw_publishers = document.get("publishers")
    if not isinstance(raw_publishers, list):
        raise PublisherGraphLoadError(f"{path}: publishers must be a list.")

    bindings: list[CanonicalPublisherBinding] = []
    for index, item in enumerate(cast("list[object]", raw_publishers)):
        where = f"{path}: publishers[{index}]"
        if not isinstance(item, dict):
            raise PublisherGraphLoadError(f"{where} must be a mapping.")
        raw = cast("Mapping", item)
        unknown_entry = set(raw) - {"dataset", "publisher"}
        if unknown_entry:
            raise PublisherGraphLoadError(
                f"{where} has unknown keys: {', '.join(sorted(unknown_entry))}."
            )
        dataset = _text(raw.get("dataset"), key="dataset", where=where)
        publisher = _text(raw.get("publisher"), key="publisher", where=where)
        bindings.append(
            CanonicalPublisherBinding(
                dataset=CanonicalDatasetKey(dataset),
                publisher=CanonicalPublisherKey(publisher),
            )
        )
    return PublicationGraph(expected_datasets=expected_datasets, bindings=tuple(bindings))


def _text(value: object, *, key: str, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublisherGraphLoadError(f"{where}.{key} must be a non-empty string.")
    return value
