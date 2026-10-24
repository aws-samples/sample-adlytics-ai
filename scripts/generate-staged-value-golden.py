# SPDX-License-Identifier: MIT-0
"""Render T-4's edge inputs through the base value renderer into the staged-value golden.

ADR-149 decision 3: every staged cell is the exact text the deleted ``VALUES`` renderer
(``_value`` in ``src/adapters/outbound/athena/semantic_storage.py``) sent, as a parameter or
as an inlined literal, or JSON ``null``. This script runs once, while that file is still the
base renderer, and records each edge input's text so ``tests/unit/test_athena_staging.py``
can hold the staging renderer to it after the base renderer is deleted.

It refuses to run unless the renderer file's git blob equals the base blob, and writes that
blob into the golden. ``_value`` is loaded with ``importlib`` inside ``main()`` because the
symbol is deleted later; nothing here imports staging code.

Usage (from the repository root):
    uv run python scripts/generate-staged-value-golden.py \
        tests/unit/golden/staged_value_edge_inputs.json > tests/unit/golden/staged_value_text.json
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
RENDERER = ROOT / "src" / "adapters" / "outbound" / "athena" / "semantic_storage.py"
# pragma: allowlist nextline secret (A-ENTROPY-IDENTIFIER-001)
BASE_BLOB = "d5346b39ac87b94a276ec0bbb9d2ffa4a9127c76"


def git_blob(path: Path) -> str:
    """Return the git blob id of a file, as ``git hash-object`` computes it."""
    data = path.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data, usedforsecurity=False).hexdigest()


def decoded(tagged: dict[str, Any]) -> object:
    """Decode one tagged edge value to the Python type the writers pass."""
    tag = tagged["t"]
    if tag == "null":
        return None
    raw = tagged["v"]
    decoders = {
        "str": str,
        "int": int,
        "decimal": Decimal,
        "bool": bool,
        "date": date.fromisoformat,
        "datetime": datetime.fromisoformat,
    }
    return decoders[tag](raw)


def unquoted(literal: str) -> str:
    """Undo ``sql_literal``: drop the outer quotes and un-double each inner quote."""
    if len(literal) < 2 or literal[0] != "'" or literal[-1] != "'":  # noqa: PLR2004
        raise ValueError("an inlined value is one quoted SQL literal.")
    return literal[1:-1].replace("''", "'")


def rendered(render: Any, column: Any, value: object) -> dict[str, object]:
    """Return the text ``_value`` sends for one value, or the error class it raises."""
    try:
        expression, parameters = render(value, column)
    except ValueError as error:
        return {"error": type(error).__name__}
    if parameters:
        return {"text": parameters[0]}
    if expression.startswith("CAST(NULL AS "):
        return {"text": None}
    inner = expression[len("CAST(") : expression.rindex(" AS ")]
    return {"text": unquoted(inner)}


def main(argv: list[str]) -> int:
    """Write the golden for the input file named by ``argv[0]`` to stdout."""
    if len(argv) != 1:
        sys.stderr.write("usage: generate-staged-value-golden.py <edge-inputs.json>\n")
        return 2
    blob = git_blob(RENDERER)
    if blob != BASE_BLOB:
        sys.stderr.write(f"refused: {RENDERER.name} is blob {blob}, not the base {BASE_BLOB}\n")
        return 1
    sys.path.insert(0, str(ROOT / "src"))
    storage = importlib.import_module("adapters.outbound.athena.semantic_storage")
    catalog = importlib.import_module("adapters.outbound.v2_storage_catalog")
    inputs = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    texts: list[dict[str, object]] = []
    for entry in inputs["entries"]:
        column = catalog.V2Column(entry["name"], entry["type"], entry["nullable"])
        texts.append(
            {"name": entry["name"], **rendered(storage._value, column, decoded(entry["value"]))}
        )
    golden = {"renderer_blob": blob, "texts": texts}
    sys.stdout.write(json.dumps(golden, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
