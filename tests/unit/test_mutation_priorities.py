# SPDX-License-Identifier: MIT-0
"""mutation-priorities.yaml: well-formed, every path real, every mapped test reaching its unit.

`make mutate-next` works through this file in order, and a unit's fingerprint hashes the tests
mapped to it. A unit or test that does not exist, a test that never imports its unit, or a tier
holding the wrong layer would spend the owner's mutation runs on the wrong code, or report a
unit as covered by tests that cannot see it.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
T1_FRONTEND = {
    "frontend/lib/exact-format.ts",
    "frontend/lib/display-value.ts",
    "frontend/lib/change.ts",
}


@pytest.fixture(scope="module")
def mutation() -> ModuleType:
    """Load scripts/mutation.py as a module so its resolver is the one under test."""
    spec = importlib.util.spec_from_file_location("mutation", ROOT / "scripts" / "mutation.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered first: the script's dataclasses resolve their own module while they are created.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def units(mutation: ModuleType) -> list[Any]:
    """Every unit of the real file, validated by the loader every command uses."""
    return list(mutation.load_priorities(ROOT / "mutation-priorities.yaml").units)


def test_the_tiers_are_exactly_t1_t2_t3() -> None:
    document = yaml.safe_load((ROOT / "mutation-priorities.yaml").read_text())
    assert document["version"] == 1
    assert list(document["tiers"]) == ["T1", "T2", "T3"]


def test_no_unit_is_listed_twice(units: list[Any]) -> None:
    names = [u.unit for u in units]
    assert len(names) == len(set(names))


def test_every_unit_mutate_path_and_test_file_exists(units: list[Any]) -> None:
    missing = [
        path for u in units for path in (u.unit, *u.mutate, *u.tests) if not (ROOT / path).is_file()
    ]
    assert missing == []


def test_every_mapped_test_imports_its_unit(mutation: ModuleType, units: list[Any]) -> None:
    tracked = mutation.tracked_files(ROOT)
    # Each test file is parsed once; the mapping lists most test files under several units.
    targets = {
        test: mutation.test_targets(test, (ROOT / test).read_text(), tracked)
        for test in {t for u in units for t in u.tests}
    }
    unreached = [
        f"{u.unit} <- {test}"
        for u in units
        for test in u.tests
        if not mutation.maps_to(u.unit, targets[test])
    ]
    assert unreached == []


def test_t1_holds_only_domain_inbound_handlers_and_figure_formatting(units: list[Any]) -> None:
    stray = [
        u.unit
        for u in units
        if u.tier == "T1"
        and not u.unit.startswith(("src/domain/", "src/adapters/inbound/"))
        and u.unit not in T1_FRONTEND
    ]
    assert stray == []


def test_t3_holds_only_infra_lib(units: list[Any]) -> None:
    assert [u.unit for u in units if u.tier == "T3" and not u.unit.startswith("infra/lib/")] == []


MALFORMED: list[tuple[dict[str, Any], str]] = [
    ({"version": 2, "tiers": {}}, "version: 1"),
    (
        {
            "version": 1,
            "tiers": {
                "T1": {"description": "", "units": []},
                "T2": {"description": "", "units": []},
            },
        },
        "exactly T1, T2, T3",
    ),
    (
        {
            "version": 1,
            "tiers": {
                "T1": {"description": "", "units": [{"unit": "src/domain/a.py", "tests": []}]},
                "T2": {"description": "", "units": [{"unit": "src/domain/a.py", "tests": []}]},
                "T3": {"description": "", "units": []},
            },
        },
        "listed twice",
    ),
    (
        {
            "version": 1,
            "tiers": {
                "T1": {"description": "", "units": [{"unit": "src/domain/a.py"}]},
                "T2": {"description": "", "units": []},
                "T3": {"description": "", "units": []},
            },
        },
        "keys are unit",
    ),
    (
        {
            "version": 1,
            "tiers": {
                "T1": {
                    "description": "",
                    "units": [
                        {
                            "unit": "src/domain/a.py",
                            "mutate": ["src/domain/a.py", "src/domain/b.py"],
                            "tests": [],
                        }
                    ],
                },
                "T2": {"description": "", "units": []},
                "T3": {"description": "", "units": []},
            },
        },
        "exactly one module",
    ),
]


@pytest.mark.parametrize(("document", "message"), MALFORMED)
def test_the_loader_refuses_a_malformed_document(
    mutation: ModuleType, document: dict[str, Any], message: str
) -> None:
    with pytest.raises(mutation.MutationError, match=message):
        mutation.parse_priorities(document)


def test_the_loader_accepts_the_minimal_document_beside_those_refusals(
    mutation: ModuleType,
) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "tiers": {
            "T1": {
                "description": "",
                "units": [{"unit": "src/domain/a.py", "tests": ["tests/unit/test_a.py"]}],
            },
            "T2": {"description": "", "units": []},
            "T3": {"description": "", "units": []},
        },
    }
    (unit,) = mutation.parse_priorities(document).units
    assert unit.mutate == ("src/domain/a.py",)
    assert unit.language == "python"


@pytest.mark.parametrize(
    ("path", "tier"),
    [
        ("src/domain/measurement/figures.py", "T1"),
        ("src/adapters/inbound/presentation_handlers/handler.py", "T1"),
        ("frontend/lib/display-value.ts", "T1"),
        ("frontend/lib/presentation-v2.ts", "T2"),
        ("src/adapters/outbound/athena.py", "T2"),
        ("src/adapters/config.py", "T2"),
        ("infra/lib/config/index.ts", "T3"),
        ("src/domain/__init__.py", None),
        ("src/ports/outbound/reader.py", None),
        ("src/domain/operations/generated_data_flows.py", None),
        ("infra/lib/generated/alarm-destinations.ts", None),
        ("frontend/test/display-value.test.ts", None),
    ],
)
def test_propose_tiers_each_layer_as_d7_says(
    mutation: ModuleType, path: str, tier: str | None
) -> None:
    assert mutation.tier_of(path) == tier


def test_the_resolver_follows_from_package_imports_and_module_names(mutation: ModuleType) -> None:
    text = (
        "from domain.identity import caller\nimport domain.semantics.time\n"
        "NAME = 'adapters.inbound.auth_handlers.authorizer'\n"
    )
    found = mutation.test_targets("tests/unit/test_x.py", text, frozenset())
    assert mutation.maps_to("src/domain/identity/caller.py", found)
    assert mutation.maps_to("src/domain/semantics/time.py", found)
    assert mutation.maps_to("src/adapters/inbound/auth_handlers/authorizer.py", found)
    assert not mutation.maps_to("src/domain/identity/tokens.py", found)


def test_the_typescript_resolver_follows_the_alias_and_relative_paths(mutation: ModuleType) -> None:
    tracked = frozenset(
        {
            "frontend/lib/change.ts",
            "frontend/components/chip/index.tsx",
            "infra/lib/config/index.ts",
        }
    )
    frontend = "import { a } from '@/lib/change';\nimport Chip from '../components/chip';\n"
    assert mutation.test_targets("frontend/test/x.test.tsx", frontend, tracked) == {
        "frontend/lib/change.ts",
        "frontend/components/chip/index.tsx",
    }
    infra = "import {\n  loadConfig,\n} from '../lib/config';\nimport { App } from 'aws-cdk-lib';\n"
    assert mutation.test_targets("infra/test/x.test.ts", infra, tracked) == {
        "infra/lib/config/index.ts"
    }
