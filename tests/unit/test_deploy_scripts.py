# SPDX-License-Identifier: MIT-0
"""The shared-environment deploy tools refuse what they must, and the lock releases when it should.

Every AWS, npx and uv call goes to a stub on PATH that records its argv; the real CLIs are never
reachable (the stub directory comes first, AWS_CONFIG_FILE and the credentials file are
/dev/null, and each lock test asserts the stub recorded its reads). Each refusal is paired with
the run it departs from. The lock-release cases are the regression for the stuck release after a
SIGTERM (the release wait read an account-wide listing, discarded its errors and logged nothing
per pass): with every stack terminal the lock goes within one poll after normal completion,
SIGTERM or cancel; while a stack reads UPDATE_IN_PROGRESS it stays (positive control). Stack state
is per Region, so a target outside the application Region (the edge stack, a bootstrap stack) is
read where it is; the command runs in its own process group, so a descendant is stopped and gone
before a terminal poll can release; and a stale lock is force-released only against the targets it
recorded.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from gate_script_support import BASH, SCRIPTS, commit, git, new_repo, run, script

pytestmark = pytest.mark.unit

ACCOUNT = "111111111111"
OTHER_ACCOUNT = "222222222222"
APP_REGION = "test-region-1"
EDGE_REGION = "edge-region-1"
EDGE = ("Adlytics-Edge-test", EDGE_REGION)
TARGETS = (
    ("Adlytics-Alpha-test", APP_REGION),
    ("Adlytics-Beta-test", APP_REGION),
    EDGE,
    ("CDKToolkit", APP_REGION),
    ("CDKToolkit", EDGE_REGION),
)
REPO_ROOT = SCRIPTS.parent
MAKE = "/usr/bin/make"
POLL = 1

_AWS_STUB = r"""#!/bin/bash
# Test stub of the aws CLI. Never reaches AWS. State lives in $STUB_STATE, per Region.
echo "aws $*" >> "$STUB_STATE/calls.log"
# A test seam: on the next call, move the file named in break-log aside and put a directory in its
# place, so every later append to it fails (for any user, root included).
if [ -f "$STUB_STATE/break-log" ]; then
  broken=$(cat "$STUB_STATE/break-log")
  rm -f "$STUB_STATE/break-log"
  mv "$broken" "$broken.before" && mkdir "$broken"
fi
case "$1 $2" in
  "sts get-caller-identity") cat "$STUB_STATE/account" ;;
  "cloudformation describe-stacks")
    name="" region=""
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "--stack-name" ]; then name="$2"; fi
      if [ "$1" = "--region" ]; then region="$2"; fi
      shift
    done
    f="$STUB_STATE/stacks/$region/$name"
    if [ ! -f "$f" ]; then
      echo "An error occurred (ValidationError): Stack with id $name does not exist" >&2
      exit 254
    fi
    s=$(cat "$f")
    if [ "$s" = FAIL ]; then
      echo "Could not connect to the endpoint URL: stubbed failure" >&2
      exit 255
    fi
    echo "$s" ;;
  *) echo "stub: unexpected aws $*" >&2; exit 99 ;;
esac
"""

_RECORDING_STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_STATE/calls.log"
exit 99
"""


class Stub:
    """A stub bin directory and its state: account, per-stack status and the call log."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "stub-bin"
        self.state = root / "stub-state"
        (self.state / "stacks").mkdir(parents=True)
        self.bin.mkdir()
        (self.bin / "aws").write_text(_AWS_STUB)
        for name in ("npx", "uv"):
            (self.bin / name).write_text(_RECORDING_STUB.format(name=name))
        for path in self.bin.iterdir():
            path.chmod(0o755)
        self.account(ACCOUNT)
        for stack, region in TARGETS:
            self.status(stack, "UPDATE_COMPLETE", region)
        # Variables a test adds to every run's environment (a BASH_ENV seam).
        self.extra: dict[str, str] = {}

    def account(self, value: str) -> None:
        (self.state / "account").write_text(value + "\n")

    def status(self, stack: str, value: str, region: str = APP_REGION) -> None:
        (self.state / "stacks" / region).mkdir(parents=True, exist_ok=True)
        (self.state / "stacks" / region / stack).write_text(value + "\n")

    @property
    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def env(self, *, system_path: bool = True) -> dict[str, str]:
        path = f"{self.bin}:/usr/bin:/bin" if system_path else str(self.bin)
        return {
            "PATH": path,
            "HOME": str(self.state),
            "STUB_STATE": str(self.state),
            "AWS_CONFIG_FILE": "/dev/null",
            "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
            "TMPDIR": str(self.state),
            **self.extra,
        }


@pytest.fixture
def stub(tmp_path: Path) -> Stub:
    return Stub(tmp_path)


def _targets_text(targets: tuple[tuple[str, str], ...]) -> str:
    return "".join(f"{stack} {region}\n" for stack, region in targets)


class Lock:
    """One wrapper run's paths: a clean repository, the lock, its log and the targets file."""

    def __init__(self, root: Path) -> None:
        self.repo = new_repo(root)
        self.dir = root / "custody"
        self.dir.mkdir()
        self.lock = self.dir / "deploy.lock"
        self.log = self.dir / "deploy-lock.log"
        self.targets = self.dir / "targets.txt"
        self.targets.write_text(_targets_text(TARGETS))
        self.head = git(self.repo, "rev-parse", "HEAD")

    def args(self, **override: str) -> list[str]:
        values = {
            "--profile": "my-profile",
            "--region": APP_REGION,
            "--expected-account": ACCOUNT,
            "--env": "test",
            "--lock-file": str(self.lock),
            "--log-file": str(self.log),
            "--targets-file": str(self.targets),
            "--source-sha": self.head,
            "--label": "unit",
            "--poll-seconds": str(POLL),
        }
        values.update(override)
        return [item for pair in values.items() for item in pair if pair[1] != ""]

    @property
    def log_text(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def command_groups(self) -> list[int]:
        """Every process group a wrapper of this lock started its command in."""
        return [int(found) for found in re.findall(r"process_group=(\d+)", self.log_text)]


@pytest.fixture
def lock(tmp_path: Path) -> Iterator[Lock]:
    held = Lock(tmp_path)
    yield held
    # The command leads its own process group, outside the wrapper's: stop whatever a failing
    # test or an abandoned wrapper left in it, so no stray process outlives the test.
    for group in set(held.command_groups()):
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError, PermissionError:
            continue


_HANDLED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _operator_signals() -> None:
    # A terminal delivers INT, TERM and HUP to the wrapper. The test runner may start with them
    # blocked or ignored, and bash can neither trap nor reset a signal ignored on entry, so the
    # child starts from the dispositions an operator's shell gives it.
    signal.pthread_sigmask(signal.SIG_UNBLOCK, _HANDLED)
    for handled in _HANDLED:
        signal.signal(handled, signal.SIG_DFL)


def _wrapper(lock: Lock, stub: Stub, command: list[str], **override: str) -> subprocess.Popen[str]:
    # A-SUBPROCESS-001: fixed interpreter and repository script; docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.Popen(  # noqa: S603
        [BASH, str(SCRIPTS / "deploy-locked.sh"), *lock.args(**override), "--", *command],
        cwd=lock.repo,
        env=stub.env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # Its own process group, so the reaper can stop the wrapper and its command together.
        start_new_session=True,
        preexec_fn=_operator_signals,  # noqa: PLW1509 -- signal dispositions only, before exec
    )


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Run a repository script to completion with its output captured; its exit code is returned."""
    # A-SUBPROCESS-001: fixed interpreter and repository script; docs/security-dispositions.md
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit
    return subprocess.run(  # noqa: S603
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _until(predicate: Callable[[], bool], timeout: float) -> float:
    """Wait until `predicate` holds; return the seconds it took, failing past `timeout`."""
    start = time.monotonic()
    while not predicate():
        if time.monotonic() - start > timeout:
            pytest.fail(f"condition not met within {timeout}s")
        time.sleep(0.05)
    return time.monotonic() - start


def _kill_group(process: subprocess.Popen[str]) -> None:
    """SIGKILL the wrapper's whole process group, its command included."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        # Darwin refuses a group whose only member is the exited, unreaped wrapper; reap it.
        pass
    process.wait(timeout=10)


