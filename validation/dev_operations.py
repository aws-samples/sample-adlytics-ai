# SPDX-License-Identifier: MIT-0
"""Read-only state checks for a shared environment, each ending in one exit code.

Subcommands (every target input is required; none is defaulted):

- `state`: every alarm named `adlytics-<env>-…` that is not `OK`, every non-empty failure queue
  (dead-letter and stream-failure queues) of the listed stacks, every listed stack's status and
  the deploy lock. Anything not `OK` or non-empty that `--dispositions` does not explain in its
  current state, and a stale lock (its owner process is gone), is `UNDISPOSED_STATE` (3).
- `alarm-window`: the current state and the `StateUpdate` history since `--since` of every
  `adlytics-<env>-` alarm. A transition into `ALARM` of an alarm not in `--expected`, or such an
  alarm in `ALARM` now even with no transition since `--since`, is `UNEXPECTED_ALARM` (3); an
  expected alarm still in `ALARM` more than `--recover-minutes` after it entered it, including
  one already in `ALARM` before `--since`, is `EXPECTED_ALARM_NOT_RECOVERED` (4).
  `--dispositions` (the `state` file) contributes only its owned entries: each such alarm is
  recorded in `out_of_scope` with its actual state and stops the window only outside its
  `allowed_states`.
- `source-key`: records every listed stack's synthesized function keys from `infra/cdk.out` and
  compares them with the deployed processed templates. Anything but one source key across the set,
  every stack equal and `*_COMPLETE`, is `NOT_ONE_SOURCE_KEY` (3).
- `work-queues`: the daily ingestion work queues of the listed stacks. A visible, in-flight or
  delayed message is `WORK_IN_FLIGHT` (82). `scripts/diff-then-deploy.sh` runs it before a deploy.
- `serving-refusals` (D2 ops leg O3, ADR-139): a census of every roster row's `CURRENT` pointer
  and, for each grant-bearing one, its `SERVING#<grant>/BINDING` under the one serving predicate;
  the `PresentationServingRefused` sums since `--since` (the `past_settle` sum must be 0; the
  `within_settle` sum is recorded, a defect signal not paged); both new alarms not in `ALARM`; and
  one diagnostic Logs Insights count of `operation refused` lines. `PASS` (0) only when every
  grant-bearing `CURRENT` is served, `past_settle` is 0 and neither alarm is in `ALARM`; otherwise
  `FAIL` (3). Any AWS read failure is `NOT_PROVEN` (2) with its cause.
- `lock-targets`: local only. Turns the app's `cdk list --long --json` into the deploy lock's
  targets file (`scripts/deploy-locked.sh --targets-file`): one `<stack> <region>` line per app
  stack in its own Region, then the CDK bootstrap stack in each of those Regions. A stack for
  another account or without a concrete Region is refused (2).

Every AWS read names an exact stack, a queue that stack owns, or the `adlytics-<env>-` alarm
prefix; nothing lists the account. `--cdk-list` holds the app's own `cdk list --long --json`;
each stack, and the queues it owns, is read in that stack's own Region (the edge stack is not in
the application Region), and alarms in `--region`, each through an identity-checked session.
Each run prints one `RESULT {json}` line and writes the same record, stamped with HEAD, tree,
branch and porcelain as `scripts/gate-log.sh` stamps a gate log, to
`<evidence-dir>/<subcommand>-<utc>.json`.

Exit codes: 0 clean; 2 `USAGE`, `READ_FAILED` or `NOT_PROVEN` (a refused input, a failed AWS or
git read, an identity mismatch, an unwritable record); 3 and 4 as above; 82 `WORK_IN_FLIGHT`.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, NoReturn, cast

_ROOT: Final = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters.outbound.cloudformation_stack import stack_resource_summaries  # noqa: E402
from domain.operations.generated_execution_graphs import BAKE_TASKS  # noqa: E402
from validation.probe_ingestion_traceability_gate import (  # noqa: E402
    deployed_asset_readback,
    record_assets,
)

OK: Final = 0
USAGE: Final = 2
READ_FAILED: Final = 2
UNDISPOSED_STATE: Final = 3
UNEXPECTED_ALARM: Final = 3
NOT_ONE_SOURCE_KEY: Final = 3
EXPECTED_ALARM_NOT_RECOVERED: Final = 4
SERVING_NOT_PROVEN: Final = 2
SERVING_REFUSALS_FAIL: Final = 3
WORK_IN_FLIGHT: Final = 82
_NAMES: Final = {
    ("serving-refusals", 2): "NOT_PROVEN",
    ("serving-refusals", 3): "FAIL",
    ("state", 3): "UNDISPOSED_STATE",
    ("alarm-window", 3): "UNEXPECTED_ALARM",
    ("alarm-window", 4): "EXPECTED_ALARM_NOT_RECOVERED",
    ("source-key", 3): "NOT_ONE_SOURCE_KEY",
    ("work-queues", 82): "WORK_IN_FLIGHT",
}
DAILY_WORK_QUEUES: Final = ("ingestion", "semantic-write.fifo", "preset-bake.fifo")
"""The daily ingestion work queues, after `adlytics-<env>-`; no deploy while one holds work."""
_FAILURE_QUEUE_SUFFIXES: Final = ("-dlq", "Dlq", "-failures")
_COMPLETE: Final = frozenset({"CREATE_COMPLETE", "UPDATE_COMPLETE", "IMPORT_COMPLETE"})
_ACCOUNT: Final = re.compile(r"\d{12}")
_STACK_NAME: Final = re.compile(r"[A-Za-z][A-Za-z0-9-]*")
_REGION: Final = re.compile(r"[a-z]{2,}(?:-[a-z]+)+-\d+")
BOOTSTRAP_STACK: Final = "CDKToolkit"
"""The CDK bootstrap stack `scripts/deploy-all.sh` may create or update in a target Region."""
_QUEUE_DEPTHS: Final = ("visible", "in_flight", "delayed")
_QUEUE_ATTRIBUTES: Final = (
    "ApproximateNumberOfMessages",
    "ApproximateNumberOfMessagesNotVisible",
    "ApproximateNumberOfMessagesDelayed",
)


class OperationsError(RuntimeError):
    """A refused input or a failed read: always exit 2, with its name and message in the record."""

    def __init__(self, message: str, name: str = "USAGE") -> None:
        """Carry the record name: `USAGE`, `READ_FAILED` or `IDENTITY_MISMATCH`."""
        super().__init__(message)
        self.name = name


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise OperationsError(f"invalid arguments: {message}")


# --- Pure classifiers -------------------------------------------------------------------------


def is_failure_queue(name: str) -> bool:
    """Whether a queue holds failures (a dead-letter or stream-failure queue) rather than work."""
    return name.removesuffix(".fifo").endswith(_FAILURE_QUEUE_SUFFIXES)


def _depth(queue: Mapping[str, object]) -> int:
    return sum(int(cast("int", queue[key])) for key in _QUEUE_DEPTHS)


def queue_state(queue: Mapping[str, object]) -> str:
    """Render a failure queue's exact depth, the state a queue disposition lists."""
    return " ".join(f"{key}={int(cast('int', queue[key]))}" for key in _QUEUE_DEPTHS)


