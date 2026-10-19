# SPDX-License-Identifier: MIT-0
"""Live probes fail fast on a dead connection: per-call and per-run wall-clock deadlines."""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections.abc import Generator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "validation"))
sys.path.insert(0, str(_ROOT))

from validation import probe_deadlines as deadlines  # noqa: E402

pytestmark = [pytest.mark.unit, pytest.mark.covers("ADR-086")]


@pytest.fixture
def silent_server() -> Generator[tuple[str, int]]:
    """Serve TCP that accepts and then never sends a byte: a dead upstream."""
    server = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []

    def accept() -> None:
        try:
            while True:
                connection, _ = server.accept()
                held.append(connection)
        except OSError:
            return

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[:2]
    server.close()
    for connection in held:
        connection.close()


def _read_forever(address: tuple[str, int]) -> bytes:
    """Read from the silent server with no socket timeout, as a stuck client would."""
    with socket.create_connection(address) as client:
        client.sendall(b"GET / HTTP/1.1\r\n\r\n")
        return client.recv(1)


def test_a_call_deadline_fires_on_a_socket_that_never_responds(
    silent_server: tuple[str, int],
) -> None:
    started = time.monotonic()
    with (
        pytest.raises(
            deadlines.ProbeDeadlineError, match=r"dead upstream passed its 0\.5 s deadline"
        ),
        deadlines.within(0.5, "dead upstream"),
    ):
        _read_forever(silent_server)
    assert time.monotonic() - started < 3
    assert deadlines.DEADLINES.open == []


def test_a_call_inside_its_deadline_completes_and_disarms() -> None:
    # Positive control: work that finishes in time returns normally and leaves no alarm armed.
    with deadlines.within(2, "quick call"):
        time.sleep(0.05)
    time.sleep(0.1)
    assert deadlines.DEADLINES.open == []


def test_nested_deadlines_fire_the_earliest_and_name_it(silent_server: tuple[str, int]) -> None:
    with (
        pytest.raises(deadlines.ProbeDeadlineError, match=r"^one call passed"),
        deadlines.within(30, "whole run"),
        deadlines.within(0.3, "one call"),
    ):
        _read_forever(silent_server)
    # The run deadline fires too when it is the earlier one.
    with (
        pytest.raises(deadlines.ProbeDeadlineError, match=r"^whole run passed"),
        deadlines.within(0.3, "whole run"),
        deadlines.within(30, "one call"),
    ):
        _read_forever(silent_server)


def test_boto_clients_carry_explicit_timeouts_and_bounded_retries() -> None:
    config = deadlines.BOTO_CONFIG
    fields: dict[str, object] = {
        name: getattr(config, name) for name in ("connect_timeout", "read_timeout", "retries")
    }
    assert fields == {
        "connect_timeout": 10,
        "read_timeout": 30,
        "retries": {"max_attempts": 3, "mode": "standard"},
    }


def test_a_bounded_client_wraps_every_direct_call(silent_server: tuple[str, int]) -> None:
    class Stuck:
        def describe(self) -> bytes:
            return _read_forever(silent_server)

        def quick(self) -> str:
            return "ok"

    class Session:
        def client(self, _service: str, **_options: object) -> Stuck:
            return Stuck()

    client = deadlines.bounded_client(Session(), "sqs", seconds=0.3)
    assert client.quick() == "ok"  # positive control
    with pytest.raises(deadlines.ProbeDeadlineError, match=r"^sqs\.describe passed"):
        client.describe()
