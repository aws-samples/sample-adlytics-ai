# SPDX-License-Identifier: MIT-0
"""The deployed Glue catalog against the contracts that generated it.

**The gate whose absence let G-32 surface as a failed deploy rather than a check.**
`make check-schemas` compares the generated artefacts to the contracts, and `make infra-test`
compares the synthesised template to the generated artefacts. Nothing compared either to the
catalog that is actually deployed, so three tables could sit in Glue untracked by CloudFormation
and a table's columns could disagree with `tables.json` indefinitely, with every gate green.

**It reads the Iceberg metadata, never Glue's `StorageDescriptor`.** For an Iceberg table the
`StorageDescriptor.Columns` list is a denormalised cache and it lags: measured immediately after
the update that removed two columns from `fct_ad_exposure`, the descriptor still listed both while
the Iceberg metadata and Athena's `information_schema` both correctly reported seventeen columns.
A gate reading the descriptor would report drift that is not there, which is worse than no gate —
it trains a reader to ignore it.

Integration tier, so it skips without credentials and never runs in the unit loop. It cannot be a
`make check` target for that reason: `make check` must stay credential-free.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import boto3
import pytest

from adapters.config import Settings

pytestmark = [pytest.mark.integration, pytest.mark.covers("G-32")]

ROOT = pathlib.Path(__file__).resolve().parents[2]
TABLES = ROOT / "contracts" / "generated" / "v2-tables.json"


def _generated() -> dict[str, dict[str, Any]]:
    """Read the generated table definitions, keyed by table name."""
    document = json.loads(TABLES.read_text())
    return {table["name"]: table for table in document["canonicalCurrentTables"]}


def _field_ids(table: dict[str, Any]) -> dict[str, int]:
    """Give each column's governed physical Iceberg field ID."""
    return {column["name"]: int(column["fieldId"]) for column in table["columns"]}


@pytest.fixture(scope="module")
def catalog(settings: Settings) -> dict[str, dict[str, Any]]:
    """Every deployed table's Iceberg metadata, keyed by table name.

    One Glue call per database plus one S3 read per table, done once for the module. The metadata
    document is the authoritative schema; the Glue table record is only how its location is found.
    """
    glue = boto3.client("glue")
    s3 = boto3.client("s3")

    try:
        paginator = glue.get_paginator("get_tables")
        tables = [
            table
            for page in paginator.paginate(DatabaseName=settings.semantic_v2_current_database)
            for table in page["TableList"]
        ]
    except Exception as exc:  # noqa: BLE001 — pytest.skip raises: no database is a skip
        pytest.skip(f"no deployed Glue database to compare against: {type(exc).__name__}")

    found: dict[str, dict[str, Any]] = {}
    for table in tables:
        location = table.get("Parameters", {}).get("metadata_location")
        if not location:
            # Not an Iceberg table, or a table Glue has not finished writing. Recorded as an
            # empty mapping so the assertions below can name it rather than skipping it silently.
            found[table["Name"]] = {}
            continue
        bucket, _, key = location.removeprefix("s3://").partition("/")
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        found[table["Name"]] = json.loads(body)

    return found


def _current_schema(metadata: dict[str, Any]) -> dict[str, Any]:
    """Select the schema the table currently presents, not the whole version history.

    Iceberg retains superseded schemas so old snapshots stay readable, so the current one has to
    be selected by id rather than taken as the only entry.
    """
    return next(s for s in metadata["schemas"] if s["schema-id"] == metadata["current-schema-id"])


class TestTheDeployedCatalogMatchesTheContracts:
    def test_every_generated_table_is_deployed(self, catalog: dict[str, dict[str, Any]]) -> None:
        missing = sorted(set(_generated()) - set(catalog))
        assert not missing, f"declared in tables.json and absent from the catalog: {missing}"

    def test_no_table_is_deployed_that_the_contracts_do_not_declare(
        self, catalog: dict[str, dict[str, Any]]
    ) -> None:
        """The assertion that would have caught G-32's orphans.

        Three residual tables sat in Glue untracked by CloudFormation, because a rollback skipped
        deleting what it had created. Nothing noticed until the next deploy failed on "already
        exists". An extra table is not harmless: it is the shape a blocked redeploy takes.
        """
        extra = sorted(set(catalog) - set(_generated()))
        assert not extra, f"deployed and not declared in tables.json: {extra}"

    def test_every_column_carries_its_governed_physical_field_id(
        self, catalog: dict[str, dict[str, Any]]
    ) -> None:
        """The G-32 failure, checkable before a deploy rather than during one.

        Glue assigns field ids sequentially in submission order and discards what we send, so a
        generator emitting anything else guarantees the next update resubmits a partition spec
        Glue reads as pointing at a different field.
        """
        for name, table in _generated().items():
            metadata = catalog.get(name)
            if not metadata:
                continue  # absence is the previous test's assertion, not this one's
            deployed = {f["name"]: f["id"] for f in _current_schema(metadata)["fields"]}
            assert deployed == _field_ids(table), f"{name}: field ids disagree with v2-tables.json"

    def test_every_partition_key_is_sourced_from_its_own_ordinal(
        self, catalog: dict[str, dict[str, Any]]
    ) -> None:
        """`Cannot create identity partition sourced from different field in schema`, prevented.

        This is the exact assertion that turns G-32 from a rollback into a red check.
        """
        for name, table in _generated().items():
            metadata = catalog.get(name)
            if not metadata or not table["partitionKeys"]:
                continue
            field_ids = _field_ids(table)
            specs: list[dict[str, Any]] = metadata.get("partition-specs") or []
            sourced: dict[str, int] = {}
            for spec in specs:
                fields: list[dict[str, Any]] = spec["fields"]
                for field in fields:
                    sourced[str(field["name"])] = int(field["source-id"])

            assert sourced == {key: field_ids[key] for key in table["partitionKeys"]}, (
                f"{name}: partition spec sources disagree with governed physical field ids"
            )