@dataclass(frozen=True, slots=True)
class Disposition:
    """Why a named alarm or failure queue may stand, and the exact states in which it may.

    An alarm state is `OK`, `ALARM` or `INSUFFICIENT_DATA`; a failure queue state is its
    `queue_state`, for example `visible=1 in_flight=0 delayed=0`. An `owner` marks an alarm as out
    of this check's scope and owned elsewhere: it is recorded in every read, with its actual state,
    in `out_of_scope`, never silently `OK`, and still refused in a state it does not list.
    """

    allowed_states: tuple[str, ...]
    reason: str
    owner: str | None = None

    @property
    def projection(self) -> dict[str, object]:
        """The fields a state record carries for this disposition."""
        projected: dict[str, object] = {
            "disposition": self.reason,
            "allowed_states": list(self.allowed_states),
        }
        if self.owner is not None:
            projected["owner"] = self.owner
        return projected


def owned(dispositions: Mapping[str, Disposition]) -> dict[str, Disposition]:
    """Select the dispositions that place an alarm out of scope, owned elsewhere."""
    return {name: item for name, item in dispositions.items() if item.owner is not None}


def _scoped_record(alarm: Mapping[str, str], disposition: Disposition) -> dict[str, object]:
    return {
        "alarm": alarm["name"],
        "state": alarm["state"],
        "transitioned": alarm.get("transitioned", alarm.get("updated", "")),
        "conforms": alarm["state"] in disposition.allowed_states,
        **disposition.projection,
    }


def _entered_alarm(alarm: Mapping[str, str]) -> datetime:
    """When an alarm now in `ALARM` entered it; a read without that time cannot be classified."""
    if not alarm.get("transitioned"):
        raise OperationsError(
            f"alarm {alarm['name']} is in ALARM but the read has no StateTransitionedTimestamp",
            "READ_FAILED",
        )
    return datetime.fromisoformat(alarm["transitioned"]).astimezone(UTC)


def check_alarm_expectations(alarms: Sequence[Mapping[str, str]], expected: frozenset[str]) -> None:
    """Refuse an expectation the current alarm states cannot classify, before any history read.

    `USAGE` when `expected` names an alarm `alarms` does not list; `READ_FAILED` when an expected
    alarm in `ALARM` carries no transition time.
    """
    current = {alarm["name"]: alarm for alarm in alarms}
    unknown = sorted(expected - current.keys())
    if unknown:
        raise OperationsError(f"--expected names alarms the prefix does not list: {unknown}")
    for name in sorted(expected):
        if current[name]["state"] == "ALARM":
            _entered_alarm(current[name])


def check_out_of_scope(
    alarms: Sequence[Mapping[str, str]],
    expected: frozenset[str],
    out_of_scope: Mapping[str, Disposition],
) -> None:
    """Refuse an out-of-scope alarm the prefix does not list, or one also `--expected` (`USAGE`)."""
    names = {alarm["name"] for alarm in alarms}
    unknown = sorted(set(out_of_scope) - names)
    if unknown:
        raise OperationsError(f"out-of-scope dispositions name unlisted alarms: {unknown}")
    both = sorted(set(out_of_scope) & expected)
    if both:
        raise OperationsError(f"an alarm cannot be both expected and out of scope: {both}")


