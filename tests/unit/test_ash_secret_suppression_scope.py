# SPDX-License-Identifier: MIT-0
"""Secret suppressions outside test fixtures are bound to reviewed values (O-ASH-SECRET-SCOPE-001).

`.ash/.ash.yaml` can suppress a detect-secrets rule only by file path, so a path entry alone would
also hide a real hex credential added to that file. Every entry whose rule_id ASH could match to
a secret rule, a glob or an absent rule_id included, is governed here. Outside the fixture trees,
such an entry is allowed only when it names one exact file, never a glob, and every hex run in
that file is accounted for by the file's own source (A-ENTROPY-IDENTIFIER-001): the digest of a
committed file or of a contract (bound by value), a line of the deep-input registry's own shape
(bound by shape), or a documented vector's own stated digest (bound by consistency). A pasted hex
value in one of those files, a glob, or an unbound suppressed path fails this test before the
scan can hide it.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
import yaml

from adapters.deep_input_revision import DEEP_INPUT_REVISIONS
from adapters.jobs.data_flow_registry import alarm_destinations, execution_graphs
from domain.operations.status import canonical_authority_bytes, canonical_authority_digest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
ASH_CONFIG = ROOT / ".ash" / ".ash.yaml"
SECRET_RULE_PATTERNS = ("secret-*", "ckv_secret_*", "*entropy*")
SECRET_RULE_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
GLOB_CHARACTERS = frozenset("*?[")
FIXTURE_PREFIXES = ("tests/fixtures/", "frontend/test/fixtures/")
"""The approved scope for path-only secret suppressions: synthetic fixtures, never shipped."""
HEX_RUN = re.compile(r"(?<![0-9A-Za-z])(?!\d{1,13}(?![0-9A-Za-z]))[0-9a-fA-F]{9,}(?![0-9A-Za-z])")
"""A superset of the runs detect-secrets could score above its 3.0 hex limit. Eight symbols cap at
3.0, so a run needs nine characters. After the plugin's decimal penalty, an all-decimal run first
exceeds 3.0 at 19 digits (3.018; 18 digits reach 2.993), but this excludes only runs of 13 digits
or fewer. That errs toward failing this test on a decimal detect-secrets would never flag."""

Verifier = Callable[[Path, str], set[str]]


def _design_manifest(root: Path, text: str) -> set[str]:
    """Each `sha256` that is the digest of the committed file its sibling `path` names."""
    try:
        document: object = json.loads(text)
    except json.JSONDecodeError:
        return set()
    derived: set[str] = set()
    pending: list[object] = [document]
    while pending:
        node = pending.pop()
        if isinstance(node, list):
            pending.extend(cast("list[object]", node))
        elif isinstance(node, dict):
            entry = cast("dict[str, object]", node)
            pending.extend(entry.values())
            path, digest = entry.get("path"), entry.get("sha256")
            if (
                isinstance(path, str)
                and isinstance(digest, str)
                and (root / path).is_file()
                and hashlib.sha256((root / path).read_bytes()).hexdigest() == digest
            ):
                derived.add(digest)
    return derived


REGISTRY_ENTRY = re.compile(
    r'^\s*\("(?P<name>\d{4}-\d{2}-\d{2}(?:\.\d+)+)", "(?P<digest>[0-9a-f]{64})"\),$'
)


def _registry_entries(text: str) -> list[tuple[str, str]]:
    return [
        (match["name"], match["digest"])
        for match in map(REGISTRY_ENTRY.match, text.splitlines())
        if match is not None
    ]


def _deep_input_revisions(_root: Path, text: str) -> set[str]:
    """Each digest on a line of the registry's own `("<revision>", "<sha256>"),` shape.

    This binds by shape, not by value. `test_the_registry_lines_are_the_registry` holds those
    lines to `DEEP_INPUT_REVISIONS`, and `test_deep_input_revision.py` holds only the last entry's
    digest to the code closure, so a well-formed new entry with any 64-hex value passes here. It
    refuses a value pasted anywhere else in the file.
    """
    return {digest for _, digest in _registry_entries(text)}


def _contract_digest(contract: Path) -> Verifier:
    """Derive the generator's `CONTRACT_DIGEST`: the SHA-256 of the contract it projects."""

    def verify(root: Path, _text: str) -> set[str]:
        return {hashlib.sha256((root / contract).read_bytes()).hexdigest()}

    return verify


