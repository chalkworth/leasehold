"""`leasehold demo` (leasehold.demo): the made-up machine fills up, makes work wait and says
why, without touching the real one."""

from __future__ import annotations

import time

from leasehold.client import Client
from leasehold.demo import Demo


def test_the_demo_machine_gets_busy_on_its_own_socket():
    demo = Demo(seed=1)
    demo.start()
    try:
        assert demo.socket.parent == demo.directory and demo.directory.name.startswith(
            "leasehold-demo-"
        )
        client = Client(demo.socket)
        deadline = time.monotonic() + 30
        status = client.status()
        while time.monotonic() < deadline and not (status["leases"] and status["queue"]):
            time.sleep(0.5)
            status = client.status()
        assert status["machine"]["name"] == "buildbox" and status["slots"] == 12
        assert status["leases"], "nobody got a lease"
        assert status["queue"] and all(q["blocked"] for q in status["queue"])
        assert {lease["user"] for lease in status["leases"]} <= {"alice", "bob", "runner", "dave"}
    finally:
        demo.stop()
    assert not demo.directory.exists()