def classify_alarm_window(  # noqa: PLR0917 -- the plan's positional classifier inputs
    history_rows: Sequence[Mapping[str, str]],
    alarms: Sequence[Mapping[str, str]],
    expected: frozenset[str],
    recover_minutes: int,
    now: datetime,
    out_of_scope: Mapping[str, Disposition] | None = None,
) -> tuple[int, dict[str, object]]:
    """Classify an alarm window's `StateUpdate` rows (`timestamp`, `alarm`, `summary`).

    `alarms` is every prefixed alarm's current state (`name`, `state`, `transitioned`, the time its
    state last changed), which `expected` must be drawn from. 3 on any transition into `ALARM` of
    an alarm outside `expected`, and on any alarm outside `expected` in `ALARM` now: one that
    entered `ALARM` before the window and never left it has no row in it, but it is no cleaner
    for that (`standing_unexpected`); otherwise 4 when an expected alarm is in `ALARM` now and
    entered it at `transitioned`, more than `recover_minutes` before `now`. The current state
    decides, not the history: an alarm `OK` now has recovered even when its last visible row
    enters `ALARM` (history can lag or stop short of the recovery). `transitioned` can precede the
    window, so an expected alarm already in `ALARM` when the window starts is still bounded.

    `out_of_scope` holds owned dispositions (`owned`). Each such alarm is recorded under
    `out_of_scope` with its actual state, and its transitions and current state do not stop the
    window while they stay in its `allowed_states`; outside them it is unexpected like any other.
    """
    scoped = owned(out_of_scope or {})
    check_alarm_expectations(alarms, expected)
    check_out_of_scope(alarms, expected, scoped)
    current = {alarm["name"]: alarm for alarm in alarms}

    def tolerated(name: str, state: str) -> bool:
        return name in scoped and state in scoped[name].allowed_states

    rows = sorted(
        (datetime.fromisoformat(row["timestamp"]).astimezone(UTC), row["alarm"], row["summary"])
        for row in history_rows
    )
    unexpected = [
        row
        for row in rows
        if row[2].endswith("to ALARM") and row[1] not in expected and not tolerated(row[1], "ALARM")
    ]
    standing = sorted(
        name
        for name, alarm in current.items()
        if name not in expected
        and (
            (alarm["state"] == "ALARM" and not tolerated(name, "ALARM"))
            or (name in scoped and not tolerated(name, alarm["state"]))
        )
    )
    bound = timedelta(minutes=recover_minutes)
    not_recovered: list[str] = []
    expected_now: list[dict[str, str]] = []
    for name in sorted(expected):
        in_alarm = current[name]["state"] == "ALARM"
        entered = _entered_alarm(current[name]) if in_alarm else None
        expected_now.append(
            {
                "alarm": name,
                "state": current[name]["state"],
                "entered": entered.strftime("%Y-%m-%dT%H:%M:%SZ") if entered else "",
            }
        )
        if entered is not None and now - entered > bound:
            not_recovered.append(name)
    code = (
        UNEXPECTED_ALARM
        if unexpected or standing
        else EXPECTED_ALARM_NOT_RECOVERED
        if not_recovered
        else OK
    )
    return code, {
        "transitions": [
            {
                "timestamp": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "alarm": alarm,
                "summary": summary,
                "unexpected": (at, alarm, summary) in unexpected,
            }
            for at, alarm, summary in rows
        ],
        "expected": sorted(expected),
        "expected_now": expected_now,
        "unexpected_alarm": sorted({row[1] for row in unexpected} | set(standing)),
        "standing_unexpected": [
            {
                "alarm": name,
                "state": current[name]["state"],
                "transitioned": current[name].get("transitioned", ""),
            }
            for name in standing
        ],
        "out_of_scope": [
            {
                **_scoped_record(current[name], scoped[name]),
                "window_transitions": sum(1 for row in rows if row[1] == name),
            }
            for name in sorted(scoped)
        ],
        "not_recovered": not_recovered,
        "recover_minutes": recover_minutes,
        "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def classify_state(  # noqa: PLR0917 -- the plan's positional classifier inputs
    alarms: Sequence[Mapping[str, str]],
    queues: Sequence[Mapping[str, object]],
    stacks: Sequence[Mapping[str, str]],
    lock: Mapping[str, object],
    dispositions: Mapping[str, Disposition],
) -> tuple[int, dict[str, object]]:
    """Classify one environment read: 3 when anything not OK or non-empty is not disposed.

    `alarms` carry `name`, `state` and `updated`; `queues` carry `name`, `stack`, `visible`,
    `in_flight` and `delayed`; `stacks` carry `name` and `status` (`ABSENT` when not deployed);
    `lock` is `read_lock`'s report. A stale lock is never disposed: it needs the force-release.
    An alarm is disposed only in a listed alarm state, and a failure queue only at a listed
    `queue_state`; the same name in any other state is undisposed with `refused_disposition`. An
    alarm with an owned disposition is recorded in `out_of_scope` in every state, `OK` included,
    and is undisposed in a state its disposition does not list.
    """
    undisposed: list[dict[str, object]] = []
    disposed: list[dict[str, object]] = []
    refused: set[str] = set()

    def file(entry: dict[str, object], state: str) -> None:
        # A disposition explains a name only in the states it lists; any other state of the
        # same name is a change the disposition never accounted for, so it stops the read.
        name = str(entry["name"])
        disposition = dispositions.get(name)
        if disposition is None:
            undisposed.append(entry)
        elif state in disposition.allowed_states:
            disposed.append({**entry, **disposition.projection})
        else:
            refused.add(name)
            undisposed.append({**entry, "refused_disposition": disposition.projection})

    out_of_scope: list[dict[str, object]] = []
    for alarm in sorted(alarms, key=lambda item: item["name"]):
        scoped = dispositions.get(alarm["name"])
        if scoped is not None and scoped.owner is not None:
            record = _scoped_record(alarm, scoped)
            out_of_scope.append(record)
            if not record["conforms"]:
                refused.add(alarm["name"])
                undisposed.append(
                    {
                        "kind": "alarm",
                        "name": alarm["name"],
                        "state": alarm["state"],
                        "since": alarm.get("updated", ""),
                        "refused_disposition": scoped.projection,
                    }
                )
            continue
        if alarm["state"] != "OK":
            file(
                {
                    "kind": "alarm",
                    "name": alarm["name"],
                    "state": alarm["state"],
                    "since": alarm.get("updated", ""),
                },
                alarm["state"],
            )
    for queue in sorted(queues, key=lambda item: str(item["name"])):
        if is_failure_queue(str(queue["name"])) and _depth(queue) > 0:
            file({"kind": "failure_queue", **queue}, queue_state(queue))
    if lock.get("present") and not lock.get("owner_alive"):
        undisposed.append(
            {
                "kind": "stale_lock",
                "name": str(lock.get("path", "")),
                "pid": lock.get("pid"),
                "content": lock.get("content", ""),
                "remedy": "make deploy-lock-force-release with OWNER_PID set to this pid "
                "and a REASON, after confirming no deploy of this lock is still running, with "
                "the account, environment, Region and targets the lock records",
            }
        )
    used = {str(entry["name"]) for entry in disposed} | {
        str(entry["alarm"]) for entry in out_of_scope
    }
    report: dict[str, object] = {
        "undisposed": undisposed,
        "disposed": disposed,
        "out_of_scope": out_of_scope,
        "unused_dispositions": sorted(set(dispositions) - used - refused),
        "work_queues": [
            dict(queue) for queue in queues if not is_failure_queue(str(queue["name"]))
        ],
        "stacks": [dict(stack) for stack in stacks],
        "stacks_in_progress": sorted(
            stack["name"] for stack in stacks if stack["status"].endswith("_IN_PROGRESS")
        ),
        "lock": dict(lock),
    }
    return (UNDISPOSED_STATE if undisposed else OK), report


def classify_source_key(readback: Mapping[str, object]) -> tuple[int, dict[str, object]]:
    """Pass only one source key across the set, with every stack equal and `*_COMPLETE`.

    `readback` is `deployed_asset_readback`'s result.
    """
    keys = sorted(cast("Sequence[str]", readback.get("source_keys", [])))
    stacks = cast("Mapping[str, Mapping[str, object]]", readback.get("stacks", {}))
    unequal = sorted(name for name, entry in stacks.items() if not entry.get("equal"))
    incomplete = sorted(
        name
        for name, entry in stacks.items()
        if str(entry.get("stack_status", "")) not in _COMPLETE
    )
    passed = len(keys) == 1 and not unequal and not incomplete and bool(readback.get("passed"))
    return (OK if passed else NOT_ONE_SOURCE_KEY), {
        "source_keys": keys,
        "unequal_stacks": unequal,
        "incomplete_stacks": incomplete,
        "readback": dict(readback),
    }


def classify_work_queues(
    queues: Sequence[Mapping[str, object]], environment: str
) -> tuple[int, dict[str, object]]:
    """82 when a daily work queue holds work; 2 when one is not exactly one queue of the stacks."""
    found: list[dict[str, object]] = []
    missing: list[str] = []
    for suffix in DAILY_WORK_QUEUES:
        name = f"adlytics-{environment}-{suffix}"
        matches = [queue for queue in queues if queue["name"] == name]
        if len(matches) != 1:
            missing.append(f"{name} (found {len(matches)})")
            continue
        found.append(dict(matches[0]))
    busy = sorted(str(queue["name"]) for queue in found if _depth(queue) > 0)
    code = READ_FAILED if missing else WORK_IN_FLIGHT if busy else OK
    return code, {"work_queues": found, "not_found": missing, "in_flight": busy}


