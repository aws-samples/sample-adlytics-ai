# SPDX-License-Identifier: MIT-0
"""Print the digest of the configured JumpStart Chronos-2 artifact prefix (design §4b, R2-11).

`make demand-forecast-artifact-digest PROFILE= ENV=` runs this before the first synth; the
operator puts the printed value in the deployment's `.env` as
`ADLYTICS_DEMAND_FORECAST_MODEL_ARTIFACT_URI`'s digest. Read-only (`ListObjectsV2` every page,
`GetObject` streamed through SHA-256); writes no file. Zero-byte keys ending in `/` are folder
markers, which a mounted copy has no file for, so they are skipped. The digest algorithm is
`domain.forecasting.artifact_roster.roster_digest`, the same the job applies to its mounted copy.
Stdout is the interface: a command-line tool run by a make target.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from typing import TYPE_CHECKING

import boto3

from domain.forecasting.artifact_roster import RosterEntry, roster_digest

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client

_CHUNK = 1024 * 1024


def _split(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError("the artifact URI must be an s3:// prefix")
    bucket, _, prefix = uri.removeprefix("s3://").partition("/")
    if not bucket or not prefix:
        raise ValueError("the artifact URI must name a bucket and a prefix")
    return bucket, prefix if prefix.endswith("/") else f"{prefix}/"


def _sha256(client: S3Client, bucket: str, key: str) -> str:
    digest = hashlib.sha256()
    body = client.get_object(Bucket=bucket, Key=key)["Body"]
    for chunk in body.iter_chunks(chunk_size=_CHUNK):
        digest.update(chunk)
    return digest.hexdigest()


def artifact_entries(client: S3Client, uri: str) -> tuple[RosterEntry, ...]:
    """Every object under the prefix as a roster entry (relative path, size, SHA-256)."""
    bucket, prefix = _split(uri)
    entries: list[RosterEntry] = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            size = item.get("Size", 0)
            if key.endswith("/") and size == 0:
                continue
            entries.append(
                RosterEntry(key.removeprefix(prefix), size, _sha256(client, bucket, key))
            )
    return tuple(entries)


def main(argv: list[str]) -> int:
    """Print the artifact digest and object count."""
    parser = argparse.ArgumentParser(prog="demand-forecast-artifact-digest")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", default=None)
    parser.add_argument("--uri", required=True)
    args = parser.parse_args(argv)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client: S3Client = session.client("s3")
    entries = artifact_entries(client, args.uri)
    print(f"objects: {len(entries)}")
    print(f"digest: {roster_digest(entries)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
