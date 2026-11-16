# SPDX-License-Identifier: MIT-0
"""Print and check the digest of a JumpStart Chronos-2 artifact prefix (design §4b, R2-11, D7).

`make demand-forecast-artifact-digest PROFILE= ENV= REGION=` re-reads the Region's prefix from
`contracts/forecasting/model-artifacts.json` and reports whether its digest still equals the
committed one; `URI=` reads another prefix, for reviewing a new Region or artifact version before
its entry is committed. Read-only (`ListObjectsV2` every page, `GetObject` streamed through
SHA-256); writes no file. Zero-byte keys ending in `/` are folder markers, which a mounted copy has
no file for, so they are skipped. The digest algorithm is
`domain.forecasting.artifact_roster.roster_digest`, the same the job applies to its mounted copy.
Stdout is the interface: a command-line tool run by a make target.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from domain.forecasting.artifact_roster import RosterEntry, roster_digest

if TYPE_CHECKING:
    from types_boto3_s3.client import S3Client
_CHUNK = 1024 * 1024
CONTRACT = Path(__file__).resolve().parents[4] / "contracts/forecasting/model-artifacts.json"


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


@dataclass(frozen=True)
class PinnedArtifact:
    """One Region's committed prefix, roster digest and object count."""

    uri: str
    digest: str
    objects: int


def committed_entry(region: str, contract: Path = CONTRACT) -> PinnedArtifact | None:
    """Return the contract's entry for `region`, or None when it has none."""
    regions: dict[str, dict[str, Any]] = json.loads(contract.read_text())["regions"]
    entry = regions.get(region)
    if entry is None:
        return None
    return PinnedArtifact(str(entry["uri"]), str(entry["digest"]), int(entry["objects"]))


def verdict(pin: PinnedArtifact | None, read: PinnedArtifact) -> tuple[int, str]:
    """Return the exit code and line for a prefix read against its pin (1 when it moved)."""
    if pin is None or pin.uri != read.uri:
        return 0, f"committed: no {CONTRACT.name} entry for this Region and prefix"
    if pin == read:
        return 0, f"committed: matches {CONTRACT.name}"
    return 1, f"committed: DIFFERS from {CONTRACT.name}"


def main(argv: list[str]) -> int:
    """Print the artifact digest and object count; exit 1 when it differs from the committed pin."""
    parser = argparse.ArgumentParser(prog="demand-forecast-artifact-digest")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--uri", default=None)
    args = parser.parse_args(argv)
    pin = committed_entry(args.region)
    uri = args.uri or (pin.uri if pin is not None else None)
    if not uri:
        print(
            f"refused: {CONTRACT.name} has no entry for {args.region}; pass URI=", file=sys.stderr
        )
        return 2
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client: S3Client = session.client("s3")
    entries = artifact_entries(client, uri)
    read = PinnedArtifact(uri, roster_digest(entries), len(entries))
    print(f"objects: {read.objects}")
    print(f"digest: {read.digest}")
    code, line = verdict(pin, read)
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
