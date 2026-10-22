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

from leasehold.commands import HEAVY_WEIGHT
from leasehold.machine import default_pools
from leasehold.paths import SYSTEM_LEDGER_DIR, SYSTEM_SOCKET, Mode

#: What 0.2's `init` wrote for a user's own daemon, by release, to know such a config when it
#: is found unedited (`leasehold.upgrade`).
USER_0_2 = {"0.2.0": "user-0.2.0.toml", "0.2.1": "user-0.2.1.toml"}

#: What `deploy/machine.toml` is rendered from.
EXAMPLE = {
    "name": "buildbox",
    "mode": "system",
    "slots": 12,
    "cpus": 16,
    "memory": "48g",
    "about": "An example: a 16-thread, 64 GB box shared by its owner, a CI runner and lent work.",
}


def weights(slots: int) -> tuple[int, int]:
    """The (heavy, light) weights that let two builds or test runs run at once on `slots`
    slots: half the slots from 4 up, all of them below (one at a time, at full width, rather
    than two of one job each). Light is half of heavy, rounded up. A user's new config sets
    them (`[commands] heavy_weight`, `light_weight`) where the built-in 4 lets only one run at
    a time: on 6 slots, two at a time at weight 3 finished sooner than one at weight 4 (the
    `defaults-h1` bench gate, docs/bench.md)."""
    heavy = slots // 2 if slots >= 4 else max(1, slots)
    return heavy, max(1, (heavy + 1) // 2)


def kept_for_you(cpus: int) -> int:
    """How many CPUs a user's own config leaves out of the slots, for the person at the
    machine: 2 from 8 CPUs up, 1 from 3, none below."""
    return 2 if cpus >= 8 else 1 if cpus >= 3 else 0


def sized(mode: Mode, facts: dict) -> str:
    """The config `init` (and `setup`) writes for a machine of these `facts` (`facts.probe`):
    one slot per CPU, less `kept_for_you` for a user's own daemon, and at most three quarters of
    the memory to any one lease. A user's own also counts memory (`[memory]`), and sets the
    `weights` that run two builds at once."""
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


def _two_at_once(slots: int, *, set_them: bool) -> str:
    """The `[commands]` lines that run two heavy commands at once on `slots` slots: set, or
    (`set_them` false) offered as comments. Empty where the built-in weights already do."""
    heavy, light = weights(slots)
    if heavy >= min(HEAVY_WEIGHT, slots):
        return ""
    if not set_them:
        return (
            f"# To run two at once instead, each with fewer jobs (opt-in):\n"
            f"# heavy_weight = {heavy}\n"
            f"# light_weight = {light}\n"
            "# A command that declares no memory then counts its weight times the budget "
            "over the\n# slots, which may be less than it uses: declare memory for memory-heavy "
            "work (a rule's\n# `memory`, or `hold --memory`).\n"
        )
    return (
        f"heavy_weight = {heavy}\n"
        f"light_weight = {light}\n"
        "# A command that declares no memory counts its weight times the budget over the slots,\n"
        "# which may be less than it uses: declare memory for memory-heavy work (a rule's\n"
        "# `memory`, or `hold --memory`).\n"
    )


def render(
    *, name: str, mode: Mode, slots: int, cpus: float, memory: str, about: str,
    two_at_once: bool = True,
) -> str:  # fmt: skip
    """The config text for a machine with `slots` slots, `cpus` CPUs and `memory` to lend one
    lease (a size such as `48g`). A user's config sets the weights that run two heavy commands
    at once where the built-in ones run one; with `two_at_once` false it offers them as
    comments instead and keeps the built-in weights."""
    pools = default_pools(slots)
    if mode == "user":
        reserved = pools["work"].reserved
        text = (files("leasehold") / "templates" / "user.toml").read_text()
        two = _two_at_once(slots, set_them=two_at_once)
        at_once = slots // min(HEAVY_WEIGHT, slots)
        if at_once > 1:
            note = f"{at_once} of them run at once."
        elif two and two_at_once:
            note = (
                "4 would run one of them at a time; the two lines below run two at once,\n"
                "# each with fewer jobs, which finished sooner in the bench (docs/bench.md).\n"
                "# Delete them for one at a time."
            )
        else:
            note = "one of them runs at a time."
        return Template(text).substitute(
            name=name,
            about=about,
            slots=slots,
            cpus=f"{cpus:g}",
            memory=memory,
            work_reserved=reserved,
            background_max=max(1, slots - reserved),
            heavy_note=f"On {slots} slots, {note}",
            opt_in=two,
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


def render_user_0_2(
    *, name: str, slots: int, cpus: float, memory: str, about: str, release: str = "0.2.1"
) -> str:
    """The config `release` (0.2.0 or 0.2.1)'s `init` wrote for a user's own daemon with these
    values."""
    reserved = default_pools(slots)["work"].reserved
    text = (files("leasehold") / "templates" / USER_0_2[release]).read_text()
    return Template(text).substitute(
        name=name,
        about=about,
        slots=slots,
        cpus=f"{cpus:g}",
        memory=memory,
        work_reserved=reserved,
        background_max=max(1, slots - reserved),
    )
