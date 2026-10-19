# SPDX-License-Identifier: MIT-0
"""Decision Workspace design-manifest generation and approval gating."""

from __future__ import annotations

import pathlib
from typing import cast

import pytest

from adapters.jobs.design_manifest.__main__ import (
    MANIFEST_OUTPUT_PATH,
    ManifestError,
    generated_document,
    main,
    rendered,
)

pytestmark = [pytest.mark.unit]


def _write_candidate(root: pathlib.Path) -> None:
    (root / "docs/ui/page").mkdir(parents=True)
    (root / "docs/ui/README.md").write_text(
        "| **Status** | Pending owner approval |\n"
        "| **Implementation disposition** | Pending owner approval |\n"
        "| **Implementation decision date** | Pending owner decision |\n"
        "| **Approval scope** | Candidate scope |\n"
    )
    (root / "docs/ui/page/README.md").write_text("| **Design revision** | `2026-09-08.1` |\n")
    (root / "docs/ui/page/mockup.html").write_text(
        '<meta name="design-revision" content="2026-09-08.1">\n'
    )
    (root / "docs/ui/gallery.html").write_text(
        '<meta name="design-revision" content="2026-09-08.1">\nDesign revision · 2026-09-08.1\n'
    )
    (root / "docs/ui/design-manifest.yaml").write_text(
        "version: 1\n"
        'revision: "2026-09-08.1"\n'
        "parent:\n"
        "  path: docs/ui/README.md\n"
        "  status: Pending owner approval\n"
        "  implementation_disposition: Pending owner approval\n"
        "  implementation_decision_date: null\n"
        "  approval_scope: Candidate scope\n"
        "gallery: docs/ui/gallery.html\n"
        "shared_owners: []\n"
        "pages:\n"
        "  - id: page\n"
        "    readme: docs/ui/page/README.md\n"
        "    mockup: docs/ui/page/mockup.html\n"
        "    members:\n"
        "      - docs/ui/page/README.md\n"
        "      - docs/ui/page/mockup.html\n"
        "    shared_owners: []\n"
    )


def test_manifest_projection_records_digests_for_the_declared_closure(
    tmp_path: pathlib.Path,
) -> None:
    _write_candidate(tmp_path)

    manifest = generated_document(tmp_path)

    assert manifest["revision"] == "2026-09-08.1"
    pages = cast("list[dict[str, object]]", manifest["pages"])
    assert len(pages) == 1
    page = pages[0]
    assert page["id"] == "page"
    members = cast("list[dict[str, object]]", page["members"])
    assert len(members) == 2
    first_member = members[0]
    assert first_member["path"] == "docs/ui/page/README.md"
    sha256 = cast("str", first_member["sha256"])
    assert len(sha256) == 64


def test_manifest_rejects_a_mockup_revision_that_does_not_match_its_readme(
    tmp_path: pathlib.Path,
) -> None:
    _write_candidate(tmp_path)
    (tmp_path / "docs/ui/page/mockup.html").write_text(
        '<meta name="design-revision" content="2026-09-08.2">\n'
    )

    with pytest.raises(ManifestError, match="mockup revision"):
        generated_document(tmp_path)


def test_check_accepts_a_current_candidate_but_approval_gate_refuses_it(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_candidate(tmp_path)
    output = tmp_path / MANIFEST_OUTPUT_PATH
    output.parent.mkdir(parents=True)
    output.write_text(rendered(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert main(["--check"]) == 0
    assert main(["--check", "--require-approved"]) == 1


def test_approval_gate_requires_a_dated_parent_disposition(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_candidate(tmp_path)
    (tmp_path / "docs/ui/README.md").write_text(
        "| **Status** | Approved |\n"
        "| **Implementation disposition** | approved_for_implementation |\n"
        "| **Implementation decision date** | 2026-09-08 |\n"
        "| **Approval scope** | Candidate scope |\n"
    )
    source_path = tmp_path / "docs/ui/design-manifest.yaml"
    source_path.write_text(
        source_path.read_text()
        .replace("status: Pending owner approval", "status: Approved")
        .replace(
            "implementation_disposition: Pending owner approval",
            "implementation_disposition: approved_for_implementation",
        )
        .replace(
            "implementation_decision_date: null",
            'implementation_decision_date: "2026-09-08"',
        )
    )
    output = tmp_path / MANIFEST_OUTPUT_PATH
    output.parent.mkdir(parents=True)
    output.write_text(rendered(tmp_path))
    monkeypatch.chdir(tmp_path)

    assert main(["--check", "--require-approved"]) == 0