def lock_targets(listing: object, environment: str, account: str) -> list[tuple[str, str]]:
    """Return the exact `(stack, region)` set a deploy lock covers, from `cdk list --long --json`.

    Every app stack in its own Region (`app_stacks`), then the bootstrap stack in each of those
    Regions, because a deploy may bootstrap there.
    """
    targets = app_stacks(listing, environment, account)
    regions = list(dict.fromkeys(region for _, region in targets))
    return [*targets, *((BOOTSTRAP_STACK, region) for region in regions)]


def app_stacks(listing: object, environment: str, account: str) -> list[tuple[str, str]]:
    """Return every app stack with its own Region, from `cdk list --long --json`.

    The edge stack is not in the application Region, so each stack is read where it is. A stack
    that is not this environment's, synthesizes for another account or has no concrete Region is
    refused: it could not be read where it is.
    """
    if not isinstance(listing, list) or not listing:
        raise OperationsError("the cdk list --long --json output is not a non-empty list")
    targets: list[tuple[str, str]] = []
    for entry in cast("list[object]", listing):
        if not isinstance(entry, dict):
            raise OperationsError(f"a cdk list entry is not an object: {entry!r}")
        fields = cast("dict[str, object]", entry)
        name = fields.get("name")
        where = fields.get("environment")
        if not isinstance(name, str) or not isinstance(where, dict):
            raise OperationsError(f"a cdk list entry has no name and environment: {entry!r}")
        place = cast("dict[str, object]", where)
        if not _STACK_NAME.fullmatch(name) or not name.endswith(f"-{environment}"):
            raise OperationsError(f"cdk list names a stack that is not -{environment}: {name!r}")
        if place.get("account") != account:
            raise OperationsError(
                f"{name} synthesizes for account {place.get('account')!r}, not --expected-account"
            )
        region = place.get("region")
        if not isinstance(region, str) or _REGION.fullmatch(region) is None:
            raise OperationsError(f"{name} has no concrete Region: {region!r}")
        if (name, region) in targets:
            raise OperationsError(f"cdk list names {name} in {region} twice")
        targets.append((name, region))
    return targets


# --- Local reads ------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Another user's live process: it exists, so the lock is not stale.
        return True
    return True


def read_lock(path: Path) -> dict[str, object]:
    """Report the lock file: absent, held by a live pid, or stale (pid gone or unnamed)."""
    if not path.exists():
        return {"path": str(path), "present": False}
    try:
        content = path.read_text(errors="replace").strip()
    except OSError as error:
        raise OperationsError(f"lock file unreadable: {path}: {error}", "READ_FAILED") from error
    match = re.match(r"pid=(\d+)(?:\s|$)", content)
    pid = int(match.group(1)) if match else None
    return {
        "path": str(path),
        "present": True,
        "content": content,
        "pid": pid,
        "owner_alive": _pid_alive(pid) if pid is not None else False,
    }


def read_dispositions(path: Path | None) -> dict[str, Disposition]:
    """Load the optional `{name: {"allowed_states": [...], "reason": ..., ["owner": ...]}}` file.

    Any other shape is refused, including the earlier `{name: sentence}` form: a sentence names
    no state, so it would dispose of the name in every state.
    """
    if path is None:
        return {}
    try:
        loaded: object = json.loads(path.read_text())
    except (OSError, ValueError) as error:
        raise OperationsError(f"dispositions unreadable: {path}: {error}", "READ_FAILED") from error
    if not isinstance(loaded, dict):
        raise OperationsError("dispositions must be a JSON object of name to disposition")
    result: dict[str, Disposition] = {}
    for key, value in cast("dict[object, object]", loaded).items():
        if not isinstance(key, str) or not isinstance(value, dict):
            raise OperationsError(
                f"disposition for {key!r} must be an object with allowed_states and reason"
            )
        entry = cast("dict[object, object]", value)
        if set(entry) - {"owner"} != {"allowed_states", "reason"}:
            raise OperationsError(
                f"disposition for {key!r} must carry allowed_states and reason, and only an owner"
            )
        owner = entry.get("owner")
        if owner is not None and (not isinstance(owner, str) or not owner.strip()):
            raise OperationsError(f"disposition for {key!r} needs a non-empty owner when named")
        reason, listed = entry["reason"], entry["allowed_states"]
        if not isinstance(reason, str) or not reason.strip():
            raise OperationsError(f"disposition for {key!r} needs a non-empty reason")
        states = cast("list[object]", listed) if isinstance(listed, list) else []
        allowed = tuple(state for state in states if isinstance(state, str) and state.strip())
        if not allowed or len(allowed) != len(states) or len(set(allowed)) != len(allowed):
            raise OperationsError(
                f"disposition for {key!r} needs a non-empty list of distinct allowed_states"
            )
        result[key] = Disposition(allowed, reason, owner)
    return result


def load_cdk_list(path: Path) -> object:
    """Load a saved `cdk list --long --json` output; an unreadable one is `READ_FAILED`."""
    try:
        return cast("object", json.loads(path.read_text()))
    except (OSError, ValueError) as error:
        raise OperationsError(
            f"cdk list output unreadable: {path}: {error}", "READ_FAILED"
        ) from error


def read_stacks(path: Path, environment: str, account: str) -> list[tuple[str, str]]:
    """Read the app's stacks, each with its own Region, from a `cdk list --long --json` file."""
    return app_stacks(load_cdk_list(path), environment, account)