VECTOR_JSON = re.compile(r"^(?P<label>[^=]+) JSON = (?P<json>.+)$")
VECTOR_LENGTH = re.compile(r"^(?P<label>[^=]+) UTF-8 length = (?P<length>\d+)$")
VECTOR_DIGEST = re.compile(r"^(?P<label>[^=]+) = (?P<digest>[0-9a-f]{64})$")


def _documented_vectors(_root: Path, text: str) -> set[str]:
    """Each vector's digest and the values inside its canonical JSON, bound by consistency.

    A vector is written as three lines: `<label> JSON = <json>`, `<label> UTF-8 length = <n>`,
    then `<name> = <digest>`. Its members count as derived only when the JSON is canonical and
    its own two following lines state its byte length and `canonical_authority_digest`, so one
    vector's digest never binds another's JSON. This is a consistency binding, not a review: it
    refuses a pasted value, and an author who recomputes all three lines gets the value through.
    """
    lines = text.splitlines()
    derived: set[str] = set()
    for index, line in enumerate(lines):
        match = VECTOR_JSON.match(line)
        if match is None or index + 2 >= len(lines):
            continue
        length = VECTOR_LENGTH.match(lines[index + 1])
        stated = VECTOR_DIGEST.match(lines[index + 2])
        encoded = match["json"].encode()
        if length is None or length["label"] != match["label"] or stated is None:
            continue
        try:
            value: object = json.loads(match["json"])
        except json.JSONDecodeError:
            continue
        digest = canonical_authority_digest(value)
        if (
            canonical_authority_bytes(value) == encoded
            and int(length["length"]) == len(encoded)
            and stated["digest"] == digest
        ):
            derived |= {digest, *HEX_RUN.findall(match["json"])}
    return derived


BOUND: dict[str, Verifier] = {
    "docs/ui/generated/design-manifest.json": _design_manifest,
    "src/adapters/deep_input_revision.py": _deep_input_revisions,
    str(alarm_destinations.TYPESCRIPT_PATH): _contract_digest(alarm_destinations.CONTRACT_PATH),
    str(alarm_destinations.PYTHON_PATH): _contract_digest(alarm_destinations.CONTRACT_PATH),
    str(execution_graphs.TYPESCRIPT_PATH): _contract_digest(execution_graphs.CONTRACT_PATH),
    str(execution_graphs.PYTHON_PATH): _contract_digest(execution_graphs.CONTRACT_PATH),
    "docs/capabilities/load-path/write-discipline.md": _documented_vectors,
}
"""Every secret-suppressed path outside the fixture trees, and how its hex values are derived."""


def _unexpected(path: str, text: str) -> set[str]:
    return set(HEX_RUN.findall(text)) - BOUND[path](ROOT, text)


def _unreviewed_value() -> str:
    """Build a 64-hex value at runtime, so this file carries no hex literal of its own."""
    return hashlib.sha256(b"a value no reviewer saw").hexdigest()


def _governs_secrets(rule_id: object) -> bool:
    """Whether ASH could apply a suppression with this `rule_id` to a secret finding.

    ASH 3.7.0 matches `fnmatch(finding.lower(), rule_id.lower())`, and a suppression with no
    `rule_id` matches every rule (`AshSuppression.matches`). A secret rule ID, lower-cased, uses
    only `SECRET_RULE_ALPHABET`: detect-secrets IDs are `SECRET-` plus the upper-cased type with
    each run of non-word characters turned into `-`, and Checkov's are `CKV_SECRET_<n>`. So a
    literal rule_id governs secrets when it names one, and a glob does unless one of its literal
    characters lies outside that alphabet, which no secret rule ID could then match (a trivy
    licence glob such as `*:MIT`). A bracket class is never read that way: any `[` governs.
    """
    if not rule_id:
        return True
    lowered = str(rule_id).lower()
    if any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_RULE_PATTERNS):
        return True
    if not GLOB_CHARACTERS.intersection(lowered):
        return False
    literals = set(lowered) - GLOB_CHARACTERS
    return "[" in lowered or literals <= SECRET_RULE_ALPHABET


def _secret_suppressed_paths(suppressions: list[dict[str, object]]) -> set[str]:
    """Every non-fixture path a suppression that ASH could apply to a secret finding names."""
    return {
        str(entry["path"])
        for entry in suppressions
        if _governs_secrets(entry.get("rule_id"))
        and not str(entry["path"]).startswith(FIXTURE_PREFIXES)
    }


