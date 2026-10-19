# SPDX-License-Identifier: MIT-0
"""Batch-ship composed insight prompts: render → create → calibrate [→ activate].

The composition tooling ADR-076 decision 6 mandates: prompt fragments live in git
(contracts/prompts/insights/), the deterministic domain renderer composes
``foundation + tier core + slot block``, and every composed registry version records
its fragment digests. This tool batches the create → calibrate → activate loop over
slot × tier under bounded concurrency, through the deployed admin routes only
(ADR-069: the registry API is the only path a prompt reaches an environment).

Activation is opt-in (``--activate``): nothing composed is activated before slice
H's coordinated cutover. Prints no prompt text, cookie value, or live identifier.

``--compose <file>`` reads each fragment from a named git revision, so one candidate
fragment is composed against the fragments the active versions came from.
``--record <file>`` appends a JSON line per slot and tier with the composition, body
size, create status, version, calibration and case outcomes, the digest read-back and
the active version at that moment (the rollback target).

    set -a; source validation/.env.local; set +a
    uv run python validation/ship_prompts.py --tier card --slot metric-focus
    uv run python validation/ship_prompts.py --tier card --tier deep --all-slots \
        --concurrency 3
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from adapters.outbound.https import HttpsSession, InsecureUrlError, require_https  # noqa: E402
from domain.presentation.prompt_composition import (  # noqa: E402
    ComposedPrompt,
    PromptFragmentSet,
    compose_prompt,
)

# Every request goes through the HTTPS-only boundary, and no redirect is followed: the operator's
# cookies must never reach another origin or a plaintext hop (H-VALIDATION-HTTPS-001).
_HTTP = HttpsSession()
_OK = 200
_ENV_HINT = "Source validation/.env.local (ADL_BASE_URL https, ADL_REFRESH, ADL_CSRF)."
_REPOSITORY = Path(__file__).resolve().parents[1]
_FRAGMENTS = _REPOSITORY / "contracts" / "prompts" / "insights"
_HEADERS_BASE = {"Content-Type": "application/json", "Sec-Fetch-Site": "same-origin"}
_MODEL_ROUTE_FOR_TIER = {"card": "quick", "deep": "deep"}


def _slots() -> tuple[str, ...]:
    return tuple(sorted(path.stem for path in (_FRAGMENTS / "slots").glob("*.md")))


_FRAGMENT_KINDS = ("foundation", "tier_core", "slot_block")


@dataclass(frozen=True, slots=True)
class Composition:
    """The git revision each fragment is read from; `None` reads the working tree.

    A single-candidate arm composes one changed fragment against the active ones, for example a
    new foundation over the slot blocks the active versions were composed from, without a
    working-tree checkout.
    """

    arm: str = "working-tree"
    foundation: str | None = None
    tier_core: str | None = None
    slot_block: str | None = None

    def revision(self, kind: str) -> str | None:
        return cast("str | None", getattr(self, kind))


WORKING_TREE = Composition()


def _fragment_path(kind: str, slot: str, tier: str) -> str:
    """The fragment's path under `contracts/prompts/insights/`."""
    return {
        "foundation": "foundation.md",
        "tier_core": f"tiers/{tier}.md",
        "slot_block": f"slots/{slot}.md",
    }[kind]


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=_REPOSITORY, check=True, capture_output=True, text=True
    ).stdout


def _fragment_text(kind: str, slot: str, tier: str, composition: Composition) -> str:
    relative = _fragment_path(kind, slot, tier)
    revision = composition.revision(kind)
    if revision is None:
        return (_FRAGMENTS / relative).read_text()
    return _git("show", f"{revision}:{_FRAGMENTS.relative_to(_REPOSITORY).as_posix()}/{relative}")


