"""Fixtures for leasehold's machine-agent tests."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest
from support import FACTS, FakeLoad, machine_config

from leasehold.agent import MachineAgent
from leasehold.server import Daemon


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
