# SPDX-License-Identifier: MIT-0
"""Load the deployment's governed AMC instance kind (`contracts/shared/amc-instance.yaml`)."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import yaml

from domain.semantics.amc_instance import (
    AMC_INSTANCE_KIND_UNKNOWN,
    AmcInstanceResolution,
    resolve_amc_instance,
)

__all__ = ["AMC_INSTANCE_PATH", "load_amc_instance"]

AMC_INSTANCE_PATH = Path("contracts/shared/amc-instance.yaml")


def load_amc_instance(root: Path) -> AmcInstanceResolution:
    """Resolve the declared kind under `root`; an absent or unparseable file names its reason."""
    path = root / AMC_INSTANCE_PATH
    if not path.is_file():
        return resolve_amc_instance(None)
    try:
        document = cast("object", yaml.safe_load(path.read_text(encoding="utf-8")))
    except yaml.YAMLError:
        return AmcInstanceResolution(None, AMC_INSTANCE_KIND_UNKNOWN)
    return resolve_amc_instance(document)
