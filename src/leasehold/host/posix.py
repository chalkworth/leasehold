"""The fallback host, for a system leasehold has no port for yet (macOS among them), and the base
of the Linux host: what POSIX alone gives.

A command starts in a process group of its own, and that group is what `pause`, `resume` and
`kill` signal: a member that left the group (a daemon that called `setsid`) is not reached, so
they are `polled`, never `exact`. A tree's keeper is never paused: a group the keeper is in
is not stopped.

Everything else is not supported here, and `support` says why: there is no kernel group to put
a tree in (`start` runs the command in its process group only, `adopt` and `join` move
nothing), no portable way to list a tree's processes, read their memory or the machine's
pressure, tell a memory kill from any other, or cap a command's memory.

macOS has no limit on a tree of processes' memory that a user can set without root
(`RLIMIT_AS` bounds address space, which every process reserves far more of than it uses), so
its cap will be polled, by the macOS port; until then a cap asked for there is not enforced,
and `hold`, `doctor` and the status say so. For the same reason it adopts nothing: a lease taken
for an agent's command there only counts it, and `support("adopt")` says why."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Collection, Mapping

from leasehold.host import (
    OPERATIONS,
    Adoption,
    Done,
    Ending,
    Footprint,
    Mark,
    Pressure,
    Proc,
    SessionRef,
    Started,
    Support,
    Tree,
    TreeSpec,
    group_alive,
    signal_group,
)

_GROUP = "signals to the tree's process group: a member that left the group is not reached"


class Posix:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self._env = os.environ if env is None else env

    # --- what works here ---------------------------------------------------------------------

    def _unsupported(self, operation: str) -> str:
        darwin = sys.platform == "darwin"
        if operation == "start":
            return f"no kernel group for a command on {sys.platform}: it runs in a process group"
        if operation == "memory_hard":
            if darwin:
                return (
                    "macOS has no per-command memory limit without root, and leasehold's "
                    "polled cap for macOS is not built yet"
                )
            return f"leasehold cannot cap a command's memory on {sys.platform} yet"
        if operation == "adopt":
            if darwin:
                return (
                    "macOS has no per-command group without root, and leasehold's polled tree "
                    "for macOS is not built yet: an agent's commands are leased, not contained"
                )
            return f"leasehold cannot contain a running command on {sys.platform} yet"
        if operation == "ended":
            return f"leasehold cannot tell memory kills apart on {sys.platform} yet"
        what = {
            "members": "list a tree's processes",
            "footprint": "read a tree's memory",
            "pressure": "read the machine's memory pressure",
            "session_processes": "find a session's processes",
        }[operation]
        return f"leasehold cannot {what} on {sys.platform} yet"

    def support(self, operation: str) -> Support:
        if operation in ("pause", "resume", "kill"):
            return Support("polled", via=_GROUP)
        return Support("none", self._unsupported(operation), via=None)

    def capabilities(self) -> dict[str, Support]:
        return {op: self.support(op) for op in OPERATIONS}

    # --- trees -------------------------------------------------------------------------------

    def _fence(self, pid: int) -> str | None:
        return None

    def _tree(self, child: subprocess.Popen, popen: Mapping) -> Tree:
        grouped = popen.get("start_new_session") or popen.get("process_group") == 0
        return Tree(
            root=child.pid,
            fence=self._fence(child.pid),
            pgid=child.pid if grouped else None,
            keeper=os.getpid(),
        )

    def start(
        self, argv, spec: TreeSpec | None, *, cancel_fd: int | None = None, **popen
    ) -> Started:
        """Start `argv` (with `subprocess.Popen`'s `popen` arguments) in its process group:
        no kernel group can be made here, which `uncontained` says when `spec` asked for one."""
        mark = self.mark()
        child = subprocess.Popen(list(argv), **popen)
        why = None if spec is None else self.support("start").reason
        return Started(self, child, self._tree(child, popen), mark, why)

    def adopt(self, pids: list[int], spec: TreeSpec) -> Adoption:
        return Adoption("none", reason=self.support("adopt").reason)

    def join(self, tree: Tree, pids: list[int]) -> Done:
        return Done("none", self.support("adopt").reason)

    def resize(self, tree: Tree, spec: TreeSpec) -> Done:
        return Done("none", self.support("start").reason)

    def release(self, tree: Tree) -> Done:
        return Done("none", self.support("start").reason)

    def members(self, tree: Tree) -> list[Proc]:
        return []

    def footprint(self, tree: Tree) -> Footprint:
        return Footprint(reason=self.support("footprint").reason)

    def pressure(self) -> Pressure:
        return Pressure(reason=self.support("pressure").reason)

    def session_processes(self, session: SessionRef) -> list[Proc]:
        return []

    def named(self, names: Collection[str]) -> list[Proc]:
        """The user's live processes whose program is one of `names`: none can be listed here
        (`support("session_processes")` says why)."""
        return []

    def wakeup_fd(self) -> int | None:
        """A file descriptor that turns readable when a tree or the machine's memory changes,
        so that a loop can look again before its next tick; None: there is none here."""
        return None

    # --- acting on a tree --------------------------------------------------------------------

    def _group(self, tree: Tree) -> int | None:
        """The process group `pause` may signal: the tree's own, unless its keeper is in it."""
        if tree.pgid is None:
            return None
        if tree.keeper is not None:
            try:
                if os.getpgid(tree.keeper) == tree.pgid:
                    return None
            except ProcessLookupError:
                pass
        return tree.pgid

    def _signal(self, tree: Tree, sig: int, how: str) -> Done:
        pgid = self._group(tree)
        if pgid is None:
            return Done("none", "the tree has no process group of its own apart from its keeper")
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return Done("none", "the tree has ended", empty=True)
        except PermissionError as exc:
            return Done("none", f"cannot signal process group {pgid}: {exc}")
        return Done(how)

    def pause(self, tree: Tree) -> Done:
        return self._signal(tree, signal.SIGSTOP, "sigstop")

    def resume(self, tree: Tree) -> Done:
        return self._signal(tree, signal.SIGCONT, "sigcont")

    def kill(self, tree: Tree, grace_s: float) -> Done:
        if tree.pgid is None:
            return Done("none", "the tree has no process group of its own")
        if not group_alive(tree.pgid):
            return Done("signal", empty=True)
        signal_group(tree.pgid, signal.SIGTERM)
        signal_group(tree.pgid, signal.SIGCONT)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and group_alive(tree.pgid):
            time.sleep(0.1)
        signal_group(tree.pgid, signal.SIGKILL)
        return Done("signal")

    # --- how a command ended -----------------------------------------------------------------

    def mark(self) -> Mark:
        return Mark()

    def ended(
        self, status: int, since: Mark, *, tree: Tree | None = None, counted: int | None = None
    ) -> Ending:
        return Ending(reason=self.support("ended").reason)

    def memory_max(self, pid: int) -> int | None:
        return None
