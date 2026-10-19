# SPDX-License-Identifier: MIT-0
"""Load the governed audience-segment provider classification (ADR-103 point 7).

`contracts/shared/audience-segment-providers.yaml` states, per namespace, a provider rule (or
none, for a namespace that mixes providers) and lists classified segments by exact name. The
load applies it to `dim_audience_segment.provider`. A malformed file fails closed: a provider
must never be guessed.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from domain.semantics.audience_segments import (
    NamespaceRule,
    ProviderEntry,
    SegmentNamespace,
    SegmentProvider,
    SegmentProviderClassification,
)

__all__ = ["SEGMENT_PROVIDERS_PATH", "load_segment_providers", "parse_segment_providers"]

SEGMENT_PROVIDERS_PATH = Path("contracts/shared/audience-segment-providers.yaml")

_RULE_KEYS = frozenset({"namespace", "provider", "evidence"})
_ENTRY_KEYS = frozenset({"namespace", "segment_name", "category", "provider", "evidence"})


def _mapping(value: object, where: str, keys: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{SEGMENT_PROVIDERS_PATH}: {where} must be a mapping.")
    raw = cast("dict[str, object]", value)
    if frozenset(raw) != keys:
        raise ValueError(f"{SEGMENT_PROVIDERS_PATH}: {where} takes exactly {sorted(keys)}.")
    return raw


def _text(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{SEGMENT_PROVIDERS_PATH}: {where} must be non-empty text.")
    return value


def _items(document: dict[str, object], key: str) -> list[object]:
    value = document.get(key)
    if not isinstance(value, list):
        raise ValueError(f"{SEGMENT_PROVIDERS_PATH}: {key} must be a list.")
    return cast("list[object]", value)


def parse_segment_providers(document: object) -> SegmentProviderClassification:
    """Narrow a parsed classification document; raise `ValueError` naming the first defect."""
    root = _mapping(document, "the document", frozenset({"version", "namespaces", "segments"}))
    version = root["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"{SEGMENT_PROVIDERS_PATH}: version must be an integer.")
    rules: list[NamespaceRule] = []
    for index, item in enumerate(_items(root, "namespaces")):
        raw = _mapping(item, f"namespaces[{index}]", _RULE_KEYS)
        provider = raw["provider"]
        rules.append(
            NamespaceRule(
                namespace=SegmentNamespace(_text(raw["namespace"], f"namespaces[{index}]")),
                provider=None
                if provider is None
                else SegmentProvider(_text(provider, f"namespaces[{index}].provider")),
                evidence=_text(raw["evidence"], f"namespaces[{index}].evidence"),
            )
        )
    entries = tuple(
        ProviderEntry(
            namespace=SegmentNamespace(_text(raw["namespace"], f"segments[{index}].namespace")),
            segment_name=_text(raw["segment_name"], f"segments[{index}].segment_name"),
            provider=SegmentProvider(_text(raw["provider"], f"segments[{index}].provider")),
            category=_text(raw["category"], f"segments[{index}].category"),
            evidence=_text(raw["evidence"], f"segments[{index}].evidence"),
        )
        for index, item in enumerate(_items(root, "segments"))
        for raw in (_mapping(item, f"segments[{index}]", _ENTRY_KEYS),)
    )
    return SegmentProviderClassification(version=version, rules=tuple(rules), entries=entries)


def load_segment_providers(root: Path) -> SegmentProviderClassification:
    """Read and narrow the classification under `root`; a missing file raises."""
    path = root / SEGMENT_PROVIDERS_PATH
    return parse_segment_providers(cast("object", yaml.safe_load(path.read_text(encoding="utf-8"))))