def _git(*args: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed git argv
        ["git", "-C", str(_ROOT), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OperationsError(
            f"git {' '.join(args)} failed: {completed.stderr.strip()}", "READ_FAILED"
        )
    return completed.stdout


def stamp() -> dict[str, object]:
    """HEAD, tree, branch and porcelain, the fields a gate-log header records."""
    branch = subprocess.run(  # noqa: S603 -- fixed git argv
        ["git", "-C", str(_ROOT), "symbolic-ref", "--short", "-q", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    status = _git("status", "--porcelain", "--untracked-files=all")
    porcelain = [line for line in status.splitlines() if line]
    return {
        "head": _git("rev-parse", "--verify", "HEAD").strip(),
        "tree": _git("rev-parse", "--verify", "HEAD^{tree}").strip(),
        "branch": branch or "(detached)",
        "porcelain_count": len(porcelain),
        "porcelain": porcelain,
    }


SERVING_REFUSAL_METRIC: Final = "PresentationServingRefused"
SERVING_REFUSAL_REASONS: Final = ("ungranted_generation", "serving_refresh_pending")
SERVING_REFUSAL_SETTLES: Final = ("within_settle", "past_settle")
# The refusal alarm's period, one bake worker schedule (`serving-refusal-alarm.ts`).
SERVING_REFUSAL_PERIOD_SECONDS: Final = int(cast("int", BAKE_TASKS["worker_schedule_seconds"]))
# `GetMetricData` runs a Metrics Insights query over the most recent two weeks of data (AWS
# CloudWatch API Reference, GetMetricData, read 2026-10-16); a longer O3 window cannot be read.
METRICS_INSIGHTS_QUERY_RANGE: Final = timedelta(days=14)


def serving_refusal_query(settle: str) -> str:
    """The refusal alarm's Metrics Insights query for one settle class.

    For `past_settle` this is exactly the alarm's `SERVING_REFUSAL_QUERY`
    (`infra/lib/constructs/observability/serving-refusal-alarm.ts`), equality-tested against it.
    """
    if settle not in SERVING_REFUSAL_SETTLES:
        raise ValueError(f"settle class {settle!r} is not one of {SERVING_REFUSAL_SETTLES}")
    return (
        f'SELECT SUM({SERVING_REFUSAL_METRIC}) FROM SCHEMA("Adlytics", Reason, Settle) '  # nosec B608 # A-SQL-VALIDATED-LITERAL-001
        f"WHERE Settle = '{settle}'"
    )


def serving_alarm_names(environment: str) -> tuple[str, str]:
    """The two ADR-139 alarms O3 reads: the presentation refusal and the withheld cohort."""
    return (
        f"adlytics-{environment}-presentation-serving-refused",
        f"adlytics-{environment}-graph-serving-withheld",
    )


def serving_census_row(page: str, variant: str, pointer: Any, binding: Any) -> dict[str, object]:
    """Classify one `CURRENT` pointer under the one serving predicate (ADR-139).

    A grant-less pointer is legacy history and not part of the census verdict. A grant-bearing
    pointer is served only when `granted_generation` (whose direct-binding test is
    `binding_authorizes_serving`) accepts its binding; the row lists the binding's failure entries.
    """
    from domain.operations.execution_graph_release import (
        UngrantedGenerationError,
        binding_authorizes_serving,
        failure_entries,
    )
    from domain.presentation.graph_serving import RefreshPendingError, granted_generation

    row: dict[str, object] = {"key": f"{page}#{variant}"}
    if pointer is None:
        return {**row, "pointer": "absent"}
    if pointer.grant_id is None:
        return {**row, "pointer": "legacy"}
    row |= {"pointer": "grant_bearing", "grant_id": pointer.grant_id}
    if binding is None:
        return {**row, "served": False, "reason": "serving_refresh_pending"}
    row |= {
        "binding_authorizes_serving": binding_authorizes_serving(binding),
        "failure_entries": [list(entry) for entry in failure_entries(binding)],
    }
    try:
        granted_generation(pointer, binding)
    except UngrantedGenerationError:
        return {**row, "served": False, "reason": "ungranted_generation"}
    except RefreshPendingError:
        return {**row, "served": False, "reason": "serving_refresh_pending"}
    return {**row, "served": True}


def classify_serving_refusals(  # noqa: PLR0917 -- the four O3 steps' read results
    census: Sequence[Mapping[str, object]],
    sums: Mapping[str, float],
    alarms: Mapping[str, str | None],
    logs: Sequence[Mapping[str, str]],
) -> tuple[int, dict[str, object]]:
    """O3: `PASS` only when every grant-bearing `CURRENT` is served, no refusal is past settle
    and neither alarm is in `ALARM`; else `FAIL`. An alarm that does not exist is `NOT_PROVEN`.
    """
    unserved = [
        row for row in census if row.get("pointer") == "grant_bearing" and not row["served"]
    ]
    missing = sorted(name for name, state in alarms.items() if state is None)
    in_alarm = sorted(name for name, state in alarms.items() if state == "ALARM")
    past = float(sums.get("past_settle", 0.0))
    report: dict[str, object] = {
        "census": list(census),
        "unserved": [row["key"] for row in unserved],
        "past_settle_sum": past,
        "within_settle_sum": float(sums.get("within_settle", 0.0)),
        "alarms": dict(alarms),
        "logs": list(logs),
    }
    if missing:
        return SERVING_NOT_PROVEN, {**report, "verdict": "NOT_PROVEN", "cause": "alarm_absent"}
    failed = bool(unserved) or past > 0 or bool(in_alarm)
    if failed:
        return SERVING_REFUSALS_FAIL, {**report, "verdict": "FAIL", "in_alarm": in_alarm}
    return OK, {**report, "verdict": "PASS"}


# --- AWS reads --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    """Every explicit target input; none has a default."""

    profile: str
    environment: str
    region: str
    expected_account: str


def _session(target: Target, region: str) -> Any:
    import boto3

    session = boto3.Session(profile_name=target.profile, region_name=region)
    account = str(session.client("sts").get_caller_identity().get("Account", ""))
    if account != target.expected_account:
        raise OperationsError(
            f"the profile's account {account} is not --expected-account", "IDENTITY_MISMATCH"
        )
    return session


def _sessions(target: Target, stacks: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """One identity-checked session per Region: the application Region and each stack's own."""
    regions = dict.fromkeys([target.region, *(region for _, region in stacks)])
    return {region: _session(target, region) for region in regions}


def _stack_status(cloudformation: Any, stack: str) -> str:
    from botocore.exceptions import ClientError

    try:
        # aws-pagination: an exact StackName returns one stack.
        stacks = cloudformation.describe_stacks(StackName=stack)["Stacks"]
    except ClientError as error:
        if "does not exist" in str(error):
            return "ABSENT"
        raise
    return str(stacks[0]["StackStatus"])


def _stacks_and_queues(
    sessions: Mapping[str, Any], stacks: Sequence[tuple[str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, object]]]:
    """Read each stack, and the queues it owns, in the stack's own Region."""
    clients: dict[tuple[str, str], Any] = {}

    def client(service: str, region: str) -> Any:
        if (service, region) not in clients:
            clients[service, region] = sessions[region].client(service)
        return clients[service, region]

    statuses: list[dict[str, str]] = []
    queues: list[dict[str, object]] = []
    for stack, region in stacks:
        cloudformation = client("cloudformation", region)
        sqs = client("sqs", region)
        status = _stack_status(cloudformation, stack)
        statuses.append({"name": stack, "region": region, "status": status})
        if status == "ABSENT":
            continue
        for resource in stack_resource_summaries(cloudformation, stack):
            if resource.get("ResourceType") != "AWS::SQS::Queue":
                continue
            url = str(resource.get("PhysicalResourceId", ""))
            if not url.startswith("https://"):
                raise OperationsError(
                    f"{stack} queue {resource.get('LogicalResourceId')} has no queue URL "
                    f"(status {resource.get('ResourceStatus')})",
                    "READ_FAILED",
                )
            attributes = sqs.get_queue_attributes(
                QueueUrl=url, AttributeNames=list(_QUEUE_ATTRIBUTES)
            )["Attributes"]
            queues.append(
                {
                    "name": url.rsplit("/", 1)[-1],
                    "stack": stack,
                    "visible": int(attributes["ApproximateNumberOfMessages"]),
                    "in_flight": int(attributes["ApproximateNumberOfMessagesNotVisible"]),
                    "delayed": int(attributes["ApproximateNumberOfMessagesDelayed"]),
                }
            )
    return statuses, queues


def _alarms(session: Any, environment: str) -> list[dict[str, str]]:
    cloudwatch = session.client("cloudwatch")
    found: list[dict[str, str]] = []
    for page in cloudwatch.get_paginator("describe_alarms").paginate(
        AlarmNamePrefix=f"adlytics-{environment}-", AlarmTypes=["MetricAlarm", "CompositeAlarm"]
    ):
        for alarm in [*page.get("MetricAlarms", []), *page.get("CompositeAlarms", [])]:
            transitioned = alarm.get("StateTransitionedTimestamp")
            found.append(
                {
                    "name": str(alarm["AlarmName"]),
                    "state": str(alarm["StateValue"]),
                    "updated": str(alarm.get("StateUpdatedTimestamp", "")),
                    "transitioned": (
                        cast("datetime", transitioned).isoformat() if transitioned else ""
                    ),
                }
            )
    return found


def _alarm_history(  # noqa: PLR0917 -- one session, names and a window
    session: Any, names: Sequence[str], since: datetime, now: datetime
) -> list[dict[str, str]]:
    cloudwatch = session.client("cloudwatch")
    paginator = cloudwatch.get_paginator("describe_alarm_history")
    rows: list[dict[str, str]] = []
    for name in names:
        for page in paginator.paginate(
            AlarmName=name, HistoryItemType="StateUpdate", StartDate=since, EndDate=now
        ):
            for item in page.get("AlarmHistoryItems", []):
                timestamp = cast("datetime", item["Timestamp"])
                rows.append(
                    {
                        "timestamp": timestamp.isoformat(),
                        "alarm": str(item["AlarmName"]),
                        "summary": str(item["HistorySummary"]),
                    }
                )
    return rows


# --- Subcommands ------------------------------------------------------------------------------


def run_state(target: Target, arguments: argparse.Namespace) -> tuple[int, dict[str, object]]:
    """Read and classify the environment's alarms, failure queues, stacks and lock."""
    stacks = read_stacks(Path(arguments.cdk_list), target.environment, target.expected_account)
    dispositions = read_dispositions(
        Path(arguments.dispositions) if arguments.dispositions else None
    )
    lock = read_lock(Path(arguments.lock_file))
    sessions = _sessions(target, stacks)
    statuses, queues = _stacks_and_queues(sessions, stacks)
    return classify_state(
        _alarms(sessions[target.region], target.environment), queues, statuses, lock, dispositions
    )


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise OperationsError(f"--since is not an ISO 8601 instant: {value!r}") from error
    if parsed.tzinfo is None:
        raise OperationsError("--since must carry a zone, for example 2026-01-01T00:00:00Z")
    return parsed.astimezone(UTC)


def parse_expected(value: str) -> frozenset[str]:
    """`none` is the empty set; otherwise a comma-separated list of alarm names."""
    if value == "none":
        return frozenset()
    names = frozenset(name.strip() for name in value.split(",") if name.strip())
    if not names:
        raise OperationsError("--expected must name alarms, comma-separated, or be `none`")
    return names


def run_alarm_window(
    target: Target, arguments: argparse.Namespace
) -> tuple[int, dict[str, object]]:
    """Read every prefixed alarm's state and its history since `--since`; classify the window."""
    since = _instant(str(arguments.since))
    expected = parse_expected(str(arguments.expected))
    if arguments.recover_minutes < 0:
        raise OperationsError("--recover-minutes must be zero or more")
    now = datetime.now(UTC)
    if since >= now:
        raise OperationsError("--since must be in the past")
    out_of_scope = owned(
        read_dispositions(Path(arguments.dispositions) if arguments.dispositions else None)
    )
    session = _session(target, target.region)
    alarms = _alarms(session, target.environment)
    # Refused from the current states alone: an invalid expectation reads no history.
    check_alarm_expectations(alarms, expected)
    check_out_of_scope(alarms, expected, out_of_scope)
    names = sorted(alarm["name"] for alarm in alarms)
    history = _alarm_history(session, names, since, now)
    code, report = classify_alarm_window(
        history,
        alarms,
        expected,
        int(arguments.recover_minutes),
        now,
        out_of_scope,
    )
    report["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    report["alarms_read"] = len(names)
    return code, report


def run_source_key(target: Target, arguments: argparse.Namespace) -> tuple[int, dict[str, object]]:
    """Record each listed stack's synthesized keys, then read back the deployed templates."""
    stacks = read_stacks(Path(arguments.cdk_list), target.environment, target.expected_account)
    evidence_dir = Path(arguments.evidence_dir)
    head = _git("rev-parse", "--verify", "HEAD").strip()
    utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    files: list[Path] = []
    for stack, _ in stacks:
        recorded = record_assets(_ROOT, stack)
        recorded["sha"] = head
        path = evidence_dir / f"{stack}-assets-{utc}.json"
        with path.open("x") as handle:
            handle.write(json.dumps(recorded, indent=2, sort_keys=True) + "\n")
        files.append(path)
    sessions = _sessions(target, stacks)
    readback = deployed_asset_readback(
        sessions[target.region],
        files,
        stack_clients={
            stack: sessions[region].client("cloudformation") for stack, region in stacks
        },
    )
    readback["assets_files"] = [str(path) for path in files]
    readback["stack_regions"] = dict(stacks)
    return classify_source_key(readback)


def run_work_queues(target: Target, arguments: argparse.Namespace) -> tuple[int, dict[str, object]]:
    """Read the listed stacks' queues and classify the daily work queues."""
    stacks = read_stacks(Path(arguments.cdk_list), target.environment, target.expected_account)
    _, queues = _stacks_and_queues(_sessions(target, stacks), stacks)
    return classify_work_queues(queues, target.environment)


def run_lock_targets(arguments: argparse.Namespace) -> tuple[int, dict[str, object]]:
    """Write the lock's `<stack> <region>` targets file from a `cdk list --long --json` file."""
    environment = str(arguments.env)
    if not environment:
        raise OperationsError("--env is required")
    if _ACCOUNT.fullmatch(str(arguments.expected_account)) is None:
        raise OperationsError("--expected-account must be a 12-digit account id")
    listing = load_cdk_list(Path(arguments.cdk_list))
    targets = lock_targets(listing, environment, str(arguments.expected_account))
    out = Path(arguments.out)
    with out.open("x") as handle:
        handle.write("".join(f"{stack} {region}\n" for stack, region in targets))
    return OK, {"out": str(out), "targets": [f"{stack} {region}" for stack, region in targets]}


def _serving_census(session: Any, environment: str) -> list[dict[str, object]]:
    """O3 step 1: strongly read every roster row's `CURRENT` and each grant's binding."""
    from adapters.outbound.dynamodb.preset_artifacts import DynamoPresetArtifactPointerStore
    from adapters.outbound.dynamodb.presentation_graph import DynamoPresentationGraphReader
    from domain.presentation.preset_artifacts import (
        PresetArtifactPage,
        PresetArtifactPointerRosterRequest,
        PresetArtifactVariant,
    )

    configuration = session.client("lambda").get_function_configuration(
        FunctionName=f"adlytics-{environment}-PresentationFunction"
    )
    variables = configuration.get("Environment", {}).get("Variables", {})
    pointer_table = str(variables.get("ADL_PRESET_ARTIFACT_POINTER_TABLE", ""))
    graph_table = str(variables.get("ADL_EXECUTION_GRAPH_TABLE", ""))
    if not pointer_table or not graph_table:
        raise OperationsError(
            "the Presentation function names no pointer or graph table", "NOT_PROVEN"
        )
    dynamodb = session.client("dynamodb")
    pointers = DynamoPresetArtifactPointerStore(pointer_table, client=dynamodb)
    graph = DynamoPresentationGraphReader(graph_table, client=dynamodb)
    pages: dict[str, list[str]] = {}
    for row in cast("Sequence[Mapping[str, str]]", BAKE_TASKS["roster"]):
        pages.setdefault(str(row["page"]), []).append(str(row["variant"]))
    census: list[dict[str, object]] = []
    for page, variants in pages.items():
        roster = pointers.roster(
            PresetArtifactPointerRosterRequest(
                PresetArtifactPage(page),
                tuple(PresetArtifactVariant(variant) for variant in variants),
            )
        )
        for variant, pointer in zip(variants, roster.pointers, strict=True):
            grant = None if pointer is None else pointer.grant_id
            binding = None if grant is None else graph.serving_binding(grant)
            census.append(serving_census_row(page, variant, pointer, binding))
    return census


def _serving_refusal_sum(session: Any, settle: str, since: datetime, now: datetime) -> float:
    """Sum one settle class's refusals over `[since, now]` from its Metrics Insights query.

    `GetMetricData` takes one Metrics Insights query per call, so each class is one call, on the
    alarm's 60-second period. Every result must say `Complete` itself: a missing status, any
    other status on any page, or a response message is a read that did not cover the window,
    `NOT_PROVEN`, never a silent partial sum. The sum also needs evidence: a result naming any
    `Id` other than the query's `refused`, or a read whose pages carry no `refused` result at all,
    is `NOT_PROVEN`, never a zero. A legitimate read never needs a `PartialData` page:
    the query has no `GROUP BY`, so it is one series, and the window is at most
    `METRICS_INSIGHTS_QUERY_RANGE` at 60 s (20,161 points), under `GetMetricData`'s default
    `MaxDatapoints` of 100,800 (AWS CloudWatch API Reference, GetMetricData, read 2026-10-17).
    """
    total = 0.0
    answered = False
    paginator = session.client("cloudwatch").get_paginator("get_metric_data")
    queries = [
        {
            "Id": "refused",
            "Expression": serving_refusal_query(settle),
            "Period": SERVING_REFUSAL_PERIOD_SECONDS,
            "ReturnData": True,
        }
    ]
    for page in paginator.paginate(MetricDataQueries=queries, StartTime=since, EndTime=now):
        messages = [*page.get("Messages", [])]
        for result in page.get("MetricDataResults", []):
            if result.get("Id") != "refused":
                raise OperationsError(
                    f"the {settle} refusal query returned result id {result.get('Id')}",
                    "NOT_PROVEN",
                )
            answered = True
            messages.extend(result.get("Messages", []))
            status = result.get("StatusCode")
            if status != "Complete":
                raise OperationsError(
                    f"the {settle} refusal query returned status {status}", "NOT_PROVEN"
                )
            total += float(sum(result.get("Values", [])))
        if messages:
            codes = sorted({str(message.get("Code", "")) for message in messages})
            raise OperationsError(
                f"the {settle} refusal query returned messages {codes}", "NOT_PROVEN"
            )
    if not answered:
        raise OperationsError(f"the {settle} refusal query returned no result", "NOT_PROVEN")
    return total


def _serving_refusal_sums(session: Any, since: datetime, now: datetime) -> dict[str, float]:
    """O3 step 2: the refusal metric's sums by `Settle` over `[since, now]`, period 60.

    `past_settle` is read with the alarm's own Metrics Insights query; `within_settle` with the
    same query for its class, recorded and not paged (ADR-139).
    """
    return {
        settle: _serving_refusal_sum(session, settle, since, now)
        for settle in SERVING_REFUSAL_SETTLES
    }


def _serving_alarm_states(session: Any, environment: str) -> dict[str, str | None]:
    """O3 step 3: each new alarm's state, or None when it does not exist."""
    names = serving_alarm_names(environment)
    states: dict[str, str | None] = dict.fromkeys(names)
    paginator = session.client("cloudwatch").get_paginator("describe_alarms")
    for page in paginator.paginate(AlarmNames=list(names), AlarmTypes=["MetricAlarm"]):
        for alarm in page.get("MetricAlarms", []):
            states[str(alarm["AlarmName"])] = str(alarm["StateValue"])
    return states


_REFUSAL_LOG_QUERY: Final = (
    "filter message = 'operation refused' and (error_context.reason = 'ungranted_generation' "
    "or error_context.reason = 'serving_refresh_pending') "
    "| stats count(*) as refusals by operation, error_context.reason | limit 100"
)
_LOG_QUERY_POLLS: Final = 60


def _serving_refusal_logs(
    session: Any, environment: str, since: datetime, now: datetime
) -> list[dict[str, str]]:
    """O3 step 4 (diagnostic): refusals by operation and reason, bounded to 100 rows."""
    import time

    logs = session.client("logs")
    started = logs.start_query(
        logGroupName=f"/aws/lambda/adlytics-{environment}-PresentationFunction",
        startTime=int(since.timestamp()),
        endTime=int(now.timestamp()),
        queryString=_REFUSAL_LOG_QUERY,
        limit=100,
    )
    for _ in range(_LOG_QUERY_POLLS):
        result = logs.get_query_results(queryId=started["queryId"])
        status = str(result.get("status", ""))
        if status == "Complete":
            return [
                {str(field["field"]): str(field["value"]) for field in row}
                for row in result.get("results", [])
            ]
        if status in {"Failed", "Cancelled", "Timeout", "Unknown"}:
            raise OperationsError(f"the refusal log query ended {status}", "NOT_PROVEN")
        time.sleep(1)
    raise OperationsError("the refusal log query did not complete", "NOT_PROVEN")


def run_serving_refusals(
    target: Target, arguments: argparse.Namespace
) -> tuple[int, dict[str, object]]:
    """O3 (ADR-139): census, refusal metric, both alarms and the diagnostic log count."""
    since = _instant(str(arguments.since))
    now = datetime.now(UTC)
    if since >= now:
        raise OperationsError("--since must be in the past")
    if now - since > METRICS_INSIGHTS_QUERY_RANGE:
        raise OperationsError(
            "--since is more than the Metrics Insights two-week query range before now"
        )
    session = _session(target, target.region)
    try:
        census = _serving_census(session, target.environment)
        sums = _serving_refusal_sums(session, since, now)
        alarms = _serving_alarm_states(session, target.environment)
        logs = _serving_refusal_logs(session, target.environment, since, now)
    except Exception as error:  # noqa: BLE001 -- every AWS read failure is NOT_PROVEN, named
        return SERVING_NOT_PROVEN, {
            "verdict": "NOT_PROVEN",
            "cause": f"{type(error).__name__}: {error}",
            "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    code, report = classify_serving_refusals(census, sums, alarms, logs)
    report["since"] = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    return code, report


_RUNNERS: Final = {
    "serving-refusals": run_serving_refusals,
    "state": run_state,
    "alarm-window": run_alarm_window,
    "source-key": run_source_key,
    "work-queues": run_work_queues,
}


def _parser() -> _ArgumentParser:
    parser = _ArgumentParser(description="Read-only shared-environment checks.")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in _RUNNERS:
        command = commands.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--env", required=True)
        command.add_argument("--region", required=True)
        command.add_argument("--expected-account", required=True)
        command.add_argument("--evidence-dir", required=True)
        if name == "alarm-window":
            command.add_argument("--since", required=True)
            command.add_argument("--recover-minutes", type=int, required=True)
            command.add_argument("--expected", required=True)
            command.add_argument("--dispositions")
        elif name == "serving-refusals":
            command.add_argument("--since", required=True)
        else:
            command.add_argument("--cdk-list", required=True)
        if name == "state":
            command.add_argument("--lock-file", required=True)
            command.add_argument("--dispositions")
    targets = commands.add_parser("lock-targets")
    for flag in ("--cdk-list", "--env", "--expected-account", "--out"):
        targets.add_argument(flag, required=True)
    return parser


def _target(arguments: argparse.Namespace) -> Target:
    if str(arguments.profile) in {"", "default"}:
        raise OperationsError("--profile must name an explicit nondefault profile")
    for name in ("env", "region"):
        if not str(getattr(arguments, name)):
            raise OperationsError(f"--{name} is required")
    if _ACCOUNT.fullmatch(str(arguments.expected_account)) is None:
        raise OperationsError("--expected-account must be a 12-digit account id")
    evidence_dir = Path(arguments.evidence_dir)
    if not evidence_dir.is_absolute() or not evidence_dir.is_dir():
        raise OperationsError("--evidence-dir must be an existing absolute directory")
    return Target(
        profile=str(arguments.profile),
        environment=str(arguments.env),
        region=str(arguments.region),
        expected_account=str(arguments.expected_account),
    )


def _write_record(evidence_dir: Path, command: str, record: Mapping[str, object]) -> Path:
    utc = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = evidence_dir / f"{command}-{utc}.json"
    with path.open("x") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True, default=str) + "\n")
    return path


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand, print one RESULT line, and return its exit code."""
    command = "unknown"
    evidence_dir: Path | None = None
    started = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        arguments = _parser().parse_args(argv)
        command = str(arguments.command)
        if command == "lock-targets":
            # Local only: no target session and no evidence record; the targets file is the output.
            code, report = run_lock_targets(arguments)
            written = {"subcommand": command, "code": code, "name": "OK", "report": report}
            print("RESULT " + json.dumps(written, sort_keys=True))
            return code
        target = _target(arguments)
        evidence_dir = Path(arguments.evidence_dir)
        record: dict[str, object] = {"subcommand": command, "started": started, **stamp()}
        code, report = _RUNNERS[command](target, arguments)
        record.update(
            {
                "target": {"env": target.environment, "region": target.region},
                "code": code,
                "name": _NAMES.get((command, code), "OK" if code == OK else "READ_FAILED"),
                "report": report,
            }
        )
    except Exception as error:  # noqa: BLE001 -- every failure becomes an explicit exit-2 record
        # A refused input carries its own name; any other failure (an AWS, git, file or probe
        # error, or a defect here) is READ_FAILED with its type and message, never swallowed.
        record = {
            "subcommand": command,
            "started": started,
            "code": READ_FAILED,
            "name": error.name if isinstance(error, OperationsError) else "READ_FAILED",
            "error": f"{type(error).__name__}: {error}",
        }
    code = int(cast("int", record["code"]))
    if evidence_dir is not None:
        try:
            path = _write_record(evidence_dir, command, record)
        except OSError as error:
            record = {**record, "code": READ_FAILED, "record_error": f"{error}"}
            code = READ_FAILED
        else:
            print(f"evidence: {path}", file=sys.stderr)
    print("RESULT " + json.dumps(record, sort_keys=True, default=str))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