def _finish(process: subprocess.Popen[str], timeout: float = 20) -> int:
    try:
        return process.wait(timeout=timeout)
    finally:
        _kill_group(process)


@pytest.fixture
def reaper() -> Iterator[list[subprocess.Popen[str]]]:
    """Kill any wrapper group a failing test left running, so no stray process outlives it."""
    started: list[subprocess.Popen[str]] = []
    yield started
    for process in started:
        _kill_group(process)


# --- Inputs are refused before any AWS call ---------------------------------------------------

_LOCK_REQUIRED = (
    "--profile",
    "--region",
    "--expected-account",
    "--env",
    "--lock-file",
    "--log-file",
    "--targets-file",
    "--source-sha",
    "--label",
)


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize("missing", _LOCK_REQUIRED)
def test_deploy_locked_refuses_each_missing_input_before_any_command(
    missing: str, lock: Lock, stub: Stub
) -> None:
    argv = [BASH, str(SCRIPTS / "deploy-locked.sh"), *lock.args(**{missing: ""}), "--", "true"]
    result = _run(argv, cwd=lock.repo, env=stub.env(system_path=False))

    assert result.returncode == 2
    assert f"{missing} is required" in result.stderr
    assert stub.calls == []
    assert not lock.lock.exists()


@pytest.mark.covers("ADR-136")
def test_deploy_locked_refuses_a_default_profile_and_a_missing_command(
    lock: Lock, stub: Stub
) -> None:
    base = [BASH, str(SCRIPTS / "deploy-locked.sh")]

    def attempt(argv: list[str]) -> subprocess.CompletedProcess[str]:
        return _run([*base, *argv], cwd=lock.repo, env=stub.env(system_path=False))

    default = attempt([*lock.args(**{"--profile": "default"}), "--", "true"])
    no_command = attempt([*lock.args(), "--"])

    assert default.returncode == 2
    assert "nondefault" in default.stderr
    assert no_command.returncode == 2
    assert "a command after -- is required" in no_command.stderr
    assert stub.calls == []


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("text", "refusal"),
    [
        (
            f"Adlytics-Alpha-test {APP_REGION}\nAdlytics-Alpha-prod {APP_REGION}\n",
            "does not end in -test",
        ),
        ("Adlytics-Alpha-test\n", "is not '<stack> <region>'"),
        (f"Adlytics-Alpha-test {APP_REGION} extra\n", "is not '<stack> <region>'"),
        (f"Adlytics-Alpha-test {APP_REGION}\nAdlytics-Alpha-test {APP_REGION}\n", "listed twice"),
        ("\n", "no target is listed"),
    ],
    ids=["other-env", "no-region", "extra-field", "duplicate", "empty"],
)
def test_deploy_locked_refuses_a_target_it_could_not_read_where_it_is(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    text: str, refusal: str, lock: Lock, stub: Stub
) -> None:
    lock.targets.write_text(text)

    process = _wrapper(lock, stub, ["true"])

    assert _finish(process) == 2
    assert refusal in (process.stdout.read() if process.stdout else "")
    assert stub.calls == []
    assert not lock.lock.exists()


_DIFF_REQUIRED = (
    "--profile",
    "--env",
    "--region",
    "--expected-account",
    "--stack",
    "--evidence-dir",
)


def _diff_args(tmp_path: Path, **override: str) -> list[str]:
    values = {
        "--profile": "my-profile",
        "--env": "test",
        "--region": "test-region-1",
        "--expected-account": ACCOUNT,
        "--stack": "Adlytics-Alpha-test",
        "--evidence-dir": str(tmp_path / "absent-evidence"),
    }
    values.update(override)
    return [*(item for pair in values.items() for item in pair if pair[1] != ""), "--diff-only"]


@pytest.mark.parametrize("missing", _DIFF_REQUIRED)
def test_diff_then_deploy_refuses_each_missing_input_before_any_command(
    missing: str, tmp_path: Path, stub: Stub
) -> None:
    result = _run(
        [BASH, str(SCRIPTS / "diff-then-deploy.sh"), *_diff_args(tmp_path, **{missing: ""})],
        cwd=tmp_path,
        env=stub.env(system_path=False),
    )

    assert result.returncode == 2
    assert f"{missing} is required" in result.stderr
    assert stub.calls == []


def test_diff_then_deploy_requires_a_mode(tmp_path: Path, stub: Stub) -> None:
    result = _run(
        [BASH, str(SCRIPTS / "diff-then-deploy.sh"), *_diff_args(tmp_path)[:-1]],
        cwd=tmp_path,
        env=stub.env(system_path=False),
    )

    assert result.returncode == 2
    assert "--diff-only or --deploy" in result.stderr


def test_diff_then_deploy_outside_the_lock_exits_79_and_inside_it_passes_that_gate(
    tmp_path: Path, stub: Stub
) -> None:
    argv = [BASH, str(SCRIPTS / "diff-then-deploy.sh"), *_diff_args(tmp_path)]
    not_ancestor = tmp_path / "other.lock"
    not_ancestor.write_text("pid=1 label=other\n")
    ancestor = tmp_path / "held.lock"
    ancestor.write_text(f"pid={os.getpid()} label=unit\n")

    def attempt(held: str | None) -> subprocess.CompletedProcess[str]:
        env = stub.env()
        if held is not None:
            env["ADLYTICS_DEPLOY_LOCK_HELD"] = held
        return _run(argv, cwd=tmp_path, env=env)

    unset = attempt(None)
    foreign = attempt(str(not_ancestor))
    held = attempt(str(ancestor))

    assert (unset.returncode, foreign.returncode) == (79, 79)
    assert "NOT_UNDER_LOCK" in unset.stderr
    # Paired control: a lock whose pid is this test's own process passes the gate and stops at
    # the next one, the absent evidence directory.
    assert held.returncode == 2
    assert "the evidence directory does not exist" in held.stderr
    assert stub.calls == []


# --- Staged tree --------------------------------------------------------------------------------


def _staged_repo(tmp_path: Path) -> Path:
    repo = new_repo(tmp_path)
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "contracts").mkdir()
    commit(repo, "src/pkg/handler.py", "VALUE = 1\n")
    commit(repo, "contracts/table.yaml", "name: fictional\n")
    (repo / "build" / "src" / "pkg").mkdir(parents=True)
    (repo / "build" / "src" / "contracts").mkdir()
    (repo / "build" / "src" / "pkg" / "handler.py").write_text("VALUE = 1\n")
    (repo / "build" / "src" / "contracts" / "table.yaml").write_text("name: fictional\n")
    return repo


def test_a_staged_tree_equal_to_head_passes_and_one_changed_byte_exits_81(tmp_path: Path) -> None:
    repo = _staged_repo(tmp_path)

    equal = script("staged-tree-check.sh", str(repo), cwd=repo)
    (repo / "build" / "src" / "pkg" / "handler.py").write_text("VALUE = 2\n")
    changed = script("staged-tree-check.sh", str(repo), cwd=repo)

    assert equal.returncode == 0
    assert equal.stdout.splitlines()[-1] == "STAGED_EQUAL"
    assert "checked=2 mismatches=0 extra_py=0" in equal.stdout
    assert changed.returncode == 81
    assert "MISMATCH src/pkg/handler.py" in changed.stdout
    assert changed.stdout.splitlines()[-1] == "STAGED_DIFFERS"


