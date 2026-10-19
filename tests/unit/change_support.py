# SPDX-License-Identifier: MIT-0
"""The repository's governed change semantics, loaded once for tests (ADR-121)."""

from __future__ import annotations

import pathlib
from decimal import Decimal
from typing import cast

from adapters.outbound.metric_dictionary import load_catalog
from domain.catalog.capability import FavourableDirection
from domain.measurement.comparison import percentage_change
from domain.presentation.change import (
    CHANGE_KEY,
    PERCENTAGE_KEY,
    ChangeScale,
    ChangeSemantics,
    ComparedMetric,
)
from domain.presentation.compared_metrics import presentation_change_semantics

ROOT = pathlib.Path(__file__).resolve().parents[2]

SEMANTICS = presentation_change_semantics(load_catalog(ROOT))
"""The served roster's semantics from `contracts/shared/metric-dictionary.yaml`."""

FICTIONAL_SEMANTICS = ChangeSemantics(
    (
        *SEMANTICS.metrics,
        ("fictional_metric", ComparedMetric(FavourableDirection.NONE, ChangeScale.RELATIVE)),
    )
)
"""The roster plus a neutral relative `fictional_metric`, for focus fixtures that use it."""


def as_retained(payload: object) -> None:
    """Rewrite every comparison in an encoded body, in place, as a pre-block body carries it.

    A body baked before the change block (Campaigns format 6, Retail health format 4) carries
    the percentage and no `change` key.
    """
    pending: list[object] = [payload]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(cast("list[object]", node))
        elif isinstance(node, dict):
            item = cast("dict[str, object]", node)
            if CHANGE_KEY in item:
                del item[CHANGE_KEY]
                current = cast("dict[str, object]", item["current"])["value"]
                prior = cast("dict[str, object]", item["prior"])["value"]
                item[PERCENTAGE_KEY] = (
                    None
                    if current is None or prior is None
                    else percentage_change(Decimal(str(current)), Decimal(str(prior)))
                )
            pending.extend(item.values())