def _suppressions() -> list[dict[str, object]]:
    config = yaml.safe_load(ASH_CONFIG.read_text())
    return cast("list[dict[str, object]]", config["global_settings"]["suppressions"])


def test_secret_suppressions_outside_fixtures_name_exactly_the_bound_files() -> None:
    paths = _secret_suppressed_paths(_suppressions())
    assert paths == BOUND.keys(), "bind each non-fixture secret suppression in this test"
    assert all((ROOT / path).is_file() for path in paths)


SECRET_RULE_PROBES = (
    "SECRET-HEX-HIGH-ENTROPY-STRING",
    "SECRET-BASE64-HIGH-ENTROPY-STRING",
    "CKV_SECRET_6",
)
"""Secret rule IDs ASH reports here: detect-secrets `SECRET-<type>` and Checkov `CKV_SECRET_<n>`."""


@pytest.mark.parametrize(
    "rule_id",
    [
        "*",
        "secret-hex-*",
        "*ENTROPY*",
        "secret-hex-high-entropy-string",
        "ckv_secret_6",
        "?ECRET-HEX-HIGH-ENTROPY-STRING",
        "[Ss]ECRET-*",
        None,
        "",
    ],
)
def test_a_suppression_ash_could_apply_to_a_secret_is_held_to_the_bound_files(
    rule_id: str | None,
) -> None:
    # ASH 3.7.0 matches `fnmatch(finding.lower(), rule_id.lower())`, and a suppression with no
    # rule_id matches every rule, so each of these hides a secret finding on its path.
    if rule_id:
        assert any(
            fnmatch.fnmatchcase(probe.lower(), rule_id.lower()) for probe in SECRET_RULE_PROBES
        )
    entry: dict[str, object] = {"path": "src/adapters/unreviewed.py", "reason": "probe"}
    if rule_id is not None:
        entry["rule_id"] = rule_id
    assert "src/adapters/unreviewed.py" in _secret_suppressed_paths([*_suppressions(), entry])


@pytest.mark.parametrize("path", sorted(BOUND))
def test_every_hex_value_in_a_bound_file_is_derived_from_its_source(path: str) -> None:
    text = (ROOT / path).read_text()
    assert HEX_RUN.search(text), f"{path} has no hex value; drop its suppression"
    assert not _unexpected(path, text), (
        f"{path} carries a hex value its source does not derive; remove it, regenerate the file, "
        "or review it and bind it in this test before the ASH suppression can cover it"
    )


def test_the_registry_lines_are_the_registry() -> None:
    text = (ROOT / "src/adapters/deep_input_revision.py").read_text()
    assert tuple(_registry_entries(text)) == DEEP_INPUT_REVISIONS


@pytest.mark.parametrize("path", sorted(BOUND))
def test_an_added_hex_value_is_refused(path: str) -> None:
    value = _unreviewed_value()
    text = (ROOT / path).read_text() + f'\nTOKEN = "{value}"\n'
    assert value in _unexpected(path, text)


def test_a_vector_binds_its_members_only_through_its_own_length_and_digest_lines() -> None:
    path = "docs/capabilities/load-path/write-discipline.md"
    text = (ROOT / path).read_text()
    vectors = [match for match in map(VECTOR_JSON.match, text.splitlines()) if match is not None]
    vector, member = next(
        (match, run)
        for match in vectors
        for run in HEX_RUN.findall(match["json"])
        if len(run) == 64
    )
    value = _unreviewed_value()
    forged = vector["json"].replace(member, value, 1)
    # A digest stated on some other line is not this vector's digest, so it binds nothing.
    digest = canonical_authority_digest(json.loads(forged))
    added = f"\n{vector['label']} JSON = {forged}\nother = {digest}\n"
    assert value in _unexpected(path, text + added)


@pytest.mark.parametrize("path", sorted(set(BOUND) - {"src/adapters/deep_input_revision.py"}))
def test_a_replaced_derived_value_is_refused(path: str) -> None:
    text = (ROOT / path).read_text()
    first = HEX_RUN.findall(text)[0]
    value = _unreviewed_value()
    assert value in _unexpected(path, text.replace(first, value, 1))
