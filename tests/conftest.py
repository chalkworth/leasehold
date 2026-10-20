"""Fixtures for leasehold's machine-agent tests."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest
from support import FACTS, FakeLoad, machine_config, outer_lease

from leasehold.agent import MachineAgent
from leasehold.server import Daemon


@pytest.fixture(autouse=True)
def _outside_any_lease(monkeypatch):
    """Each test starts with no lease's variables in its environment, even when the suite runs
    inside `leasehold hold`."""
    for name in [n for n in os.environ if outer_lease(n)]:
        monkeypatch.delenv(name)


@pytest.fixture
def sock_dir():
    """A short directory of its own for a unix socket (AF_UNIX paths are capped near 108
    bytes, which a deep tmp_path can pass); removed afterwards."""
    path = Path(tempfile.mkdtemp(prefix="lh-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def daemon(tmp_path, sock_dir):
    """A machine agent on the small `machine_config` machine, serving on a real unix socket."""
    config = machine_config(tmp_path, socket=str(sock_dir / "s.sock"))
    d = Daemon(config, MachineAgent(config, facts=FACTS, load=FakeLoad()))
    d.start()
    try:
        yield d
    finally:
        d.stop()


def pytest_sessionfinish(session, exitstatus):
    """A run that collects no tests passes: tests are added where a contract needs one, so an
    empty selection is not a failure."""
    if exitstatus == pytest.ExitCode.NO_TESTS_COLLECTED:
        session.exitstatus = pytest.ExitCode.OK
