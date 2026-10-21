"""`leasehold hold`'s exit codes for each way its wait in line ends (`leasehold.exits`)."""

from __future__ import annotations

import pytest
from support import cli

import leasehold

#: case: (hold's options, whether a daemon is there, exit code, what it says on stderr). The one
#: `external` slot is held throughout.
CASES = {
    "granted": ([], True, 3, ""),
    "busy": (["--kind", "external", "--timeout", "0.3"], True, 75, "gave up after 0.3s in line"),
    "refused": (["--pool", "nope"], True, 77, "refused: there is no pool 'nope'"),
    "no daemon": ([], False, 0, "running without a lease"),
    "no daemon, fail": (["--if-unavailable", "fail"], False, 69, "is a daemon running?"),
}


@pytest.mark.parametrize(("options", "there", "code", "says"), CASES.values(), ids=CASES.keys())
def test_how_a_wait_ends(daemon, sock_dir, options, there, code, says):
    sock = daemon.path if there else sock_dir / "gone.sock"
    with leasehold.lease(kind="external", socket=daemon.path):
        done = cli(sock, "hold", *options, "--", "sh", "-c", f"exit {code if code == 3 else 0}")
    assert done.returncode == code
    assert says in done.stderr
