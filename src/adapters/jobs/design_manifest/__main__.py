# SPDX-License-Identifier: MIT-0
"""Generate and validate the committed Decision Workspace design manifest.

The YAML source names the complete review closure. This job records content digests in a
committed JSON projection and checks the page README, mockup metadata, gallery, and parent
implementation disposition against that one source of truth.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from typing import cast

from adapters.outbound import repository_yaml

MANIFEST_SOURCE_PATH = pathlib.Path("docs/ui/design-manifest.yaml")
MANIFEST_OUTPUT_PATH = pathlib.Path("docs/ui/generated/design-manifest.json")

_README_REVISION = re.compile(
    r"^\| \*\*Design revision\*\* \| `(?P<revision>[^`]+)` \|$",
    re.MULTILINE,
)
_MOCKUP_REVISION = re.compile(r'<meta name="design-revision" content="(?P<revision>[^"]+)">')
_TABLE_VALUE = re.compile(
    r"^\| \*\*(?P<key>[^*]+)\*\* \| (?P<value>.+) \|$",
    re.MULTILINE,
)
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


type Mapping = dict[str, object]


class ManifestError(ValueError):
    """Raised when the manifest source is malformed."""


def _mapping(value: object, where: str) -> Mapping:
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: expected a mapping.")
    return cast("Mapping", value)


def _sequence(value: object, where: str) -> list[object]:
    if not isinstance(value, list):
        raise ManifestError(f"{where}: expected a list.")
    return cast("list[object]", value)


def _string(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ManifestError(f"{where}: expected a non-empty string.")
    return value


def _optional_decision_date(value: object, where: str) -> str | None:
    if value is None:
        return None
    date = _string(value, where)
    if _ISO_DATE.fullmatch(date) is None:
        raise ManifestError(f"{where}: expected an ISO calendar date or null.")
    return date


def _exact(mapping: Mapping, expected: set[str], where: str) -> None:
    if set(mapping) != expected:
        missing = ", ".join(sorted(expected - set(mapping)))
        unexpected = ", ".join(sorted(set(mapping) - expected))
        detail_parts = (
            f"missing {missing}" if missing else "",
            f"unexpected {unexpected}" if unexpected else "",
        )
        details = ", ".join(item for item in detail_parts if item)
        raise ManifestError(f"{where}: invalid fields ({details}).")


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _document_value(text: str, key: str) -> str | None:
    for match in _TABLE_VALUE.finditer(text):
        if match.group("key") == key:
            return match.group("value")
    return None


def _page_projection(root: pathlib.Path, page: Mapping, revision: str) -> Mapping:
    _exact(page, {"id", "readme", "mockup", "members", "shared_owners"}, "pages[]")
    page_id = _string(page["id"], "pages[].id")
    readme = pathlib.Path(_string(page["readme"], f"pages[{page_id}].readme"))
    mockup = pathlib.Path(_string(page["mockup"], f"pages[{page_id}].mockup"))
    members = tuple(
        pathlib.Path(_string(item, f"pages[{page_id}].members[]"))
        for item in _sequence(page["members"], f"pages[{page_id}].members")
    )
    shared_owners = tuple(
        _string(item, f"pages[{page_id}].shared_owners[]")
        for item in _sequence(page["shared_owners"], f"pages[{page_id}].shared_owners")
    )
    if readme not in members or mockup not in members:
        raise ManifestError(f"pages[{page_id}]: members must include readme and mockup.")
    if len(set(members)) != len(members):
        raise ManifestError(f"pages[{page_id}]: member paths must be unique.")
    for member in members:
        if not (root / member).is_file():
            raise ManifestError(f"pages[{page_id}]: member is missing: {member}")
    readme_match = _README_REVISION.search((root / readme).read_text())
    if readme_match is None:
        raise ManifestError(f"pages[{page_id}]: README has no Design revision row.")
    mockup_match = _MOCKUP_REVISION.search((root / mockup).read_text())
    if mockup_match is None:
        raise ManifestError(f"pages[{page_id}]: mockup has no design-revision metadata.")
    if readme_match.group("revision") != revision:
        raise ManifestError(f"pages[{page_id}]: README revision does not match {revision}.")
    if mockup_match.group("revision") != revision:
        raise ManifestError(f"pages[{page_id}]: mockup revision does not match {revision}.")
    return {
        "id": page_id,
        "members": [{"path": str(member), "sha256": _digest(root / member)} for member in members],
        "shared_owners": list(shared_owners),
    }


def _shared_projection(root: pathlib.Path, owner: Mapping, revision: str) -> Mapping:
    _exact(owner, {"id", "members"}, "shared_owners[]")
    owner_id = _string(owner["id"], "shared_owners[].id")
    members = tuple(
        pathlib.Path(_string(item, f"shared_owners[{owner_id}].members[]"))
        for item in _sequence(owner["members"], f"shared_owners[{owner_id}].members")
    )
    if not members or len(set(members)) != len(members):
        raise ManifestError(f"shared_owners[{owner_id}]: members must be non-empty and unique.")
    for member in members:
        if not (root / member).is_file():
            raise ManifestError(f"shared_owners[{owner_id}]: member is missing: {member}")
    return {
        "id": owner_id,
        "members": [{"path": str(member), "sha256": _digest(root / member)} for member in members],
        "revision": revision,
    }


def generated_document(root: pathlib.Path) -> Mapping:  # noqa: PLR0912, PLR0915
    """Return the deterministic manifest projection for the governed source closure."""
    source_path = root / MANIFEST_SOURCE_PATH
    if not source_path.is_file():
        raise ManifestError(f"manifest source is missing: {MANIFEST_SOURCE_PATH}")
    source = _mapping(repository_yaml.safe_load(source_path.read_text()), str(MANIFEST_SOURCE_PATH))
    _exact(
        source,
        {"version", "revision", "parent", "gallery", "shared_owners", "pages"},
        "manifest",
    )
    version = source["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise ManifestError("manifest.version: expected an integer.")
    revision = _string(source["revision"], "manifest.revision")
    parent = _mapping(source["parent"], "manifest.parent")
    _exact(
        parent,
        {
            "path",
            "status",
            "implementation_disposition",
            "implementation_decision_date",
            "approval_scope",
        },
        "manifest.parent",
    )
    parent_path = pathlib.Path(_string(parent["path"], "manifest.parent.path"))
    if not (root / parent_path).is_file():
        raise ManifestError(f"manifest.parent: document is missing: {parent_path}")
    parent_text = (root / parent_path).read_text()
    status = _string(parent["status"], "manifest.parent.status")
    disposition = _string(
        parent["implementation_disposition"],
        "manifest.parent.implementation_disposition",
    )
    decision_date = _optional_decision_date(
        parent["implementation_decision_date"],
        "manifest.parent.implementation_decision_date",
    )
    approval_scope = _string(parent["approval_scope"], "manifest.parent.approval_scope")
    if _document_value(parent_text, "Status") != status:
        raise ManifestError("manifest.parent: Status does not match the declared source.")
    if _document_value(parent_text, "Implementation disposition") != disposition:
        raise ManifestError(
            "manifest.parent: Implementation disposition does not match the declared source."
        )
    expected_date = decision_date if decision_date is not None else "Pending owner decision"
    if _document_value(parent_text, "Implementation decision date") != expected_date:
        raise ManifestError(
            "manifest.parent: Implementation decision date does not match the declared source."
        )
    if _document_value(parent_text, "Approval scope") != approval_scope:
        raise ManifestError("manifest.parent: Approval scope does not match the declared source.")

    gallery = pathlib.Path(_string(source["gallery"], "manifest.gallery"))
    if not (root / gallery).is_file():
        raise ManifestError(f"manifest.gallery: document is missing: {gallery}")
    gallery_text = (root / gallery).read_text()
    gallery_match = _MOCKUP_REVISION.search(gallery_text)
    if gallery_match is None or gallery_match.group("revision") != revision:
        raise ManifestError(
            "manifest.gallery: root design-revision does not match manifest.revision."
        )

    shared = tuple(
        _mapping(item, "manifest.shared_owners[]")
        for item in _sequence(source["shared_owners"], "manifest.shared_owners")
    )
    shared_projection = tuple(_shared_projection(root, item, revision) for item in shared)
    shared_ids = {item["id"] for item in shared_projection}
    if len(shared_ids) != len(shared_projection):
        raise ManifestError("manifest.shared_owners: owner IDs must be unique.")

    pages = tuple(
        _mapping(item, "manifest.pages[]") for item in _sequence(source["pages"], "manifest.pages")
    )
    page_projection = tuple(_page_projection(root, item, revision) for item in pages)
    page_ids = {item["id"] for item in page_projection}
    if len(page_ids) != len(page_projection):
        raise ManifestError("manifest.pages: page IDs must be unique.")
    for page in page_projection:
        unknown = set(cast("list[str]", page["shared_owners"])) - shared_ids
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ManifestError(f"pages[{page['id']}]: unknown shared owner(s): {names}")
    if gallery_text.count(f"Design revision · {revision}") != len(page_projection):
        raise ManifestError("manifest.gallery: page labels do not match the complete page roster.")

    return {
        "gallery": {"path": str(gallery), "sha256": _digest(root / gallery)},
        "pages": list(page_projection),
        "parent": {
            "approval_scope": approval_scope,
            "implementation_decision_date": decision_date,
            "implementation_disposition": disposition,
            "path": str(parent_path),
            "sha256": _digest(root / parent_path),
            "status": status,
        },
        "revision": revision,
        "shared_owners": list(shared_projection),
        "version": version,
    }


def rendered(root: pathlib.Path) -> str:
    """Render the committed manifest as stable JSON."""
    return json.dumps(generated_document(root), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    """Write the manifest, check its committed projection, or require owner approval."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-approved", action="store_true")
    args = parser.parse_args(argv)
    root = pathlib.Path.cwd()
    try:
        expected = rendered(root)
    except ManifestError as error:
        print(f"DESIGN MANIFEST ERROR: {error}")  # noqa: T201
        return 1
    output_path = root / MANIFEST_OUTPUT_PATH
    if args.check:
        if not output_path.is_file() or output_path.read_text() != expected:
            print(f"design manifest is stale: {MANIFEST_OUTPUT_PATH}")  # noqa: T201
            return 1
        print(f"design manifest is current: {MANIFEST_OUTPUT_PATH}")  # noqa: T201
    else:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(expected)
        print(f"wrote {MANIFEST_OUTPUT_PATH}")  # noqa: T201
    if args.require_approved:
        parent = cast("Mapping", generated_document(root)["parent"])
        decision_date = parent["implementation_decision_date"]
        approved = parent["implementation_disposition"] == "approved_for_implementation"
        if not approved or not isinstance(decision_date, str):
            print("implementation remains blocked: parent owner approval is pending.")  # noqa: T201
            return 1
        print("parent implementation approval is recorded.")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