def load_composition(path: Path) -> Composition:
    """Read a `--compose` file strictly: `{"arm": str, "revisions": {kind: revision, ...}}`.

    Every revision resolves to a full commit id, so the record names an exact git state.
    """
    document = cast("object", json.loads(path.read_text()))
    if not isinstance(document, dict):
        raise SystemExit("--compose needs exactly the keys arm and revisions.")
    fields = cast("dict[str, object]", document)
    if set(fields) != {"arm", "revisions"}:
        raise SystemExit("--compose needs exactly the keys arm and revisions.")
    arm, named = fields["arm"], fields["revisions"]
    if not isinstance(arm, str) or not arm.strip():
        raise SystemExit("--compose arm must be a non-empty string.")
    if not isinstance(named, dict):
        raise SystemExit(f"--compose revisions must name exactly {list(_FRAGMENT_KINDS)}.")
    revisions = cast("dict[str, object]", named)
    if set(revisions) != set(_FRAGMENT_KINDS):
        raise SystemExit(f"--compose revisions must name exactly {list(_FRAGMENT_KINDS)}.")
    resolved: dict[str, str] = {}
    for kind in _FRAGMENT_KINDS:
        value = revisions[kind]
        if not isinstance(value, str) or not value.strip() or value.startswith("-"):
            raise SystemExit(f"--compose revision for {kind} must be a git revision.")
        resolved[kind] = _git("rev-parse", "--verify", f"{value}^{{commit}}").strip()
    return Composition(arm=arm.strip(), **resolved)


def _composed(slot: str, tier: str, composition: Composition = WORKING_TREE) -> ComposedPrompt:
    return compose_prompt(
        PromptFragmentSet(
            foundation=_fragment_text("foundation", slot, tier, composition),
            tier_core=_fragment_text("tier_core", slot, tier, composition),
            slot_block=_fragment_text("slot_block", slot, tier, composition),
        )
    )


def _origin(raw: str) -> str:
    """Return the validated origin, or stop before any credential is placed in a request."""
    try:
        return require_https(raw.strip().rstrip("/"))
    except InsecureUrlError:
        raise SystemExit(_ENV_HINT) from None


def _refreshed_session(origin: str, refresh: str, csrf: str) -> str:
    with _HTTP.post(
        require_https(f"{origin}/api/auth/refresh"),
        data=b"",
        headers={
            **_HEADERS_BASE,
            "Cookie": f"adl_refresh={refresh}; adl_csrf={csrf}",
            "x-adl-csrf": csrf,
        },
        timeout=30,
        allow_redirects=False,
    ) as response:
        # Any status but 200 (a 3xx included) is refused before a Set-Cookie header is read.
        if response.status_code != _OK:
            raise SystemExit(
                "The refresh route returned no session cookie. Is ADL_REFRESH current?"
            )
        issued = response.raw.headers.getlist("Set-Cookie")  # requests folds repeated headers
    for header in issued:
        if header.startswith("adl_session="):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise SystemExit("The refresh route returned no session cookie. Is ADL_REFRESH current?")


PROBE_MARKER_HEADER = "x-adl-probe-marker"


def probe_marker(raw: str) -> str:
    """Return a canonical UUID for `--probe-marker`, or refuse anything else."""
    try:
        return str(uuid.UUID(raw))
    except ValueError:
        raise argparse.ArgumentTypeError("--probe-marker must be a UUID") from None


SESSION_REUSE_SECONDS = 300
"""Re-mint the session this often: its access token lasts 15 minutes, and a batch that waits on
calibration runs longer than that."""


