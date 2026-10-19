# SPDX-License-Identifier: MIT-0
"""Load strict Semantic V2 source packs without altering legacy data contracts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from domain.semantics.publication import CanonicalDatasetKey
from domain.semantics.source_pack import (
    CampaignIdentityField,
    IdentityField,
    MediaIdentityField,
    PackReadiness,
    SourcePack,
    SourcePackBlocker,
    SourcePackBlockerFamily,
    SourcePackControl,
    SourcePackControlRole,
    SourcePackRole,
    TimeField,
)
from domain.semantics.time import FactTimeClass, ObservationTimeBasis, TimeGrain

PACKS_PATH = Path("contracts/v2/source-packs.json")
SOURCE_PACK_REGISTRY_VERSION = 2


def load_source_packs(root: Path) -> tuple[SourcePack, ...]:
    """Load every V2 pack and reject duplicate contract identities."""
    raw = cast("object", json.loads((root / PACKS_PATH).read_text()))
    if not isinstance(raw, dict):
        raise ValueError("source packs require an object document.")
    document = cast("dict[str, object]", raw)
    if (
        set(document) != {"version", "sourcePacks"}
        or document["version"] != SOURCE_PACK_REGISTRY_VERSION
    ):
        raise ValueError(
            f"source packs require exactly version {SOURCE_PACK_REGISTRY_VERSION} and sourcePacks."
        )
    registry_version = cast("int", document["version"])
    entries = document["sourcePacks"]
    if not isinstance(entries, list):
        raise ValueError("sourcePacks must be a list.")
    packs = tuple(_pack(item, registry_version) for item in cast("list[object]", entries))
    ids = tuple(pack.contract_id for pack in packs)
    if len(set(ids)) != len(ids):
        raise ValueError("source packs repeat a contract id.")
    by_id = {pack.contract_id: pack for pack in packs}
    for pack in packs:
        control = pack.control
        if control is None:
            continue
        peer = by_id.get(control.peer_contract_id)
        if (
            peer is None
            or peer.control is None
            or peer.control.group != control.group
            or peer.control.peer_contract_id != pack.contract_id
            or peer.control.role is control.role
        ):
            raise ValueError(f"source-pack control group {control.group!r} is not reciprocal.")
    return packs


def _pack(value: object, registry_version: int) -> SourcePack:
    item = _object(value, "source pack")
    allowed = {
        "advertiser",
        "blocker",
        "campaign",
        "contractId",
        "dataset",
        "role",
        "profile",
        "marketplace",
        "media",
        "product",
        "time",
        "status",
        "control",
    }
    optional = {"advertiser", "blocker", "campaign", "control", "media"}
    if set(item) - allowed or allowed - optional - set(item):
        raise ValueError(f"source pack has unknown or missing keys: {sorted(set(item))}.")
    product = item.get("product")
    return SourcePack(
        contract_id=_text(item["contractId"]),
        registry_version=registry_version,
        dataset=CanonicalDatasetKey(_text(item["dataset"])),
        role=SourcePackRole(_text(item["role"])),
        profile=_text(item["profile"]),
        marketplace=_identity(item["marketplace"]),
        media=None if item.get("media") is None else _media(item["media"]),
        product=None if product is None else _identity(product),
        time=_time(item["time"]),
        readiness=PackReadiness(_text(item["status"])),
        blocker=None if item.get("blocker") is None else _blocker(item["blocker"]),
        campaign=None if item.get("campaign") is None else _campaign(item["campaign"]),
        advertiser=(None if item.get("advertiser") is None else _identity(item["advertiser"])),
        control=None if item.get("control") is None else _control(item["control"]),
    )


def _control(value: object) -> SourcePackControl:
    item = _object(value, "source-pack control")
    if set(item) != {"group", "role", "peerContractId"}:
        raise ValueError("source-pack control requires group, role, and peerContractId only.")
    return SourcePackControl(
        group=_text(item["group"]),
        role=SourcePackControlRole(_text(item["role"])),
        peer_contract_id=_text(item["peerContractId"]),
    )


def _blocker(value: object) -> SourcePackBlocker:
    item = _object(value, "source-pack blocker")
    if set(item) != {"family", "detail"}:
        raise ValueError("source-pack blocker requires family and detail only.")
    return SourcePackBlocker(
        family=SourcePackBlockerFamily(_text(item["family"])),
        detail=_text(item["detail"]),
    )


def _identity(value: object) -> IdentityField:
    item = _object(value, "identity field")
    if set(item) != {"namespace", "path"}:
        raise ValueError("identity field requires namespace and path only.")
    return IdentityField(_text(item["namespace"]), _text(item["path"]))


def _campaign(value: object) -> CampaignIdentityField:
    item = _object(value, "campaign identity field")
    if set(item) != {"namespace", "path", "identifierKind"}:
        raise ValueError(
            "campaign identity field requires namespace, path, and identifierKind only."
        )
    return CampaignIdentityField(
        _text(item["namespace"]),
        _text(item["path"]),
        _text(item["identifierKind"]),
    )


def _media(value: object) -> MediaIdentityField:
    item = _object(value, "media identity")
    if set(item) not in ({"namespace", "path"}, {"namespace", "literal"}):
        raise ValueError("media identity requires namespace and exactly one path or literal.")
    return MediaIdentityField(
        namespace=_text(item["namespace"]),
        source_path=None if "path" not in item else _text(item["path"]),
        literal_value=None if "literal" not in item else _text(item["literal"]),
    )


def _time(value: object) -> TimeField:
    item = _object(value, "time field")
    common = {"class", "observationBasis", "startPath", "endPath"}
    time_class = FactTimeClass(_text(item.get("class")))
    # A periodic pack declares the calendar periods it measures instead of one grain (ADR-100).
    expected = common | ({"periodGrains"} if time_class is FactTimeClass.PERIODIC else {"grain"})
    if set(item) != expected:
        raise ValueError("time field has an invalid shape.")
    start = item["startPath"]
    end = item["endPath"]
    if time_class is FactTimeClass.PERIODIC:
        declared = item["periodGrains"]
        if not isinstance(declared, list) or not declared:
            raise ValueError("periodic time field requires a nonempty periodGrains list.")
        grains = tuple(TimeGrain(_text(grain)) for grain in cast("list[object]", declared))
        grain = grains[0]
    else:
        grains = ()
        grain = TimeGrain(_text(item["grain"]))
    return TimeField(
        time_class,
        None if start is None else _text(start),
        None if end is None else _text(end),
        grain,
        ObservationTimeBasis(_text(item["observationBasis"])),
        grains,
    )


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object.")
    return cast("dict[str, object]", value)


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("source-pack value must be a non-empty string.")
    return value
