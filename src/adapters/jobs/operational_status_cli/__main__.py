# SPDX-License-Identifier: MIT-0
r"""`flow explain <manifest-key-or-flow-id>`: print one execution explanation (S2, FR-8).

    uv run python -m adapters.jobs.operational_status_cli explain <target> \
        --origin https://<app origin> --session-file <file holding the adl_session value> \
        [--version-id <VersionId>] [--graph-id <id> [--node <node>]] [--limit N] [--all-pages]

The session value is read from a file, never from argv or the environment, so it never reaches
a process listing. Output is the server's JSON, one page per line. Exit status: 0 for a
complete tree, 2 for a valid but incomplete one (the typed non-success result), 1 for any
refusal or failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from adapters.jobs.operational_status_cli.client import (
    SessionCredential,
    explain,
    reference,
)
from adapters.outbound.https import InsecureUrlError, require_https

_OK = 200
_INCOMPLETE = 2
_MAX_PAGES = 64


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flow", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("explain")
    command.add_argument("target")
    command.add_argument("--origin", required=True)
    command.add_argument("--session-file", required=True, type=Path)
    command.add_argument("--version-id")
    command.add_argument("--graph-id")
    command.add_argument("--node")
    command.add_argument("--limit", type=int)
    command.add_argument("--all-pages", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run `flow explain` and print every server-owned page."""
    arguments = _parser().parse_args(argv)
    try:
        origin = require_https(str(arguments.origin))
    except InsecureUrlError:
        print(json.dumps({"error": "origin_not_https"}), file=sys.stderr)  # noqa: T201
        return 1
    credential = SessionCredential(Path(arguments.session_file).read_text("utf-8").strip())
    body: dict[str, object] = reference(
        str(arguments.target),
        version_id=arguments.version_id,
        graph_id=arguments.graph_id,
        node=arguments.node,
    )
    if arguments.limit is not None:
        body["limit"] = int(arguments.limit)
    complete = True
    for _ in range(_MAX_PAGES):
        response = explain(origin, credential, body)
        print(json.dumps(dict(response.body), sort_keys=True))  # noqa: T201
        if response.status != _OK:
            return 1
        complete = complete and response.body.get("complete") is True
        cursor = response.body.get("next_cursor")
        if not arguments.all_pages or not isinstance(cursor, str):
            break
        body["cursor"] = cursor
    return 0 if complete else _INCOMPLETE


if __name__ == "__main__":
    raise SystemExit(main())
