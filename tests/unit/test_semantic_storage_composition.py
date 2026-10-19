# SPDX-License-Identifier: MIT-0
"""Semantic storage composition for the S3-only ingestion path."""

from __future__ import annotations

from typing import Any, cast

import pytest

from adapters.outbound.s3.semantic_storage import S3CanonicalAdapter, S3SourceAlignedAdapter
from adapters.outbound.semantic_storage import s3_only_semantic_storage
from ports.outbound.canonical import CanonicalDatasetReadPort, CanonicalDatasetWritePort
from ports.outbound.source_aligned import SourceAlignedReadPort, SourceAlignedWritePort

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-075")]


def test_s3_only_storage_returns_existing_port_compatible_adapters() -> None:
    storage = s3_only_semantic_storage("semantic-bucket", cast("Any", object()))

    assert type(storage.source) is S3SourceAlignedAdapter
    assert isinstance(storage.source, SourceAlignedWritePort)
    assert isinstance(storage.source, SourceAlignedReadPort)
    assert type(storage.canonical) is S3CanonicalAdapter
    assert isinstance(storage.canonical, CanonicalDatasetWritePort)
    assert isinstance(storage.canonical, CanonicalDatasetReadPort)


def test_s3_only_storage_rejects_a_blank_bucket() -> None:
    with pytest.raises(ValueError, match="semantic storage bucket is required"):
        s3_only_semantic_storage("  ", cast("Any", object()))
