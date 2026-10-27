# SPDX-License-Identifier: MIT-0
"""The one repository YAML parser: libyaml's C parser, safe construction, duplicate keys refused.

A failure here means a Lambda either pays the pure-Python parse on every cold start, could
construct a Python object from a declaration, or would accept a contract whose repeated key
silently overrides the first.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from yaml._yaml import CParser

from adapters.jobs.contract_validation import insight_loader
from adapters.jobs.data_flow_registry import contract as data_flow_contract
from adapters.jobs.data_flow_registry.contract import DataFlowRegistryError, load_registry
from adapters.jobs.trace_contract import contract as trace_contract
from adapters.outbound import repository_yaml
from adapters.outbound.commerce_identity import (
    CommerceIdentityLoadError,
    load_commerce_identity_bridge,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_the_repository_loader_parses_with_libyamls_c_parser() -> None:
    assert yaml.__with_libyaml__
    assert issubclass(repository_yaml.RepositoryLoader, CParser)
    assert issubclass(repository_yaml.RepositoryLoader, yaml.constructor.SafeConstructor)


@pytest.mark.parametrize(
    "strict",
    [
        insight_loader._StrictSafeLoader,  # pyright: ignore[reportPrivateUsage]
        data_flow_contract._StrictSafeLoader,  # pyright: ignore[reportPrivateUsage]
        trace_contract._StrictSafeLoader,  # pyright: ignore[reportPrivateUsage]
    ],
    ids=["insight", "data-flow-registry", "trace-contract"],
)
def test_every_duplicate_key_loader_extends_the_repository_loader(strict: type) -> None:
    assert issubclass(strict, repository_yaml.RepositoryLoader)


def test_a_plain_document_constructs_as_plain_values() -> None:
    assert repository_yaml.safe_load("version: 1\nnames: [a, b]\n") == {
        "version": 1,
        "names": ["a", "b"],
    }


def test_a_python_tag_is_refused_by_the_safe_constructor() -> None:
    with pytest.raises(yaml.constructor.ConstructorError, match=r"python/object/apply:os\.system"):
        repository_yaml.safe_load("!!python/object/apply:os.system ['true']\n")


@dataclass(frozen=True)
class _Repeated:
    relative: str
    first_line: str
    load: Callable[[Path], object]
    error: type[Exception]


@pytest.mark.parametrize(
    "case",
    [
        _Repeated(
            "contracts/shared/commerce-identities.yaml",
            "version: 1",
            load_commerce_identity_bridge,
            CommerceIdentityLoadError,
        ),
        _Repeated(
            "contracts/operations/data-flows.yaml",
            'registry_version: "1"',
            load_registry,
            DataFlowRegistryError,
        ),
    ],
    ids=["compose-check", "constructor-check"],
)
def test_a_repeated_top_level_key_is_refused_under_the_c_parser(
    tmp_path: Path, case: _Repeated
) -> None:
    target = tmp_path / case.relative
    target.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / case.relative, target)
    case.load(tmp_path)  # positive control: the committed file loads
    target.write_text(
        target.read_text().replace(case.first_line, f"{case.first_line}\n{case.first_line}", 1)
    )

    with pytest.raises(case.error, match="duplicate"):
        case.load(tmp_path)
