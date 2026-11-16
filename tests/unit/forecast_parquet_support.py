# SPDX-License-Identifier: MIT-0
"""Write a small Parquet file for forecast tests; pyarrow ships no type information."""
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq


def parquet_bytes(names: list[str], rows: int, row_group_size: int = 500) -> bytes:
    """Return a Parquet file of `rows` rows: a string and an int8 column named by `names`."""
    table = pa.table(
        {
            names[0]: pa.array([f"amazon_asin:b{n:09d}" for n in range(rows)]),
            names[1]: pa.array([7] * rows, type=pa.int8()),
        }
    )
    buffer = io.BytesIO()
    pq.write_table(table, buffer, row_group_size=row_group_size)
    return buffer.getvalue()
