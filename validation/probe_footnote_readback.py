# SPDX-License-Identifier: MIT-0
"""HIST-8, read-only: one persona readback of a presented V2 page's footnote after the bake.

    uv run python validation/probe_footnote_readback.py --profile <profile> --region <region> \
        --expected-account <account> --environment dev --acceptance-execution-id <id> \
        --not-before <bake finalize end> --output <execution dir>/footnote-readback-verdict.json \
        [--persona probe-a] [--page-path /campaigns/]

Signs the persona in, renders the page in a headless browser (`probe_footnote_readback.mjs`,
the frontend's own Playwright), and judges it with `graph_footnote_verdict`. The receipt names
this execution, so the hold-clear matrix binds it by its own ID. Exit 0 only on PASS.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

_ROOT: Final = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "validation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import probe_ingestion_traceability_gate as gate  # noqa: E402

SCHEMA: Final = "graph-footnote-readback-v1"
READER: Final = Path(__file__).with_name("probe_footnote_readback.mjs")
RENDER_TIMEOUT_SECONDS: Final = 180


def receipt(
    readback: Mapping[str, object],
    texts: Mapping[str, object],
    *,
    execution_id: str,
    not_before: str,
) -> dict[str, object]:
    """Bind one readback and its verdict to this execution; the full texts are not retained."""
    page_text = str(texts.get("page_text") or "")
    raw = texts.get("asset_texts")
    assets = [str(item) for item in cast("list[object]", raw)] if isinstance(raw, list) else []
    verdict = gate.graph_footnote_verdict(readback, assets, page_text, not_before=not_before)
    return {
        "schema_version": SCHEMA,
        "acceptance_execution_id": execution_id,
        "not_before": not_before,
        "readback": dict(readback),
        **verdict,
    }


def render_failed(
    error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
) -> dict[str, object]:
    """Record a reader that exited non-zero or overran its limit as `render_failed`."""
    failure: dict[str, object] = (
        {"kind": "timeout", "timeout_seconds": error.timeout}
        if isinstance(error, subprocess.TimeoutExpired)
        else {"kind": "exit", "returncode": error.returncode}
    )
    return {
        "render_failed": failure,
        "generation": None,
        "window_end_iso": None,
        "read_at": datetime.now(UTC).isoformat(),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Sign in, render, judge and write the receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "--profile",
        "--region",
        "--expected-account",
        "--environment",
        "--acceptance-execution-id",
        "--not-before",
        "--output",
    ):
        parser.add_argument(name, required=True)
    parser.add_argument("--persona", default="probe-a")
    parser.add_argument("--page-path", default="/campaigns/")
    arguments = parser.parse_args(argv)
    import boto3

    session = boto3.Session(profile_name=arguments.profile, region_name=arguments.region)
    if session.client("sts").get_caller_identity()["Account"] != arguments.expected_account:
        print("NOT_PROVEN: the explicit profile is not the expected account", file=sys.stderr)
        return 2
    api = gate._persona_api(session, arguments.environment, arguments.persona)  # pyright: ignore[reportPrivateUsage]
    output = Path(arguments.output)
    raw = output.with_suffix(".readback.json")
    handle, name = tempfile.mkstemp(prefix="adl-footnote-", suffix=".json")
    os.close(handle)
    texts_path = Path(name)
    try:
        environment = {
            **os.environ,
            "ADL_BASE_URL": api.origin,
            "ADL_SESSION": api.session,
            "ADL_PATH": arguments.page_path,
            "ADL_OUT": str(raw),
            "ADL_TEXTS": str(texts_path),
        }
        try:
            subprocess.run(  # noqa: S603 -- a fixed argument list, no shell
                ["node", str(READER)],  # noqa: S607 -- the operator's node, as the frontend uses
                env=environment,
                check=True,
                timeout=RENDER_TIMEOUT_SECONDS,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            # The reader produced no readback: still write a NOT_PROVEN receipt, so the
            # matrix reports this execution's render failure rather than an absent receipt.
            readback = render_failed(error)
            texts: dict[str, object] = {}
        else:
            readback = json.loads(raw.read_text(encoding="utf-8"))
            texts = json.loads(texts_path.read_text(encoding="utf-8"))
    finally:
        texts_path.unlink(missing_ok=True)
        raw.unlink(missing_ok=True)
    document = receipt(
        readback,
        texts,
        execution_id=arguments.acceptance_execution_id,
        not_before=arguments.not_before,
    )
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"receipt: {output} disposition={document['disposition']} reasons={document['reasons']}")
    return 0 if document["passed"] is True else 1


if __name__ == "__main__":
    sys.exit(main())
