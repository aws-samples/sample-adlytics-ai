# SPDX-License-Identifier: MIT-0
"""`ForecastInputPort` on the Processing job's local input mounts (design §4e validators).

Adapted from the source engine's Parquet input. SageMaker copies the manifest object to
`<input>/manifest/` and the shard's `UNLOAD` objects (no suffix) to `<input>/target/` and
`<input>/traffic/`. Callers may rely on:

- the manifest validates as `InputManifestV1` or the read fails `INPUT_MANIFEST_INVALID`;
- every file's physical Arrow schema equals the one its kind's boundary model maps to, field for
  field and type for type, or the read fails `INPUT_SCHEMA_MISMATCH` (so a traffic file can't pass
  as a target file, and a renamed column can't pass at all);
- the shard's distinct canonical ASINs match its `ShardV1` count, first and last, and no traffic
  ASIN lies outside them, or the read fails `INPUT_SHARD_MISMATCH`.
"""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel, ValidationError

from domain.forecasting.boundary import (
    InputManifestV1,
    ShardV1,
    WeeklyTargetAggregateRowV1,
    WeeklyTrafficAggregateRowV1,
)
from ports.outbound.forecast_input import ForecastInputError, ShardInput

_ARROW: dict[type[BaseModel], dict[str, Any]] = {
    WeeklyTargetAggregateRowV1: {
        "schema_version": pa.int8(),
        "canonical_asin": pa.string(),
        "week_start": pa.date32(),
        "target": pa.string(),
        "value_sum": pa.decimal128(38, 4),
        "days_with_row": pa.int8(),
        "days_with_null": pa.int8(),
        "min_currency": pa.string(),
        "max_currency": pa.string(),
    },
    WeeklyTrafficAggregateRowV1: {
        "schema_version": pa.int8(),
        "canonical_asin": pa.string(),
        "week_start": pa.date32(),
        "glance_views_sum": pa.int64(),
        "days_with_traffic_row": pa.int8(),
    },
}


def arrow_schema(model: type[BaseModel]) -> Any:
    """Return the Arrow schema an `UNLOAD` file of `model`'s kind must have, in field order."""
    types = _ARROW[model]
    if list(types) != list(model.model_fields):
        raise RuntimeError(f"{model.__name__}: the Arrow mapping differs from the model's fields")
    return pa.schema([pa.field(name, kind) for name, kind in types.items()])


def _files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*") if path.is_file())


class ParquetForecastInput:
    """Reads one shard's manifest and weekly aggregates from the job's input directory."""

    def __init__(self, input_dir: Path) -> None:
        self._input = input_dir

    def read_manifest(self) -> InputManifestV1:
        """Validate the one manifest object SageMaker copied in."""
        files = _files(self._input / "manifest")
        if len(files) != 1:
            raise ForecastInputError(
                "INPUT_MANIFEST_INVALID", f"expected 1 manifest, found {len(files)}"
            )
        try:
            return InputManifestV1.model_validate_json(files[0].read_bytes())
        except ValidationError as error:
            raise ForecastInputError(
                "INPUT_MANIFEST_INVALID", f"{error.error_count()} errors"
            ) from error

    def read_shard(self, shard: int) -> ShardInput:
        """Return the shard's validated rows (see the module docstring for the refusals)."""
        manifest = self.read_manifest()
        planned = next((item for item in manifest.shards if item.index == shard), None)
        if planned is None:
            raise ForecastInputError("INPUT_SHARD_MISMATCH", f"the manifest has no shard {shard}")
        targets = self._rows("target", WeeklyTargetAggregateRowV1)
        traffic = self._rows("traffic", WeeklyTrafficAggregateRowV1)
        _check_shard(
            planned,
            {row.canonical_asin for row in targets},
            {row.canonical_asin for row in traffic},
        )
        return ShardInput(manifest=manifest, targets=targets, traffic=traffic)

    def _rows[M: BaseModel](self, kind: str, model: type[M]) -> tuple[M, ...]:
        expected = arrow_schema(model)
        files = _files(self._input / kind)
        if not files:
            raise ForecastInputError("INPUT_SHARD_MISMATCH", f"no {kind} objects for the shard")
        rows: list[M] = []
        for path in files:
            try:
                part = pq.ParquetFile(path)
            except (OSError, pa.ArrowException) as error:
                raise ForecastInputError("INPUT_SCHEMA_MISMATCH", f"{kind}: not Parquet") from error
            if not part.schema_arrow.equals(expected, check_metadata=False):
                raise ForecastInputError(
                    "INPUT_SCHEMA_MISMATCH",
                    f"{kind}: columns {part.schema_arrow.names} differ from {expected.names}",
                )
            try:
                rows.extend(model.model_validate(row) for row in part.read().to_pylist())
            except ValidationError as error:
                raise ForecastInputError(
                    "INPUT_SCHEMA_MISMATCH", f"{kind}: {error.error_count()} invalid rows"
                ) from error
        return tuple(rows)


def _check_shard(planned: ShardV1, target_asins: set[str], traffic_asins: set[str]) -> None:
    ordered = sorted(target_asins)
    observed = (len(ordered), ordered[0], ordered[-1]) if ordered else (0, None, None)
    declared = (planned.asin_count, planned.first_canonical_asin, planned.last_canonical_asin)
    if observed != declared:
        raise ForecastInputError(
            "INPUT_SHARD_MISMATCH",
            f"shard {planned.index}: {observed[0]} ASINs read, {planned.asin_count} declared",
        )
    if not traffic_asins <= target_asins:
        raise ForecastInputError(
            "INPUT_SHARD_MISMATCH", f"shard {planned.index}: traffic names ASINs outside the shard"
        )