def test_an_extra_staged_module_or_an_absent_staged_tree_exits_81(tmp_path: Path) -> None:
    repo = _staged_repo(tmp_path)
    (repo / "build" / "src" / "pkg" / "stray.py").write_text("STRAY = True\n")

    extra = script("staged-tree-check.sh", str(repo), cwd=repo)
    run(["/bin/rm", "-rf", str(repo / "build")], repo)
    absent = script("staged-tree-check.sh", str(repo), cwd=repo)

    assert extra.returncode == 81
    assert "EXTRA build/src/pkg/stray.py" in extra.stdout
    assert absent.returncode == 81
    assert "build/src is absent" in absent.stdout


def test_the_staged_tree_check_needs_exactly_one_repository_argument(tmp_path: Path) -> None:
    assert script("staged-tree-check.sh", cwd=tmp_path).returncode == 2


# --- Lock release (regression for the stuck release) ------------------------------------------


@pytest.mark.covers("ADR-136")
def test_normal_completion_with_every_stack_terminal_releases_within_one_poll(
    lock: Lock, stub: Stub
) -> None:
    out = lock.dir / "held.txt"
    process = _wrapper(lock, stub, [BASH, "-c", f'echo "$ADLYTICS_DEPLOY_LOCK_HELD" > "{out}"'])

    assert _finish(process) == 0
    assert not lock.lock.exists()
    assert out.read_text().strip() == str(lock.lock)
    log = lock.log_text
    assert "checks ok" in log
    assert "release_poll" in log
    # Released at the first poll: one release_poll line, and it read every stack terminal.
    assert log.count("release_poll") == 1
    assert "result=terminal" in log
    assert "\nreleased " in log
    # Each target is read by its exact name in its own Region; nothing lists the account.
    for stack, region in TARGETS:
        assert any(
            f"describe-stacks --stack-name {stack} " in call and f"--region {region} " in call
            for call in stub.calls
        ), (stack, region)
    assert not any("list-stacks" in call for call in stub.calls)


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("sent", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)], ids=["sigterm", "cancel"]
)
def test_a_signal_with_every_stack_terminal_stops_the_command_and_releases_within_one_poll(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    sent: signal.Signals, code: int, lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    process = _wrapper(lock, stub, ["sleep", "60"])
    reaper.append(process)
    _until(lambda: "command started" in lock.log_text, timeout=10)

    process.send_signal(sent)
    released = _until(lambda: not lock.lock.exists(), timeout=POLL + 4)

    assert _finish(process) == code
    assert released <= POLL + 2
    log = lock.log_text
    assert f"signal {sent.name.removeprefix('SIG')}" in log
    assert "forwarded=TERM" in log
    assert "command exit=143" in log
    # Released at the first poll (bash's whole-second clock may already read 1s there).
    assert log.count("release_poll") == 1
    assert "\nreleased " in log
    assert "LOCK_KEPT" not in log


@pytest.mark.covers("ADR-136")
def test_the_lock_stays_held_while_a_stack_is_in_progress_and_goes_once_it_is_terminal(
    lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    gate = lock.dir / "go"
    process = _wrapper(lock, stub, [BASH, "-c", f'while [ ! -f "{gate}" ]; do sleep 0.1; done'])
    reaper.append(process)
    _until(lambda: "command started" in lock.log_text, timeout=10)
    stub.status("Adlytics-Beta-test", "UPDATE_IN_PROGRESS")
    gate.write_text("")
    _until(lambda: "command exit=0" in lock.log_text, timeout=10)

    # Positive control: three polls later the lock is still held, and each poll says why.
    time.sleep(3 * POLL + 0.5)
    assert lock.lock.exists()
    assert process.poll() is None
    assert (
        lock.log_text.count(
            f"result=in_progress Adlytics-Beta-test@{APP_REGION}=UPDATE_IN_PROGRESS"
        )
        >= 2
    )

    stub.status("Adlytics-Beta-test", "UPDATE_COMPLETE")
    released = _until(lambda: not lock.lock.exists(), timeout=POLL + 4)

    assert _finish(process) == 0
    assert released <= POLL + 2
    assert "released" in lock.log_text.splitlines()[-1]


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("stack", "region"),
    [EDGE, ("CDKToolkit", APP_REGION), ("CDKToolkit", EDGE_REGION)],
    ids=["edge-stack", "bootstrap-app-region", "bootstrap-edge-region"],
)
def test_a_target_outside_the_app_stacks_in_progress_keeps_the_lock_while_they_are_terminal(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    stack: str, region: str, lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    gate = lock.dir / "go"
    process = _wrapper(lock, stub, [BASH, "-c", f'while [ ! -f "{gate}" ]; do sleep 0.1; done'])
    reaper.append(process)
    _until(lambda: "command started" in lock.log_text, timeout=10)
    stub.status(stack, "UPDATE_IN_PROGRESS", region)
    gate.write_text("")
    _until(lambda: "command exit=0" in lock.log_text, timeout=10)

    # Every application-Region app stack reads UPDATE_COMPLETE; the edge stack is absent from the
    # application Region, where a single-Region read would have found nothing to wait for.
    time.sleep(2 * POLL + 0.5)
    assert lock.lock.exists()
    assert process.poll() is None
    assert lock.log_text.count(f"result=in_progress {stack}@{region}=UPDATE_IN_PROGRESS") >= 2

    stub.status(stack, "UPDATE_COMPLETE", region)
    _until(lambda: not lock.lock.exists(), timeout=POLL + 4)

    assert _finish(process) == 0
    assert "result=terminal" in lock.log_text


def _script(path: Path, text: str) -> list[str]:
    path.write_text(text)
    return [BASH, str(path)]


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("sent", "code"), [(signal.SIGTERM, 143), (signal.SIGINT, 130)], ids=["sigterm", "cancel"]
)
def test_a_signal_stops_every_descendant_before_the_first_terminal_poll(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    sent: signal.Signals, code: int, lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    # The command is a shell that starts a deploy-like descendant and waits, as deploy-all.sh starts
    # npx and cdk. The descendant takes a second to stop on TERM and records each step in the log.
    descendant = lock.dir / "descendant.sh"
    descendant.write_text(
        f'trap \'echo "descendant TERM" >> "{lock.log}"; sleep 1; '
        f'echo "descendant exited" >> "{lock.log}"; exit 143\' TERM\n'
        f'echo "descendant started $$" >> "{lock.log}"\n'
        "while :; do sleep 0.1; done\n"
    )
    command = _script(lock.dir / "command.sh", f'{BASH} "{descendant}" &\nwait\n')
    process = _wrapper(lock, stub, command)
    reaper.append(process)
    _until(lambda: "descendant started" in lock.log_text, timeout=10)
    pid = int(re.findall(r"descendant started (\d+)", lock.log_text)[0])

    process.send_signal(sent)
    _until(lambda: not lock.lock.exists(), timeout=POLL + 6)

    assert _finish(process) == code
    log = lock.log_text
    assert "forwarded=TERM process_group=" in log
    assert "descendant TERM" in log
    # The wait saw the descendant still running, and released only after it was gone.
    assert "result=command_running" in log
    assert log.index("descendant exited") < log.index("result=terminal")
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert "LOCK_KEPT" not in log


@pytest.mark.covers("ADR-136")
def test_a_descendant_that_outlives_the_command_keeps_the_lock_until_it_is_gone(
    lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    gate = lock.dir / "go"
    lingering = lock.dir / "lingering.sh"
    lingering.write_text(
        f'while [ ! -f "{gate}" ]; do sleep 0.1; done\necho "lingering exited" >> "{lock.log}"\n'
    )
    command = _script(lock.dir / "command.sh", f'{BASH} "{lingering}" &\nexit 0\n')
    process = _wrapper(lock, stub, command)
    reaper.append(process)
    _until(lambda: "command exit=0" in lock.log_text, timeout=10)

    # Positive control: the command has exited and every target is terminal, yet a process of its
    # group still runs, so the lock stays and each poll says why.
    time.sleep(2 * POLL + 0.5)
    assert lock.lock.exists()
    assert process.poll() is None
    assert lock.log_text.count("result=command_running") >= 2

    gate.write_text("")
    _until(lambda: not lock.lock.exists(), timeout=POLL + 4)

    assert _finish(process) == 0
    log = lock.log_text
    assert log.index("lingering exited") < log.index("result=terminal")


@pytest.mark.covers("ADR-136")
def test_the_command_leads_its_own_group_with_terminal_stops_ignored(
    lock: Lock, stub: Stub
) -> None:
    seen = lock.dir / "seen.txt"
    probe = (
        "import os, signal\n"
        "stops = (signal.SIGTTIN, signal.SIGTTOU)\n"
        "ignored = all(signal.getsignal(stop) is signal.SIG_IGN for stop in stops)\n"
        f"with open({str(seen)!r}, 'w') as out:\n"
        "    out.write('%d %d %d %s' % (os.getpgid(0), os.getpid(), os.getppid(), ignored))\n"
    )

    process = _wrapper(lock, stub, [sys.executable, "-c", probe])

    assert _finish(process) == 0
    group, pid, parent, ignored = seen.read_text().split(" ")
    # The command leads a group of its own; the wrapper leads its own session's group.
    assert group == pid
    assert parent == str(process.pid)
    assert int(group) != process.pid
    assert ignored == "True"


@pytest.mark.covers("ADR-136")
def test_a_read_failure_never_releases_and_is_logged_on_every_poll(lock: Lock, stub: Stub) -> None:
    gate = lock.dir / "go"
    process = _wrapper(
        lock,
        stub,
        [BASH, "-c", f'while [ ! -f "{gate}" ]; do sleep 0.1; done'],
        **{"--bound-seconds": "2"},
    )
    _until(lambda: "command started" in lock.log_text, timeout=10)
    stub.status("Adlytics-Alpha-test", "FAIL")
    gate.write_text("")

    assert _finish(process) == 84
    assert lock.lock.exists()
    log = lock.log_text
    assert f"result=read_failed Adlytics-Alpha-test@{APP_REGION}: Could not connect" in log
    assert "LOCK_KEPT" in log
    assert "reason=bound" in log
    assert "released" not in log


@pytest.mark.covers("ADR-136")
def test_a_signal_during_the_release_wait_keeps_the_lock_with_a_record(
    lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    gate = lock.dir / "go"
    process = _wrapper(lock, stub, [BASH, "-c", f'while [ ! -f "{gate}" ]; do sleep 0.1; done'])
    reaper.append(process)
    _until(lambda: "command started" in lock.log_text, timeout=10)
    stub.status("Adlytics-Beta-test", "UPDATE_IN_PROGRESS")
    gate.write_text("")
    _until(lambda: "result=in_progress" in lock.log_text, timeout=10)

    process.send_signal(signal.SIGTERM)

    assert _finish(process) == 84
    assert lock.lock.exists()
    log = lock.log_text
    assert "signal_during_release TERM" in log
    assert "reason=signal_during_release" in log


# --- Refusals at entry --------------------------------------------------------------------------


@pytest.mark.covers("ADR-136")
def test_a_held_lock_is_refused_and_left_unchanged(lock: Lock, stub: Stub) -> None:
    lock.lock.write_text("pid=1 label=other\n")

    process = _wrapper(lock, stub, ["true"])

    assert _finish(process) == 75
    assert lock.lock.read_text() == "pid=1 label=other\n"
    assert "LOCK_HELD" in lock.log_text
    assert stub.calls == []


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("arrange", "code", "record"),
    [
        ("identity", 76, "IDENTITY_MISMATCH"),
        ("in_progress", 77, "STACK_IN_PROGRESS"),
        ("sha", 78, "reason=head"),
        ("dirty", 78, "reason=dirty_tree"),
        ("branch", 78, "reason=branch"),
    ],
)
def test_an_entry_refusal_runs_nothing_and_releases_at_once(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    arrange: str, code: int, record: str, lock: Lock, stub: Stub
) -> None:
    ran = lock.dir / "ran"
    override: dict[str, str] = {}
    if arrange == "identity":
        stub.account(OTHER_ACCOUNT)
    elif arrange == "in_progress":
        stub.status("Adlytics-Alpha-test", "CREATE_IN_PROGRESS")
    elif arrange == "sha":
        # HEAD moves on; the declared source stays at the first commit.
        commit(lock.repo, "b.txt", "second\n")
        override["--source-sha"] = lock.head
    elif arrange == "dirty":
        (lock.repo / "untracked.txt").write_text("dirty\n")
    else:
        override["--source-branch"] = "release"

    process = _wrapper(lock, stub, ["touch", str(ran)], **override)

    assert _finish(process) == code
    assert not ran.exists()
    assert not lock.lock.exists()
    log = lock.log_text
    assert record in log
    assert "waited_for_terminal=skipped" in log


@pytest.mark.covers("ADR-136")
def test_the_paired_clean_entry_runs_the_command(lock: Lock, stub: Stub) -> None:
    ran = lock.dir / "ran"

    process = _wrapper(lock, stub, ["touch", str(ran)], **{"--source-branch": "main"})

    assert _finish(process) == 0
    assert ran.exists()


# --- Stale lock: explicit force-release only ---------------------------------------------------


def _gone_pid() -> int:
    # A-SUBPROCESS-001: fixed argv; see docs/security-dispositions.md
    finished = subprocess.Popen(["/usr/bin/true"])
    finished.wait()
    return finished.pid


def _abandon(lock: Lock, stub: Stub) -> int:
    """Take the lock for a running command, then SIGKILL the wrapper: a real stale lock.

    Returns the gone owner's pid. The command's own group is killed too, so nothing runs on.
    """
    process = _wrapper(lock, stub, ["sleep", "60"])
    try:
        _until(lambda: "command started" in lock.log_text, timeout=10)
    finally:
        _kill_group(process)
        for group in lock.command_groups():
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                continue
    for group in lock.command_groups():
        _until(lambda group=group: _group_gone(group), timeout=10)
    assert lock.lock.exists()
    return process.pid


def _group_gone(group: int) -> bool:
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        # Darwin refuses a group whose only member is exited and not yet reaped: not gone yet.
        return False
    return False


def _force(lock: Lock, stub: Stub, owner: int, **override: str) -> subprocess.CompletedProcess[str]:
    args = lock.args(**{"--source-sha": "", "--label": "", "--poll-seconds": "", **override})
    return _run(
        [
            BASH,
            str(SCRIPTS / "deploy-locked.sh"),
            "--force-release",
            *args,
            "--owner-pid",
            str(owner),
            "--reason",
            "wrapper killed by the operator",
        ],
        cwd=lock.repo,
        env=stub.env(),
    )


@pytest.mark.covers("ADR-136")
def test_the_lock_records_its_account_environment_region_and_targets(
    lock: Lock, stub: Stub
) -> None:
    owner = _abandon(lock, stub)

    lines = lock.lock.read_text().splitlines()

    assert lines[0].startswith(f"pid={owner} label=unit ")
    assert lines[1].startswith(f"account={ACCOUNT} env=test region={APP_REGION} targets=5 ")
    assert re.search(r" targets_sha256=[0-9a-f]{64}$", lines[1])
    assert lines[2:-1] == [f"target={stack} {region}" for stack, region in TARGETS]
    # The command's process group, recorded before the command was let start.
    assert lines[-1] == f"command_group={lock.command_groups()[0]}"


@pytest.mark.covers("ADR-136")
def test_force_release_refuses_while_the_command_group_outlives_its_killed_wrapper(
    lock: Lock, stub: Stub
) -> None:
    tick = lock.dir / "tick"
    command = [BASH, "-c", f'while :; do echo tick >> "{tick}"; sleep 0.1; done']
    process = _wrapper(lock, stub, command)
    try:
        _until(lambda: "command started" in lock.log_text, timeout=10)
        group = lock.command_groups()[0]
        # SIGKILL the wrapper alone: the command's group, outside the wrapper's, runs on.
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        before = lock.lock.read_text()
        assert f"command_group={group}" in before.splitlines()
        _until(tick.exists, timeout=5)
        ticks = tick.stat().st_size
        _until(lambda: tick.stat().st_size > ticks, timeout=5)
        calls_before = len(stub.calls)

        refused = _force(lock, stub, process.pid)

        # Every stub stack is terminal and the owner pid is gone, yet the lock stays.
        assert refused.returncode == 83
        assert f"command_group_alive process_group={group}" in lock.log_text
        assert "FORCED_RELEASE " not in lock.log_text
        assert lock.lock.read_text() == before
        assert len(stub.calls) == calls_before

        # Paired control: once that group is gone, the same force release removes the lock.
        os.killpg(group, signal.SIGKILL)
        _until(lambda: _group_gone(group), timeout=10)
        released = _force(lock, stub, process.pid)

        assert released.returncode == 0
        assert not lock.lock.exists()
        assert f"command_group={group} command_group_alive=false" in lock.log_text
    finally:
        _kill_group(process)


@pytest.mark.covers("ADR-136")
def test_a_stale_lock_is_removed_only_by_force_release_and_logs_forced_release(
    lock: Lock, stub: Stub
) -> None:
    owner = _abandon(lock, stub)
    # The same target set, listed in another order, is the same set.
    lock.targets.write_text(_targets_text(tuple(reversed(TARGETS))))

    result = _force(lock, stub, owner)

    assert result.returncode == 0
    assert not lock.lock.exists()
    line = lock.log_text.splitlines()[-1]
    assert line.startswith("FORCED_RELEASE ")
    assert f"pid={owner}" in line
    group = lock.command_groups()[0]
    assert (
        f"sts={ACCOUNT} in_progress=none owner_alive=false command_group={group} "
        "command_group_alive=false targets=5"
    ) in line


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("arrange", "code", "reason"),
    [
        ("account", 83, "account_mismatch"),
        ("region", 83, "region_mismatch"),
        ("changed_targets", 83, "targets_mismatch"),
        ("omitted_in_progress", 83, "targets_mismatch"),
        ("in_progress", 77, f"Adlytics-Edge-test@{EDGE_REGION}=UPDATE_IN_PROGRESS"),
        ("identity", 76, "IDENTITY_MISMATCH"),
    ],
)
def test_force_release_refuses_a_target_set_other_than_the_one_the_lock_recorded(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    arrange: str, code: int, reason: str, lock: Lock, stub: Stub
) -> None:
    owner = _abandon(lock, stub)
    before = lock.lock.read_text()
    override: dict[str, str] = {}
    if arrange == "account":
        # The caller's identity agrees with the changed account: only the lock's record refuses.
        stub.account(OTHER_ACCOUNT)
        override["--expected-account"] = OTHER_ACCOUNT
    elif arrange == "region":
        override["--region"] = "other-region-1"
    elif arrange == "changed_targets":
        lock.targets.write_text(_targets_text((*TARGETS, ("Adlytics-Gamma-test", APP_REGION))))
    elif arrange == "omitted_in_progress":
        # The abandoned deploy's edge stack is still updating; a target set without it is refused
        # before any read, so its in-progress state cannot be missed.
        stub.status(EDGE[0], "UPDATE_IN_PROGRESS", EDGE[1])
        lock.targets.write_text(_targets_text(tuple(t for t in TARGETS if t != EDGE)))
    elif arrange == "in_progress":
        stub.status(EDGE[0], "UPDATE_IN_PROGRESS", EDGE[1])
    else:
        stub.account(OTHER_ACCOUNT)
    calls_before = len(stub.calls)

    result = _force(lock, stub, owner, **override)

    assert result.returncode == code
    assert reason in lock.log_text
    assert "FORCED_RELEASE " not in lock.log_text
    assert lock.lock.read_text() == before
    if code == 83:
        assert len(stub.calls) == calls_before


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize(
    ("arrange", "reason"),
    [
        ("alive", "owner_alive"),
        ("mismatch", "owner_mismatch"),
        ("absent", "no_lock_file"),
        ("no_targets", "lock_records_no_targets"),
    ],
)
def test_force_release_refuses_unless_the_owner_is_named_and_gone(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    arrange: str, reason: str, lock: Lock, stub: Stub
) -> None:
    owner = os.getpid() if arrange == "alive" else _gone_pid()
    if arrange != "absent":
        # A lock that records no targets (the shape the untracked wrapper wrote) cannot be bound.
        lock.lock.write_text(f"pid={owner} label=held\n")

    result = _force(lock, stub, owner + 1 if arrange == "mismatch" else owner)

    assert result.returncode == 83
    assert reason in lock.log_text
    assert "FORCED_RELEASE " not in lock.log_text
    assert lock.lock.exists() == (arrange != "absent")
    assert stub.calls == []


# Sourced by the non-interactive wrapper through BASH_ENV: `kill -0` of the denied pid fails as a
# pid this caller may not signal does, and every other kill is the builtin.
_DENIED_KILL = """kill() {
  if [ "$1" = -0 ] && [ "$2" = "$STUB_DENIED_PID" ]; then
    echo "kill: ($2) - Operation not permitted" >&2
    return 1
  fi
  builtin kill "$@"
}
"""


@pytest.mark.covers("ADR-136")
def test_force_release_refuses_an_owner_check_that_does_not_prove_the_owner_gone(
    lock: Lock, stub: Stub
) -> None:
    owner = _abandon(lock, stub)
    before = lock.lock.read_text()
    seam = lock.dir / "denied-kill.bash"
    seam.write_text(_DENIED_KILL)
    calls_before = len(stub.calls)

    stub.extra = {"BASH_ENV": str(seam), "STUB_DENIED_PID": str(owner)}

    refused = _force(lock, stub, owner)

    # The owner is gone, but its check failed for another reason: nothing proves it gone.
    assert refused.returncode == 83
    assert f'owner_unknown pid={owner} error="kill: ({owner}) - Operation not permitted"' in (
        lock.log_text
    )
    assert "FORCED_RELEASE " not in lock.log_text
    assert lock.lock.read_text() == before
    assert len(stub.calls) == calls_before

    # Paired control: the same lock, with the owner check reporting no such process, is released.
    stub.extra = {}
    released = _force(lock, stub, owner)

    assert released.returncode == 0
    assert not lock.lock.exists()
    assert "owner_alive=false" in lock.log_text.splitlines()[-1]


@pytest.mark.covers("ADR-136")
@pytest.mark.skipif(os.geteuid() == 0, reason="root may signal pid 1, so its check is conclusive")
def test_force_release_refuses_a_recorded_owner_this_caller_may_not_signal(
    lock: Lock, stub: Stub
) -> None:
    # A real non-signalable pid: kill -0 1 fails with "Operation not permitted", not "No such
    # process", for any caller but root.
    _abandon(lock, stub)
    lines = lock.lock.read_text().splitlines()
    lock.lock.write_text("\n".join(["pid=1 label=unit", *lines[1:]]) + "\n")
    before = lock.lock.read_text()
    calls_before = len(stub.calls)

    refused = _force(lock, stub, 1)

    assert refused.returncode == 83
    assert "owner_unknown pid=1 error=" in lock.log_text
    assert "FORCED_RELEASE " not in lock.log_text
    assert lock.lock.read_text() == before
    assert len(stub.calls) == calls_before


# --- A lock-log append that fails after the start check is LOG_WRITE_FAILED (85) ---------------


def _restore_log(lock: Lock) -> str:
    """Undo a broken log (moved aside, a directory in its place); return what it held."""
    before = lock.log.with_name(lock.log.name + ".before")
    if lock.log.is_dir():
        lock.log.rmdir()
    if before.exists():
        before.rename(lock.log)
    return lock.log_text


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize("broken", ["before_command", "during_command", "never"])
def test_a_failed_lock_log_append_keeps_the_lock_and_exits_85(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    broken: str, lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    ran = lock.dir / "ran"
    # During the command: once `command started` is durable, so the record that fails is the
    # command's exit.
    breaker = (
        f'until grep -q "command started" "{lock.log}"; do sleep 0.05; done; '
        f'mv "{lock.log}" "{lock.log}.before" && mkdir "{lock.log}"; '
        if broken == "during_command"
        else ""
    )
    if broken == "before_command":
        # Broken at the identity read, after the start check: `checks ok` is the record that fails.
        (stub.state / "break-log").write_text(str(lock.log))
    process = _wrapper(lock, stub, [BASH, "-c", f'{breaker}echo ran > "{ran}"'])
    reaper.append(process)
    try:
        code = _finish(process)
        assert process.stdout is not None
        out = process.stdout.read()
    finally:
        log = _restore_log(lock)

    if broken == "never":
        # Paired control: the writable log records the run and the lock goes.
        assert code == 0
        assert not lock.lock.exists()
        assert "LOG_WRITE_FAILED" not in out
        assert "\nreleased " in log
        assert ran.exists()
        return
    assert code == 85
    assert lock.lock.exists()
    record = "checks" if broken == "before_command" else "command"
    assert re.search(
        rf"^LOG_WRITE_FAILED \S+ log={re.escape(str(lock.log))} record={record}$", out, re.M
    )
    assert "LOCK_KEPT " in out
    assert "reason=log_write_failed" in out
    assert "released " not in out
    assert "release_approved " not in out
    # Before the command, the missing `checks ok` stops it from starting; during it, the command
    # ran and the release wait never removed the lock.
    assert ran.exists() == (broken == "during_command")
    if broken == "during_command":
        assert "command started" in log
    else:
        assert "checks ok" not in log
        assert "command started" not in log

    # Custody stays recoverable: with the log writable again, a force release removes the lock.
    released = _force(lock, stub, process.pid)

    assert released.returncode == 0, released.stderr
    assert not lock.lock.exists()
    assert lock.log_text.splitlines()[-1].startswith("FORCED_RELEASE ")


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize("broken", [True, False], ids=["unwritable", "writable"])
def test_a_force_release_whose_approval_cannot_be_written_leaves_the_lock_unchanged(
    broken: bool,
    lock: Lock,
    stub: Stub,
) -> None:
    owner = _abandon(lock, stub)
    before = lock.lock.read_text()
    if broken:
        # Broken at the identity read: every check passes, then FORCE_RELEASE_APPROVED fails.
        (stub.state / "break-log").write_text(str(lock.log))
    try:
        result = _force(lock, stub, owner)
    finally:
        log = _restore_log(lock)

    if not broken:
        # Paired control: the approval is written before the removal, the outcome after it.
        assert result.returncode == 0
        assert not lock.lock.exists()
        approved, forced = log.splitlines()[-2:]
        assert approved.startswith("FORCE_RELEASE_APPROVED ")
        assert f"pid={owner} checks: sts={ACCOUNT} in_progress=none" in approved
        assert forced.startswith("FORCED_RELEASE ")
        return
    assert result.returncode == 85
    assert re.search(
        rf"^LOG_WRITE_FAILED \S+ log={re.escape(str(lock.log))} record=FORCE_RELEASE_APPROVED$",
        result.stderr,
        re.M,
    )
    assert lock.lock.read_text() == before
    assert "FORCE_RELEASE_APPROVED " not in log
    assert "FORCED_RELEASE " not in log
    assert "FORCED_RELEASE " not in result.stderr


# Sourced by the non-interactive wrapper through BASH_ENV: reading the lock at $STUB_UNREADABLE_LOCK
# fails as an unreadable file does, and with $STUB_BREAK_LOG set, the log is first broken as the
# aws stub's break-log does. Every other cat is the real one.
_UNREADABLE_LOCK = """cat() {
  if [ -n "${STUB_UNREADABLE_LOCK:-}" ] && [ "$1" = "$STUB_UNREADABLE_LOCK" ]; then
    if [ -n "${STUB_BREAK_LOG:-}" ]; then
      mv "$STUB_BREAK_LOG" "$STUB_BREAK_LOG.before" && mkdir "$STUB_BREAK_LOG"
    fi
    echo "cat: $1: Permission denied" >&2
    return 1
  fi
  command cat "$@"
}
"""


@pytest.mark.covers("ADR-136")
@pytest.mark.parametrize("case", ["log_writable", "log_broken", "readable"])
def test_a_lock_record_unreadable_after_the_link_starts_nothing_and_goes_only_once_approved(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    case: str, lock: Lock, stub: Stub, reaper: list[subprocess.Popen[str]]
) -> None:
    ran = lock.dir / "ran"
    seam = lock.dir / "unreadable-lock.bash"
    seam.write_text(_UNREADABLE_LOCK)
    stub.extra = {"BASH_ENV": str(seam)}
    if case != "readable":
        stub.extra["STUB_UNREADABLE_LOCK"] = str(lock.lock)
    if case == "log_broken":
        stub.extra["STUB_BREAK_LOG"] = str(lock.log)
    process = _wrapper(lock, stub, ["touch", str(ran)])
    reaper.append(process)
    try:
        code = _finish(process)
        assert process.stdout is not None
        out = process.stdout.read()
    finally:
        log = _restore_log(lock)

    if case == "readable":
        # Paired control: the same seam with the lock readable passes the read-back and runs.
        assert code == 0, out
        assert "reason=lock_unreadable" not in out
        assert ran.exists()
        assert not lock.lock.exists()
        return
    refusal = re.compile(
        rf"^READ_FAILED \S+ label=unit lock={re.escape(str(lock.lock))} reason=lock_unreadable "
        rf"error=cat: {re.escape(str(lock.lock))}: Permission denied$",
        re.M,
    )
    # Refused at the read-back, before any check: nothing ran and AWS was never read.
    assert refusal.search(out)
    assert not ran.exists()
    assert stub.calls == []
    assert "checks ok" not in out
    if case == "log_writable":
        # The removal follows its durable approval, as for any run that started nothing.
        assert code == 2
        assert not lock.lock.exists()
        assert refusal.search(log)
        approved, released = log.splitlines()[-2:]
        assert approved.startswith("release_approved ")
        assert approved.endswith("label=unit command=not_started reason=lock_unreadable")
        assert released.startswith("released ")
        return
    # Neither the refusal nor an approval is durable: the lock stays, with this run's record.
    assert code == 85
    assert re.search(
        rf"^LOG_WRITE_FAILED \S+ log={re.escape(str(lock.log))} record=READ_FAILED$", out, re.M
    )
    assert re.search(r"^LOCK_KEPT \S+ label=unit reason=log_write_failed$", out, re.M)
    assert "release_approved " not in out
    assert "released " not in out
    assert lock.lock.read_text().startswith(f"pid={process.pid} label=unit ")
    assert "READ_FAILED" not in log

    # Custody stays recoverable: with the log writable again, a force release removes the lock.
    stub.extra = {}
    forced = _force(lock, stub, process.pid)

    assert forced.returncode == 0, forced.stderr
    assert not lock.lock.exists()
    assert lock.log_text.splitlines()[-1].startswith("FORCED_RELEASE ")


# --- Make targets refuse each missing input -----------------------------------------------------


def _target_inputs(root: Path) -> dict[str, dict[str, str]]:
    evidence = root / "evidence"
    custody = root / "custody-make"
    evidence.mkdir(exist_ok=True)
    custody.mkdir(exist_ok=True)
    lock_file = str(custody / "deploy.lock")
    log_file = str(custody / "deploy-lock.log")
    return {
        "dev-state": {"EVIDENCE_DIR": str(evidence), "LOCK_FILE": lock_file},
        "check-alarm-window": {
            "SINCE": "2026-01-01T00:00:00Z",
            "RECOVER_MINUTES": "15",
            "EXPECTED": "none",
            "EVIDENCE_DIR": str(evidence),
        },
        "diff-then-deploy": {
            "STACK": "Adlytics-Alpha-test",
            "EVIDENCE_DIR": str(evidence),
            "MODE": "diff-only",
        },
        "deploy-all-locked": {"LOCK_FILE": lock_file, "LOG_FILE": log_file, "SOURCE_SHA": "0" * 40},
        "check-source-key": {"EVIDENCE_DIR": str(evidence)},
        "deploy-lock-force-release": {
            "LOCK_FILE": lock_file,
            "LOG_FILE": log_file,
            "OWNER_PID": "4242",
            "REASON": "fictional",
        },
    }


_COMMON = {"PROFILE": "my-profile", "ENV": "test", "REGION": "test-region-1", "ACCOUNT": ACCOUNT}
_TARGET_NAMES = {
    "dev-state": ("EVIDENCE_DIR", "LOCK_FILE"),
    "check-alarm-window": ("SINCE", "RECOVER_MINUTES", "EXPECTED", "EVIDENCE_DIR"),
    "diff-then-deploy": ("STACK", "EVIDENCE_DIR", "MODE"),
    "deploy-all-locked": ("LOCK_FILE", "LOG_FILE", "SOURCE_SHA"),
    "check-source-key": ("EVIDENCE_DIR",),
    "deploy-lock-force-release": ("LOCK_FILE", "LOG_FILE", "OWNER_PID", "REASON"),
}
_CASES = [(target, name) for target, extra in _TARGET_NAMES.items() for name in [*_COMMON, *extra]]


def _make(stub: Stub, target: str, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # The recipes need bash and date; everything else they could reach on PATH is a stub.
    for tool, real in (("bash", BASH), ("date", "/bin/date")):
        (stub.bin / tool).unlink(missing_ok=True)
        (stub.bin / tool).symlink_to(real)
    return subprocess.run(  # noqa: S603 -- fixed make binary and repository Makefile
        [
            MAKE,
            "--no-print-directory",
            "-C",
            str(REPO_ROOT),
            target,
            *(f"{key}={value}" for key, value in values.items()),
        ],
        env=stub.env(system_path=False),
        capture_output=True,
        text=True,
        check=False,
    )


def _assert_guard_refused(
    result: subprocess.CompletedProcess[str], target: str, message: str
) -> None:
    """Assert the refusal is the target's own input guard: its message alone, then its `exit 1`.

    make exits 2 for a failed recipe line and names the line's own status (`Error 1`, the guard's
    exit) after the target; no other recipe line printed anything, so none ran past the guard.
    """
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stdout.splitlines() == [message]
    assert re.fullmatch(
        rf"make: \*\*\* \[(?:\S+:\d+: )?{re.escape(target)}\] Error 1\n", result.stderr
    ), result.stderr


def _guard_line(stdout: str, missing: str) -> str:
    lines = stdout.splitlines()
    assert len(lines) == 1, stdout
    assert lines[0].startswith(f"{missing} is required"), stdout
    return lines[0]


@pytest.mark.parametrize(("target", "missing"), _CASES)
def test_each_make_target_refuses_a_missing_input_by_name_before_any_command(  # noqa: PLR0913, PLR0917 -- parametrized case and fixtures
    target: str, missing: str, stub: Stub, tmp_path: Path
) -> None:
    values = {**_COMMON, **_target_inputs(tmp_path)[target]}
    del values[missing]

    result = _make(stub, target, values)

    _assert_guard_refused(result, target, _guard_line(result.stdout, missing))
    assert stub.calls == []


@pytest.mark.parametrize("target", list(_TARGET_NAMES))
def test_each_make_target_with_every_input_reaches_its_tool(
    target: str, stub: Stub, tmp_path: Path
) -> None:
    result = _make(stub, target, {**_COMMON, **_target_inputs(tmp_path)[target]})
    output = result.stdout + result.stderr

    # Paired control: the same target with nothing missing passes every input check and reaches
    # its first command (a stub, so it then fails; diff-then-deploy stops at its lock gate).
    assert "is required" not in output
    if target == "diff-then-deploy":
        assert "NOT_UNDER_LOCK" in output
    else:
        assert stub.calls != []


def test_make_refuses_a_default_profile_and_a_deploy_mode_without_its_read_diff(
    stub: Stub, tmp_path: Path
) -> None:
    inputs = _target_inputs(tmp_path)
    default = _make(stub, "dev-state", {**_COMMON, **inputs["dev-state"], "PROFILE": "default"})
    deploy = _make(
        stub, "diff-then-deploy", {**_COMMON, **inputs["diff-then-deploy"], "MODE": "deploy"}
    )

    _assert_guard_refused(default, "dev-state", "PROFILE must name an explicit nondefault profile")
    _assert_guard_refused(deploy, "diff-then-deploy", "READ_DIFF is required with MODE=deploy")
    assert stub.calls == []

    # Paired controls: the same targets past those guards. A nondefault profile reaches the
    # listing stub; MODE=deploy with its read diff reaches the script's lock gate.
    read_diff = tmp_path / "read.diff"
    read_diff.write_text("fictional diff\n")
    named = _make(stub, "dev-state", {**_COMMON, **inputs["dev-state"]})
    deployed = _make(
        stub,
        "diff-then-deploy",
        {**_COMMON, **inputs["diff-then-deploy"], "MODE": "deploy", "READ_DIFF": str(read_diff)},
    )

    assert "nondefault" not in named.stdout + named.stderr
    assert stub.calls != []
    assert "READ_DIFF is required" not in deployed.stdout + deployed.stderr
    assert "NOT_UNDER_LOCK" in deployed.stdout + deployed.stderr


# --- Customer deploy-all resume and invite idempotence (fresh-env pre-flight B4 and S2) --------

_CUSTOMER_AWS_STUB = r"""#!/bin/bash
# Test stub of the aws CLI for the customer scripts. Never reaches AWS.
echo "aws $*" >> "$STUB_STATE/calls.log"
args=("$@") name=""
for ((i = 0; i < ${#args[@]}; i++)); do
  case "${args[$i]}" in --name | --stack-name) name="${args[$((i + 1))]}" ;; esac
done
case "$1 $2" in
  "sts get-caller-identity") echo 111111111111 ;;
  "configure get") echo test-region-1 ;;
  "cloudformation describe-stacks")
    f="$STUB_STATE/stacks/$name"
    if [ ! -f "$f" ]; then
      echo "An error occurred (ValidationError) when calling the DescribeStacks operation:" \
        "Stack with id $name does not exist" >&2
      exit 254
    fi
    cat "$f" ;;
  "ssm get-parameter")
    f="$STUB_STATE/ssm/$(printf '%s' "$name" | tr / _)"
    if [ ! -f "$f" ]; then
      echo "An error occurred (ParameterNotFound) when calling the GetParameter operation: " >&2
      exit 254
    fi
    if [ "$(cat "$f")" = DENIED ]; then
      echo "An error occurred (AccessDeniedException) when calling the GetParameter operation:" \
        "fixture" >&2
      exit 254
    fi
    cat "$f" ;;
  "cognito-idp admin-create-user")
    if [ -f "$STUB_STATE/cognito-error" ]; then
      cat "$STUB_STATE/cognito-error" >&2
      exit 254
    fi
    printf 'someone@example.com\tFORCE_CHANGE_PASSWORD\n' ;;
  *) echo "stub: unexpected aws $*" >&2; exit 99 ;;
esac
"""

_CUSTOMER_NPX_STUB = """#!/bin/bash
flags="api=$ADLYTICS_API_ORIGIN_ATTACHED conversation=$ADLYTICS_CONVERSATION_ORIGIN_ATTACHED"
echo "npx $* $flags" >> "$STUB_STATE/calls.log"
"""

_CUSTOMER_TOOL_STUB = """#!/bin/bash
echo "{name} $*" >> "$STUB_STATE/calls.log"
"""


class CustomerWorld:
    """Stubbed aws, npx, node, npm, uv, make and file for deploy-all.sh and create-user.sh."""

    def __init__(self, root: Path) -> None:
        self.bin = root / "customer-bin"
        self.state = root / "customer-state"
        for directory in (self.bin, self.state / "stacks", self.state / "ssm"):
            directory.mkdir(parents=True)
        (self.bin / "aws").write_text(_CUSTOMER_AWS_STUB)
        (self.bin / "npx").write_text(_CUSTOMER_NPX_STUB)
        node_major = (REPO_ROOT / ".nvmrc").read_text().strip()
        (self.bin / "node").write_text(f'#!/bin/bash\necho "v{node_major}.0.0"\n')
        for name in ("npm", "uv", "make", "file"):
            (self.bin / name).write_text(_CUSTOMER_TOOL_STUB.format(name=name))
        for path in self.bin.iterdir():
            path.chmod(0o755)
        self.stack("CDKToolkit", "CREATE_COMPLETE")
        self.parameter("/adlytics/test/frontend/public-base-url", "https://site.example.com")

    def stack(self, name: str, status: str) -> None:
        (self.state / "stacks" / name).write_text(status + "\n")

    def parameter(self, name: str, value: str) -> None:
        (self.state / "ssm" / name.replace("/", "_")).write_text(value + "\n")

    def calls(self, prefix: str) -> list[str]:
        log = self.state / "calls.log"
        lines = log.read_text().splitlines() if log.exists() else []
        return [line for line in lines if line.startswith(prefix)]

    def run(self, name: str, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return _run(
            [BASH, str(SCRIPTS / name), *args],
            cwd=REPO_ROOT,
            env={
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "HOME": str(self.state),
                "STUB_STATE": str(self.state),
                "AWS_CONFIG_FILE": "/dev/null",
                "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
                "PROFILE": "my-profile",
                **env,
            },
        )


@pytest.fixture
def customer(tmp_path: Path) -> CustomerWorld:
    return CustomerWorld(tmp_path)


_DEPLOY_ALL = (
    "npx cdk deploy --all --require-approval never --concurrency 1 --profile my-profile -c env=test"
)
_ATTACH = (
    "npx cdk deploy Adlytics-Frontend-test --exclusively --require-approval never"
    " --profile my-profile -c env=test"
)


def test_deploy_all_resumes_as_a_first_deployment_after_the_api_create_rolled_back(
    customer: CustomerWorld,
) -> None:
    """An API stack in ROLLBACK_COMPLETE wrote neither parameter the frontend's origins read."""
    customer.stack("Adlytics-Api-test", "ROLLBACK_COMPLETE")

    result = customer.run("deploy-all.sh", ENV="test", APPROVE="1")

    assert result.returncode == 0, result.stderr
    assert customer.calls("npx cdk deploy") == [
        f"{_DEPLOY_ALL} api=false conversation=false",
        f"{_ATTACH} api=true conversation=true",
    ]
    assert customer.calls("aws ssm get-parameter --name /adlytics/test/api/") == [
        "aws ssm get-parameter --name /adlytics/test/api/id --profile my-profile"
        " --region test-region-1 --query Parameter.Name --output text",
        "aws ssm get-parameter --name /adlytics/test/api/conversation-function-url"
        " --profile my-profile --region test-region-1 --query Parameter.Name --output text",
    ]


def test_deploy_all_keeps_the_origins_attached_when_both_api_parameters_exist(
    customer: CustomerWorld,
) -> None:
    """Paired control: an existing deployment deploys once, with both origins on throughout."""
    customer.stack("Adlytics-Api-test", "UPDATE_COMPLETE")
    customer.parameter("/adlytics/test/api/id", "/adlytics/test/api/id")
    customer.parameter(
        "/adlytics/test/api/conversation-function-url",
        "/adlytics/test/api/conversation-function-url",
    )

    result = customer.run("deploy-all.sh", ENV="test", APPROVE="1")

    assert result.returncode == 0, result.stderr
    assert customer.calls("npx cdk deploy") == [f"{_DEPLOY_ALL} api=true conversation=true"]


def test_deploy_all_stops_on_any_parameter_read_error_other_than_not_found(
    customer: CustomerWorld,
) -> None:
    customer.parameter("/adlytics/test/api/id", "DENIED")

    result = customer.run("deploy-all.sh", ENV="test", APPROVE="1")

    assert result.returncode == 1
    assert result.stderr.splitlines()[-1] == (
        "ERROR: could not read /adlytics/test/api/id: An error occurred (AccessDeniedException)"
        " when calling the GetParameter operation: fixture"
    )
    assert customer.calls("npx") == []


_POOL = "/adlytics/test/auth/user-pool-id"


def test_create_user_for_an_already_invited_address_sends_nothing_and_succeeds(
    customer: CustomerWorld,
) -> None:
    customer.parameter(_POOL, "test-region-1_fixture")
    (customer.state / "cognito-error").write_text(
        "An error occurred (UsernameExistsException) when calling the AdminCreateUser operation:"
        " User account already exists\n"
    )

    result = customer.run("create-user.sh", "test", "someone@example.com")

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == (
        "someone@example.com is already invited; no new invite sent."
    )
    assert len(customer.calls("aws cognito-idp")) == 1
    assert customer.calls("aws cognito-idp admin-create-user") == customer.calls("aws cognito-idp")
    assert not any("RESEND" in call for call in customer.calls("aws"))


def test_create_user_invites_a_new_address_and_fails_on_any_other_error(
    customer: CustomerWorld,
) -> None:
    """Paired controls: a new address is invited; a different refusal still fails the run."""
    customer.parameter(_POOL, "test-region-1_fixture")

    invited = customer.run("create-user.sh", "test", "someone@example.com")
    (customer.state / "cognito-error").write_text(
        "An error occurred (AccessDeniedException) when calling the AdminCreateUser operation:"
        " fixture\n"
    )
    denied = customer.run("create-user.sh", "test", "someone@example.com")

    assert invited.returncode == 0, invited.stderr
    assert "Done. someone@example.com is invited with status FORCE_CHANGE_PASSWORD." in (
        invited.stdout.splitlines()
    )
    assert denied.returncode == 1
    assert denied.stderr.splitlines()[-1] == (
        "An error occurred (AccessDeniedException) when calling the AdminCreateUser operation:"
        " fixture"
    )
