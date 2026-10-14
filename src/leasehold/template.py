"""The commented machine config `leasehold init` writes, sized for a machine.

The text is package data, so an installed wheel has it: `templates/machine.toml` for the
machine's daemon, with the four default pools, and `templates/user.toml` for a user's own, with
two (`work` and `background`), no lending, and the memory guard and containment spelled out.
`deploy/machine.toml` is the system template rendered for the example machine (`EXAMPLE`), and
a test keeps the two the same.
"""

from __future__ import annotations

import os
import socket
from importlib.resources import files
from string import Template

from leasehold.machine import default_pools
from leasehold.paths import SYSTEM_LEDGER_DIR, SYSTEM_SOCKET, Mode

#: What `deploy/machine.toml` is rendered from.
EXAMPLE = {
    "name": "buildbox",
    "mode": "system",
    "slots": 12,
    "cpus": 16,
    "memory": "48g",
    "about": "An example: a 16-thread, 64 GB box shared by its owner, a CI runner and lent work.",
}


def kept_for_you(cpus: int) -> int:
    """How many CPUs a user's own config leaves out of the slots, for the person at the
    machine: 2 from 8 CPUs up, 1 from 3, none below."""
    return 2 if cpus >= 8 else 1 if cpus >= 3 else 0


def sized(mode: Mode, facts: dict) -> str:
    """The config `init` (and `setup`) writes for a machine of these `facts` (`facts.probe`):
    one slot per CPU, less `kept_for_you` for a user's own daemon, and at most three quarters of
    the memory to any one lease."""
    cpus = facts.get("cpus_logical") or os.cpu_count() or 1
    memory = facts.get("memory_bytes") or 0
    kept = kept_for_you(cpus) if mode == "user" else 0
    slots = max(1, cpus - kept)
    for_you = f"; {kept} CPUs are kept for you" if kept else ""
    return render(
        name=socket.gethostname().split(".")[0].lower() or "machine",
        mode=mode,
        slots=slots,
        cpus=slots,
        memory=f"{max(1, memory * 3 // 4 // 2**30)}g",
        about=f"Sized for {cpus} CPUs and {memory / 2**30:.0f} GiB: one slot per CPU{for_you}, "
        "and at most three quarters of the memory to any one lease.",
    )


def _paths(mode: Mode) -> str:
    if mode == "system":
        return (
            f'socket = "{SYSTEM_SOCKET}"\n'
            f'ledger_dir = "{SYSTEM_LEDGER_DIR}"\n'
            'socket_group = "leasehold"  # its members may use the socket (mode 0660)'
        )
    return (
        "# socket and ledger_dir: by default $$XDG_RUNTIME_DIR/leasehold/leasehold.sock and\n"
        "# $$XDG_STATE_HOME/leasehold; the socket is yours alone (mode 0600)"
    )


def render(*, name: str, mode: Mode, slots: int, cpus: float, memory: str, about: str) -> str:
    """The config text for a machine with `slots` slots, `cpus` CPUs and `memory` to lend one
    lease (a size such as `48g`)."""
    pools = default_pools(slots)
    if mode == "user":
        reserved = pools["work"].reserved
        text = (files("leasehold") / "templates" / "user.toml").read_text()
        return Template(text).substitute(
            name=name,
            about=about,
            slots=slots,
            cpus=f"{cpus:g}",
            memory=memory,
            work_reserved=reserved,
            background_max=max(1, slots - reserved),
        )
    text = (files("leasehold") / "templates" / "machine.toml").read_text()
    return Template(Template(text).safe_substitute(paths=_paths(mode))).substitute(
        name=name,
        mode=mode,
        about=about,
        slots=slots,
        cpus=f"{cpus:g}",
        memory=memory,
        work_reserved=pools["work"].reserved,
        work_max=pools["work"].max,
        external_max=pools["external"].max,
        lend_max=pools["lend"].max,
    )