class Registry:
    def __init__(
        self,
        origin: str,
        session_cookie: str,
        csrf: str,
        marker: str | None = None,
        remint: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._origin = origin
        self._csrf = csrf
        self._cookie = f"adl_session={session_cookie}; adl_csrf={csrf}"
        self._remint = remint
        self._clock = clock
        self._minted_at = clock()
        self._lock = threading.Lock()
        # The live edge proof finds this request's WAF record by the marker (ADR-078 §1).
        self._marker = {} if marker is None else {PROBE_MARKER_HEADER: marker}

    def _session_cookie(self) -> str:
        with self._lock:
            now = self._clock()
            if self._remint is not None and now - self._minted_at >= SESSION_REUSE_SECONDS:
                self._cookie = f"adl_session={self._remint()}; adl_csrf={self._csrf}"
                self._minted_at = now
            return self._cookie

    def post(self, route: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        with _HTTP.post(
            require_https(f"{self._origin}/api/prompt-registry/{route}"),
            data=json.dumps(body).encode(),
            headers={
                **_HEADERS_BASE,
                **self._marker,
                "Cookie": self._session_cookie(),
                "x-adl-csrf": self._csrf,
            },
            timeout=60,
            allow_redirects=False,
        ) as response:
            payload = response.content
        try:
            decoded = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            decoded = {"raw_length": len(payload)}
        return response.status_code, decoded


def _version_record(registry: Registry, slot: str, version: object) -> dict[str, Any] | None:
    """The `versions` read-back of one created version, or None when absent or refused."""
    status, listed = registry.post("versions", {"slot_id": slot})
    if status != _OK:
        return None
    return next(
        (
            item
            for item in listed.get("versions", [])
            if isinstance(item, dict) and item.get("version") == version
        ),
        None,
    )


def _active_version(registry: Registry, slot: str, tier: str) -> object:
    """The slot's active version for the tier at this moment: the rollback target."""
    status, roster = registry.post("slots", {})
    if status != _OK:
        return "unread"
    state = next((item for item in roster.get("slots", []) if item.get("slot_id") == slot), {})
    return state.get("active_version" if tier == "card" else "active_deep_version")


def _ship_one(
    registry: Registry,
    slot: str,
    tier: str,
    *,
    activate: bool,
    max_retries: int,
    composition: Composition = WORKING_TREE,
    record: Callable[[dict[str, object]], None] | None = None,
) -> tuple[bool, str]:
    composed = _composed(slot, tier, composition)
    digests = composed.digests
    sent_digests = {
        "foundation": digests.foundation,
        "tier_core": digests.tier_core,
        "slot_block": digests.slot_block,
    }
    body: dict[str, object] = {
        "slot_id": slot,
        "prompt_template": composed.text,
        "model_routing_hint": _MODEL_ROUTE_FOR_TIER[tier],
        "tier": tier,
        "fragment_digests": sent_digests,
    }
    entry: dict[str, object] = {
        "arm": composition.arm,
        "slot": slot,
        "tier": tier,
        "composition": {
            kind: {
                "path": _fragment_path(kind, slot, tier),
                "revision": composition.revision(kind) or "working-tree",
                "digest": sent_digests[kind],
            }
            for kind in _FRAGMENT_KINDS
        },
        "combined_digest": digests.combined,
        "body_bytes": len(json.dumps(body).encode()),
        "active_version_before": _active_version(registry, slot, tier) if record else None,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    ok, line = _create_and_settle(registry, slot, tier, body, entry, max_retries=max_retries)
    if record is not None:
        record(entry)
    if not ok or not activate:
        return ok, line
    return _activate(registry, slot, tier, entry["version"], line)


def _create_and_settle(
    registry: Registry,
    slot: str,
    tier: str,
    body: dict[str, object],
    entry: dict[str, object],
    *,
    max_retries: int,
) -> tuple[bool, str]:
    """Create one version and poll until calibration settles; fill `entry` with what was read."""
    status, created = registry.post("create-version", body)
    entry["create_status"] = status
    if status not in (201, 202):
        entry["outcome"] = "create_refused"
        return False, f"{slot}/{tier}: create refused status={status} keys={sorted(created)}"
    version = created.get("version")
    entry["version"] = version
    outcome = created.get("outcome")
    retries = 0
    prior_delay, delay = 1, 2
    while outcome == "pending" and retries < max_retries:
        retries += 1
        time.sleep(delay)
        found = _version_record(registry, slot, version)
        if found is None:
            entry["outcome"] = "status_read_failed"
            return False, f"{slot}/{tier}: v{version} absent from status read"
        outcome = found.get("calibration")
        prior_delay, delay = delay, min(13, prior_delay + delay)
    # One final read-back: the stored digests and every case outcome, whatever the path above.
    found = _version_record(registry, slot, version) or {}
    entry["outcome"] = found.get("calibration", outcome)
    entry["calibration_cases"] = found.get("calibration_cases", [])
    entry["readback_fragment_digests_match"] = found.get("fragment_digests") == body.get(
        "fragment_digests"
    )
    entry["settled_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    outcome = entry["outcome"]
    combined = cast("str", entry["combined_digest"])
    line = f"{slot}/{tier}: v{version} calibration={outcome} combined={combined[:12]}…"
    if outcome != "passed":
        return False, f"{line} · activate blocked (calibration not passed)"
    return True, line


def _activate(
    registry: Registry, slot: str, tier: str, version: object, line: str
) -> tuple[bool, str]:
    status, roster = registry.post("slots", {})
    if status != 200:
        return False, f"{line} · activate skipped (slots read refused)"
    state = next((item for item in roster.get("slots", []) if item.get("slot_id") == slot), {})
    expected = state.get("active_version" if tier == "card" else "active_deep_version")
    status, activated = registry.post(
        "activate",
        {"slot_id": slot, "version": version, "expected_active_version": expected},
    )
    return status == 200, f"{line} · activate status={status} event={'event' in activated}"


class _Recorder:
    """Appends one JSON line per shipped slot and tier; never prompt text or a cookie."""

    def __init__(self, path: Path, marker: str | None) -> None:
        self._path = path
        self._marker = marker
        self._lock = threading.Lock()

    def __call__(self, entry: dict[str, object]) -> None:
        line = json.dumps({**entry, "probe_marker": self._marker}, sort_keys=True)
        with self._lock, self._path.open("a") as handle:
            handle.write(line + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", action="append", default=[])
    parser.add_argument("--all-slots", action="store_true")
    parser.add_argument("--tier", action="append", choices=("card", "deep"), required=True)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--concurrency", default=2, type=int)
    parser.add_argument("--max-retries", default=26, type=int)
    parser.add_argument(
        "--probe-marker",
        type=probe_marker,
        help="a fresh UUID sent as x-adl-probe-marker, so the edge proof finds the WAF record",
    )
    parser.add_argument(
        "--compose",
        type=Path,
        help='a candidate-set file: {"arm": name, "revisions": {foundation, tier_core, '
        "slot_block}}, each a git revision to read that fragment from (default: working tree)",
    )
    parser.add_argument(
        "--record",
        type=Path,
        help="append one JSON line per slot and tier: arm, composition, body bytes, status, "
        "version, calibration and case outcomes, digest read-back, prior active version",
    )
    args = parser.parse_args()
    composition = WORKING_TREE if args.compose is None else load_composition(args.compose)
    slots = _slots() if args.all_slots else tuple(dict.fromkeys(args.slot))
    if not slots:
        raise SystemExit("Name --slot at least once or pass --all-slots.")
    unknown = set(slots) - set(_slots())
    if unknown:
        raise SystemExit(f"Unknown slots (no fragment file): {sorted(unknown)}")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be positive.")
    if args.max_retries < 1:
        raise SystemExit("--max-retries must be positive.")

    origin = _origin(os.environ.get("ADL_BASE_URL", ""))
    refresh = os.environ.get("ADL_REFRESH", "").strip()
    csrf = os.environ.get("ADL_CSRF", "").strip()
    if not refresh or not csrf:
        raise SystemExit(_ENV_HINT)
    registry = Registry(
        origin,
        _refreshed_session(origin, refresh, csrf),
        csrf,
        args.probe_marker,
        remint=lambda: _refreshed_session(origin, refresh, csrf),
    )
    record = None if args.record is None else _Recorder(args.record, args.probe_marker)

    work = [(slot, tier) for slot in slots for tier in dict.fromkeys(args.tier)]
    print(
        f"shipping {len(work)} composed version(s) of arm {composition.arm} "
        f"with concurrency {args.concurrency}"
    )
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        results = list(
            executor.map(
                lambda pair: _ship_one(
                    registry,
                    pair[0],
                    pair[1],
                    activate=args.activate,
                    max_retries=args.max_retries,
                    composition=composition,
                    record=record,
                ),
                work,
            )
        )
    failures = 0
    for ok, line in results:
        print(f"  {line}")
        if not ok:
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
