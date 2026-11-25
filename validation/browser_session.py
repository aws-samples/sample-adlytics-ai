# SPDX-License-Identifier: MIT-0
"""Sign a synthetic dev persona in, then run one browser script with the session in its environment.

    uv run python validation/browser_session.py --profile <PROFILE> --environment dev \
        --region us-east-1 --expected-account <ACCOUNT_ID> [--persona probe-a] -- \
        node validation/probe_frontend_browser.mjs

The command after `--` runs with `ADL_BASE_URL` (the published `publicBaseUrl`) and `ADL_SESSION`
(the persona's access token, about 15 minutes) set in its environment only; `ADL_REFRESH` and
`ADL_CSRF` are removed, so the command acts as the persona and never as an operator's cookie. The
token is never printed or written. Only `dev` in `us-east-1` is accepted. Output is the account
match, the sign-in time and the command's exit status, which this exits with.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Final

_ROOT: Final = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "src", _ROOT / "validation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognito_personas import app_client, load_persona, published_parameter, sign_in  # noqa: E402

_ENVIRONMENT: Final = "dev"
_REGION: Final = "us-east-1"


def target_refused(environment: str, region: str, command: list[str]) -> bool:
    """Refuse every target but dev in us-east-1, and a run with no command to start."""
    return environment != _ENVIRONMENT or region != _REGION or not command


def child_environment(parent: dict[str, str], origin: str, token: str) -> dict[str, str]:
    """Copy the parent's environment without an operator's cookie, adding the origin and session."""
    env = {k: v for k, v in parent.items() if k not in {"ADL_REFRESH", "ADL_CSRF"}}
    return env | {"ADL_BASE_URL": origin, "ADL_SESSION": token}


def _parser() -> argparse.ArgumentParser:
    """Declare every target explicitly; only the persona has a default."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--expected-account", required=True)
    parser.add_argument("--persona", default="probe-a")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Check the target, sign the persona in, and run the command with its session."""
    args = _parser().parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if target_refused(args.environment, args.region, command):
        print("browser_session accepts only dev in us-east-1, with a command after --")
        return 2
    import boto3

    started = time.monotonic()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    if session.client("sts").get_caller_identity().get("Account") != args.expected_account:
        print("account: mismatch")
        return 2
    print("account: match")
    ssm = session.client("ssm")
    origin = published_parameter(ssm, args.environment, "publicBaseUrl").rstrip("/")
    tokens = sign_in(
        session.client("cognito-idp"),
        load_persona(ssm, args.environment, args.persona),
        app_client(session, args.environment),
    )
    print(f"signed in {args.persona} in {time.monotonic() - started:.1f} s", flush=True)
    env = child_environment(dict(os.environ), origin, tokens.access_token)
    # A-SUBPROCESS-OPERATOR-001: operator argv, dev only; see docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    code = subprocess.call(command, env=env)  # noqa: S603 -- the operator's argv, no shell
    print(f"command exit {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
