"""Holder identity (leasehold.process): a live process matches its own birth token, a dead one or
a reused pid does not, and a zombie is not live."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from leasehold.process import pid_namespace, process_birth, process_matches

linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads procfs")


@linux_only
def test_this_process_matches_its_own_birth():
    birth = process_birth()
    assert birth is not None and birth.isdigit()
    assert birth == process_birth(os.getpid())
    assert process_matches(os.getpid(), birth)
    assert not process_matches(os.getpid(), str(int(birth) + 1))  # a reused pid


@linux_only
def test_a_dead_process_has_no_birth_and_never_matches():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    birth = process_birth(proc.pid)
    assert birth is not None and process_matches(proc.pid, birth)
    proc.kill()
    proc.wait()
    assert process_birth(proc.pid) is None
    assert not process_matches(proc.pid, birth)


@linux_only
def test_a_zombie_is_not_live():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    birth = process_birth(proc.pid)
    try:
        for _ in range(500):  # exited, not yet reaped
            stat = Path(f"/proc/{proc.pid}/stat").read_text()
            if stat[stat.rfind(")") + 2] == "Z":
                break
            time.sleep(0.01)
        assert birth is not None and not process_matches(proc.pid, birth)
    finally:
        proc.wait()


def test_the_pid_namespace_is_a_link_or_none():
    ns = pid_namespace()
    assert ns is None or ns.startswith("pid:[")
