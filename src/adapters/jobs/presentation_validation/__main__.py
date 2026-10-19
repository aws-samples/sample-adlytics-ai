# SPDX-License-Identifier: MIT-0
"""Validate analytical page declarations against governed metrics and frontend bindings."""

from __future__ import annotations

import pathlib
import sys

from adapters.jobs.presentation_validation.bindings import load_v2_page_bindings
from adapters.jobs.presentation_validation.generate import rendered
from adapters.jobs.presentation_validation.loader import load_page_schemas, load_v2_page_schemas
from adapters.jobs.presentation_validation.navigation import load_navigation, stale_projections
from adapters.outbound.attribution_models import load_attribution_models
from adapters.outbound.metric_dictionary import load_catalog
from domain.presentation.page_schemas import (
    PageSchemaError,
    PageValidationContext,
    validate_page_schemas,
)


def main() -> int:
    """Report every deterministic page-schema or rendering finding."""
    root = pathlib.Path.cwd()
    print("ANALYTICAL PAGE SCHEMA VALIDATION")
    print("=" * 70)
    try:
        schemas = load_page_schemas(root)
        presentation_schemas = load_v2_page_schemas(root)
        catalog = load_catalog(root)
        attribution_models = load_attribution_models(root)
        observed = load_v2_page_bindings(root)
        binding_path = root / "frontend/generated/page-bindings.json"
        if binding_path.read_text() != rendered(root, schema_version=2):
            raise PageSchemaError(f"frontend page bindings are stale at {binding_path}.")
        navigation = load_navigation(root)
        stale = stale_projections(root)
        if stale:
            raise PageSchemaError(
                "navigation projections are stale: " + ", ".join(str(path) for path in stale)
            )
    except PageSchemaError as error:
        print(f"  ERROR: {error}")
        return 1

    validation_context = PageValidationContext(catalog, attribution_models)
    findings = validate_page_schemas(schemas, validation_context, presentation_schemas)
    if findings:
        for finding in sorted(findings):
            print(f"  [{finding.rule}] {finding.page_id}: {finding.message}")
        print(f"\n{len(findings)} page-schema finding(s).")
        return 1

    print(f"  frozen V1 schemas: {len(schemas.pages)}")
    print(f"  V2 schemas: {len(presentation_schemas.pages)}")
    print(f"  frontend V2 page bindings: {len(observed.pages)}")
    print(f"  navigation tabs: {len(navigation.tabs)} (route manifest and allowlist current)")
    print("Every frozen V1 schema and V2 binding passed its strict declaration checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
