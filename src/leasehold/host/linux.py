"""The Linux host: systemd user scopes for trees (`leasehold.host.systemd`), cgroup v2 to read
and freeze them, and procfs for processes and the machine's memory.

**Trees.** `start` runs a command in a transient scope of the user manager sized to its lease,
where `support("start")` says one can be made for this caller (`systemd.probe`); otherwise in
its process group only. `adopt` moves running processes into a new scope, never from under a
`memory.max` they already run under, and raises a negative `oom_score_adj` they inherited to 0,
as `systemd.spawn` does for a command it starts. An uncapped scope keeps running when the OOM
killer ends one of its processes; a capped one is ended whole (`memory.oom.group`).

**A hard cap** is `MemoryMax` on the scope with `MemorySwapMax=0`, so that a runaway is killed
rather than pushed to swap, and `memory.oom.group`, so that the whole command dies together. It
holds only for a command in a scope. Whether the OOM killer ended a capped command is what
systemd says of its scope (`Result=oom-kill`).

**Measuring.** A tree in a cgroup is read from its files (`memory.current` less the file cache
the kernel reclaims first, `memory.peak`, `memory.stat`, `memory.swap.*`, `memory.events`,
`cpu.stat`), its members from `cgroup.procs`, each with its resident and swapped memory
(`VmRSS` and `VmSwap`). A tree without one is its root's descendants, found through procfs
(`/proc/<pid>/task/*/children`), its footprint the sum of theirs (`polled`). The machine's
pressure is `MemAvailable` and swap from `/proc/meminfo`, PSI from `/proc/pressure/memory`, and
pages swapped in from `/proc/vmstat`. A process's fence is its start time (field 22 of
`/proc/<pid>/stat`).

**Pausing.** A tree in a cgroup is frozen (`cgroup.freeze`), which its parent cannot see. A tree
without one gets SIGSTOP, to its process group unless its keeper is in it, and to each member,
until a scan finds none still running.

**How a command ended.** The kernel counts every process its OOM killer ends (`oom_kill` in
`/proc/vmstat`), whether the machine or a cgroup's limit ran out of memory. A command that ended
by SIGKILL while that count rose most likely died for memory; the count is for the whole
machine, so the verdict is `likely`, never `certain`. It is certain when the tree's own scope
counted the kill (`memory.events`, read while the scope lived) or systemd says the OOM killer
ended its capped scope. A process's hard memory limit is the lowest `memory.max` on its cgroup
and those above it (`leasehold.cgroup`).

**Sessions.** A `leasehold run` session's processes are its slice's. Any other session's are the
agent's descendants, and the user's processes whose environment names the session
(`LEASEHOLD_SESSION`), which finds what was started and then left to run on its own. An agent is
found by name (`named`) among the user's processes, by the kernel's name for it (`comm`). One
scan of the user's processes' environments serves every session asked about within a second.
"""

from __future__ import annotations

import contextlib
import errno
import os
import signal
import time
from collections.abc import Collection, Mapping
from dataclasses import replace
from pathlib import Path, PurePosixPath

from leasehold import cgroup
from leasehold.contain import lease_slice, root_slice_cgroup
from leasehold.facts import meminfo
from leasehold.host import (
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
    sigkilled,
    signal_group,
    systemd,
)
from leasehold.host.posix import Posix

#: The variable that names the session a process belongs to.
SESSION_ENV = b"LEASEHOLD_SESSION"
#: How long one scan of the user's processes' environments serves `session_processes`.
TAGS_S = 1.0
#: Errors that say a cgroup has gone, which no one needs to hear about.
_GONE = (errno.ENOENT, errno.ENODEV, errno.ENOTDIR)


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _number(text: str | None) -> int | None:
    text = (text or "").strip()
    return int(text) if text.isdigit() else None


def _fields(text: str | None) -> dict[str, int]:
    """A file of `name value` lines as a mapping; lines that are not one are skipped."""
    out = {}
    for line in (text or "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdigit():
            out[name] = int(value)
    return out


def oom_kills(proc: Path = Path("/proc")) -> int | None:
    """How many processes the OOM killer has ended since boot; None where it cannot be read."""
    return _fields(_read(proc / "vmstat")).get("oom_kill")


def _psi(text: str | None) -> dict[str, float]:
    """`some` and `full` `avg10` of a PSI file, as far as they can be read."""
    out = {}
    for line in (text or "").splitlines():
        kind, *parts = line.split()
        for part in parts:
            name, _, value = part.partition("=")
            if name == "avg10":
                with contextlib.suppress(ValueError):
                    out[kind] = float(value)
    return out


def _cap_or_kernel(tree: Tree) -> str:
    """Whose limit an OOM kill the tree's scope counted was: `cap` when its own cap was the
    tightest on it (to within a page, which the kernel rounds it to), else `kernel`."""
    if tree.cap_bytes is None:
        return "kernel"
    page = os.sysconf("SC_PAGE_SIZE")
    above = tree.limit_bytes
    return "cap" if above is None or above + page > tree.cap_bytes else "kernel"


class Linux(Posix):
    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        proc: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
    ) -> None:
        super().__init__(env)
        self._proc = proc
        self._cgroup_root = cgroup_root
        #: Why no scope can be made for this caller ("" when one can; None: not asked yet).
        self._no_scope: str | None = None
        #: (when, pages swapped in) at the last `pressure`.
        self._swapins: tuple[float, int] | None = None
        #: (when, pids by session) at the last scan of the user's processes' environments.
        self._tags: tuple[float, dict[str, list[int]]] | None = None
        #: When the machine booted, in wall-clock seconds (None: not read yet, or unreadable).
        self._boot: float | None = None

    # --- what works here ---------------------------------------------------------------------

    def _v2(self) -> str | None:
        """Why there is no cgroup v2 here; None when there is."""
        if not (self._cgroup_root / "cgroup.controllers").is_file():
            return f"no cgroup v2 at {self._cgroup_root}"
        return None

    def support(self, operation: str) -> Support:
        if operation == "start":
            if self._no_scope is None:
                self._no_scope = (
                    systemd.probe(
                        env=self._env,
                        cgroup_root=self._cgroup_root,
                        self_cgroup=self._proc / "self" / "cgroup",
                    )
                    or ""
                )
            if self._no_scope:
                return Support("none", self._no_scope, via="process group")
            return Support("exact", via="systemd")
        if operation in ("adopt", "memory_hard"):
            why = self._v2()
            if why is not None:
                return Support("none", why)
            via = "systemd scope, over D-Bus" if operation == "adopt" else "MemoryMax"
            return Support("exact", via=via)
        if operation == "members":
            return Support("exact", via="cgroup.procs, or procfs for a tree without a cgroup")
        if operation == "footprint":
            return Support("exact", via="cgroup memory files, or procfs (polled) without one")
        if operation == "pressure":
            if not (self._proc / "meminfo").is_file():
                return Support("none", f"cannot read {self._proc / 'meminfo'}")
            psi = (self._proc / "pressure" / "memory").is_file()
            return Support("exact", via="MemAvailable and PSI" if psi else "MemAvailable, no PSI")
        if operation in ("pause", "resume"):
            via = "cgroup.freeze, or SIGSTOP and SIGCONT to each process without a cgroup"
            return Support("exact", via=via)
        if operation == "kill":
            return Support("exact", via="systemctl kill, or signals to each process")
        if operation == "ended":
            if oom_kills(self._proc) is None:
                return Support("none", "the kernel's OOM kill count cannot be read")
            return Support("exact", via="oom_kill in /proc/vmstat, and the scope's own count")
        if operation == "session_processes":
            return Support("exact", via="the session's slice, or descendants and environment")
        return super().support(operation)

    # --- processes ---------------------------------------------------------------------------

    def _stat(self, pid: int) -> tuple[str, int, str, str] | None:
        """(state, parent, start time, name) of process `pid`, from procfs."""
        try:
            raw = (self._proc / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
            fields = raw[raw.rindex(")") + 2 :].split()
            return fields[0], int(fields[1]), fields[19], raw[raw.index("(") + 1 : raw.rindex(")")]
        except (OSError, ValueError, IndexError):
            return None

    def _fence(self, pid: int) -> str | None:
        stat = self._stat(pid)
        return None if stat is None else stat[2]

    def _started(self, ticks: str) -> float | None:
        """A start time in clock ticks since boot (a fence), in wall-clock seconds."""
        if self._boot is None:
            for line in (_read(self._proc / "stat") or "").splitlines():
                if line.startswith("btime "):
                    with contextlib.suppress(ValueError):
                        self._boot = float(line.split()[1])
        if self._boot is None or not ticks.isdigit():
            return None
        return self._boot + int(ticks) / os.sysconf("SC_CLK_TCK")

    def _proc_of(self, pid: int, *, role: str | None = None, basis: str | None = None):
        """Process `pid` as a `Proc`, with its resident and swapped memory; None when it has
        gone, is a zombie, or has no memory of its own (a kernel thread)."""
        stat = self._stat(pid)
        status = _read(self._proc / str(pid) / "status") or ""
        if stat is None or stat[0] == "Z":
            return None
        name = None
        kib: dict[str, int] = {}
        for line in status.splitlines():
            key, _, value = line.partition(":")
            if key == "Name":
                name = value.strip()
            elif key in ("VmRSS", "VmSwap", "RssAnon") and value.split()[:1] != []:
                number = value.split()[0]
                if number.isdigit():
                    kib[key] = int(number)
        if name is None or ("VmRSS" not in kib and "VmSwap" not in kib):
            return None
        held = (kib.get("VmRSS", 0) + kib.get("VmSwap", 0)) << 10
        anon, swap = kib.get("RssAnon"), kib.get("VmSwap")
        return Proc(
            pid, stat[2], name, held, stat[1], self._fence(stat[1]), role=role, basis=basis,
            anon=None if anon is None else anon << 10, swap=None if swap is None else swap << 10,
            started=self._started(stat[2]),
        )  # fmt: skip

    def _children(self, pid: int) -> list[int]:
        out = []
        try:
            tasks = list((self._proc / str(pid) / "task").iterdir())
        except OSError:
            return out
        for task in tasks:
            with contextlib.suppress(OSError, ValueError):
                out += [int(w) for w in (task / "children").read_text().split()]
        return out

    def _descendants(self, pid: int, depth: int = 64) -> list[int]:
        """`pid`'s descendants, nearest first."""
        found, level = [], [pid]
        for _ in range(depth):
            level = [c for p in level for c in self._children(p)]
            if not level:
                break
            found += level
        return found

    def _pids(self, tree: Tree) -> list[int]:
        """The pids of the tree's processes now: its cgroup's, with those of the cgroups under
        it, or else its root (while its fence holds) and the root's descendants."""
        if tree.group is not None:
            base = self._cgroup_root / tree.group.lstrip("/")
            files = [base / "cgroup.procs"]
            with contextlib.suppress(OSError):
                files += sorted(base.rglob("*/cgroup.procs"))
            words = [w for f in files for w in (_read(f) or "").split()]
            return [int(w) for w in words if w.isdigit()]
        if tree.fence is not None and self._fence(tree.root) != tree.fence:
            return []
        return [tree.root, *self._descendants(tree.root)]

    def members(self, tree: Tree) -> list[Proc]:
        """The tree's live processes, largest first. A process that cannot be read is left
        out."""
        found = [p for p in map(self._proc_of, self._pids(tree)) if p is not None]
        return sorted(found, key=lambda p: -(p.bytes or 0))

    def footprint(self, tree: Tree) -> Footprint:
        if tree.group is None:
            found = self.members(tree)
            if not found:
                return Footprint(reason="no process of the tree is left")
            total = sum(p.bytes or 0 for p in found)
            anon = [p.anon for p in found if p.anon is not None]
            swap = [p.swap for p in found if p.swap is not None]
            return Footprint(
                "polled",
                current=total,
                peak_low=total,
                anon=sum(anon) if anon else None,
                swap=sum(swap) if swap else None,
                largest=found[0],
            )
        base = self._cgroup_root / tree.group.lstrip("/")
        current = _number(_read(base / "memory.current"))
        stat = _fields(_read(base / "memory.stat"))
        events = _fields(_read(base / "memory.events"))
        swap_now = _number(_read(base / "memory.swap.current"))
        swap = _number(_read(base / "memory.swap.peak"))
        peak = _number(_read(base / "memory.peak"))
        found = self.members(tree)
        held = None
        if current is not None:
            held = max(0, current - stat.get("inactive_file", 0)) + (swap_now or 0)
        return Footprint(
            "exact" if current is not None else "none",
            None if current is not None else f"cannot read {base / 'memory.current'}",
            current=held,
            peak_low=peak,
            peak_high=peak,
            anon=stat.get("anon"),
            swap=swap if swap is not None else swap_now,
            largest=found[0] if found else None,
            oom_kills=events.get("oom_kill"),
            high_events=events.get("high"),
            cpu_usec=_fields(_read(base / "cpu.stat")).get("usage_usec"),
        )

    def pressure(self) -> Pressure:
        info = meminfo(self._proc)
        if not info:
            return Pressure(reason=self.support("pressure").reason)
        psi = _psi(_read(self._proc / "pressure" / "memory"))
        swapped_in = _fields(_read(self._proc / "vmstat")).get("pswpin")
        now, rate = time.monotonic(), None
        if swapped_in is not None:
            if self._swapins is not None and now > self._swapins[0]:
                rate = max(0, swapped_in - self._swapins[1]) / (now - self._swapins[0])
            self._swapins = (now, swapped_in)
        total = info.get("SwapTotal")
        free = info.get("SwapFree")
        return Pressure(
            "exact",
            available=info.get("MemAvailable"),
            total=info.get("MemTotal"),
            swap_used=None if total is None or free is None else total - free,
            swap_total=total,
            stall=psi.get("some"),
            stall_full=psi.get("full"),
            stall_how="psi" if psi else None,
            swapins_per_s=rate,
            compressed=info.get("Zswap"),
        )

    def session_processes(self, session: SessionRef) -> list[Proc]:
        """Every process of `session`: its slice's, where it has one; else the agent's
        descendants and the user's processes whose environment names the session. The agent
        has role `agent`, a process in a lease's scope `lease`, one found only by its
        environment `orphan`, and any other `tool`."""
        if session.slice_:
            group = f"/{root_slice_cgroup()}/{session.slice_}"
            pids = self._pids(Tree(root=session.agent_pid or 0, group=group))
            return [p for p in (self._of_session(pid, session, "slice") for pid in pids) if p]
        found: dict[int, Proc] = {}
        agent = session.agent_pid
        if agent is not None and (
            session.agent_fence is None or self._fence(agent) == session.agent_fence
        ):
            for pid in [agent, *self._descendants(agent)]:
                if (got := self._of_session(pid, session, "lineage")) is not None:
                    found[pid] = got
        for pid in self._tagged().get(session.id, ()):
            if pid not in found and (got := self._of_session(pid, session, "env")) is not None:
                found[pid] = got
        return list(found.values())

    def _own(self) -> list[int]:
        """The pids of the user's processes now."""
        uid = os.getuid()
        out = []
        with contextlib.suppress(OSError):
            for name in os.listdir(self._proc):
                if name.isdigit():
                    with contextlib.suppress(OSError):
                        if os.stat(self._proc / name).st_uid == uid:
                            out.append(int(name))
        return out

    def _tagged(self) -> dict[str, list[int]]:
        """The user's processes by the session their environment names (`LEASEHOLD_SESSION`):
        read once for every session asked about within `TAGS_S`."""
        now = time.monotonic()
        if self._tags is not None and now - self._tags[0] < TAGS_S:
            return self._tags[1]
        found: dict[str, list[int]] = {}
        prefix = SESSION_ENV + b"="
        for pid in self._own():
            try:
                environ = (self._proc / str(pid) / "environ").read_bytes()
            except OSError:
                continue
            for item in environ.split(b"\0"):
                if item.startswith(prefix):
                    found.setdefault(item[len(prefix) :].decode(errors="replace"), []).append(pid)
                    break
        self._tags = (now, found)
        return found

    def named(self, names: Collection[str]) -> list[Proc]:
        """The user's live processes whose program is one of `names`, by the kernel's name for
        it (`comm`: at most 15 characters, a script's own name for a script)."""
        wanted = set(names)
        found = []
        for pid in self._own() if wanted else []:
            comm = _read(self._proc / str(pid) / "comm")
            named = comm is not None and comm.rstrip("\n") in wanted
            if named and (got := self._proc_of(pid)) is not None:
                found.append(got)
        return found

    def command_line(self, proc: Proc) -> str | None:
        """The command `proc` runs, its arguments joined by spaces; None once it has ended (or
        its pid is another process's) or when it cannot be read."""
        try:
            raw = (self._proc / str(proc.pid) / "cmdline").read_bytes()
        except OSError:
            return None
        if proc.fence is not None and self._fence(proc.pid) != proc.fence:
            return None
        words = raw.rstrip(b"\0").split(b"\0")
        line = b" ".join(words).decode(errors="replace").strip()
        return line or None

    def _of_session(self, pid: int, session: SessionRef, basis: str) -> Proc | None:
        if pid == session.agent_pid:
            role = "agent"
        elif basis == "env":
            role = "orphan"
        else:
            home = cgroup.own_cgroup(self._proc, str(pid)) or ""
            role = "lease" if PurePosixPath(home).name.startswith("lease-") else "tool"
        return self._proc_of(pid, role=role, basis=basis)

    # --- trees -------------------------------------------------------------------------------

    def start(
        self, argv, spec: TreeSpec | None, *, cancel_fd: int | None = None, **popen
    ) -> Started:
        """Start `argv` in a scope sized to `spec`, or in its process group only when `spec` is
        None or no scope can be made here. When the scope cannot be made after all, the
        command runs without it, and `Started.uncontained` says why. Raises `host.Cancelled`
        when `cancel_fd` turns readable while the scope is made."""
        if spec is None or self.support("start").how == "none":
            return super().start(argv, spec, **popen)
        cap = []
        if spec.cap_bytes is not None and self.support("memory_hard").how != "none":
            cap = systemd.cap_properties(spec.cap_bytes)
        prefix = systemd.scope_argv(
            spec.unit, spec.slice_, weight=spec.weight, memory=spec.memory,
            hard_cpu=spec.hard_cpu, cap=cap,
        )  # fmt: skip
        mark = self.mark()
        child, why = systemd.spawn(argv, prefix, cancel_fd=cancel_fd, whole=bool(cap), **popen)
        tree = self._tree(child, popen)
        if why is None:
            path = cgroup.own_cgroup(self._proc, str(child.pid))
            tree = replace(
                tree,
                unit=spec.unit,
                group=path if path and PurePosixPath(path).name == spec.unit else None,
                cap_bytes=spec.cap_bytes if cap else None,
                limit_bytes=self.memory_max(child.pid),
            )
        return Started(self, child, tree, mark, why)

    def adopt(self, pids: list[int], spec: TreeSpec) -> Adoption:
        if not pids:
            return Adoption("none", reason="no process to adopt")
        why = systemd.probe(
            env=self._env,
            cgroup_root=self._cgroup_root,
            self_cgroup=self._proc / str(pids[0]) / "cgroup",
            answers=lambda: True,  # asked by the move itself
        )
        if why is not None:
            return Adoption("none", reason=why)
        props = systemd.scope_properties(
            weight=spec.weight, memory=spec.memory, hard_cpu=spec.hard_cpu
        )
        capped = spec.cap_bytes is not None
        if capped:
            props += systemd.cap_properties(spec.cap_bytes)
        why = systemd.adopt(
            pids, spec.unit, spec.slice_, props, oom_continue=not capped, proc=self._proc
        )
        if why is not None:
            return Adoption("none", reason=why)
        for pid in pids:
            _raise_oom_score(self._proc / str(pid) / "oom_score_adj")
        homes = (cgroup.own_cgroup(self._proc, str(pid)) for pid in pids)
        path = next((h for h in homes if h and PurePosixPath(h).name == spec.unit), None)
        if capped and path is not None:
            with contextlib.suppress(OSError):
                (self._cgroup_root / path.lstrip("/") / "memory.oom.group").write_text("1")
        limit = None
        if path is not None:
            parent = str(PurePosixPath(path).parent)
            above = cgroup.limit(self._cgroup_root, parent, files=("memory.max",))
            limit = None if above is None else above.limit_bytes
        tree = Tree(
            root=pids[0],
            fence=self._fence(pids[0]),
            unit=spec.unit,
            group=path,
            cap_bytes=spec.cap_bytes,
            limit_bytes=limit,
        )
        return Adoption("exact", spec.unit, limit_bytes=limit, capped=capped, tree=tree)

    def join(self, tree: Tree, pids: list[int]) -> Done:
        if tree.unit is None:
            return Done("none", "the tree has no scope to join")
        why = systemd.adopt(pids, tree.unit, lease_slice(self._env), proc=self._proc)
        return Done("none", why) if why is not None else Done("systemd")

    def resize(self, tree: Tree, spec: TreeSpec) -> Done:
        if tree.unit is None:
            return Done("none", "the tree has no scope to resize")
        why = systemd.update_scope(
            tree.unit, weight=spec.weight, memory=spec.memory, hard_cpu=spec.hard_cpu
        )
        return Done("none", why) if why is not None else Done("systemd")

    def release(self, tree: Tree) -> Done:
        if tree.unit is None:
            return Done("none", "the tree has no scope")
        why = systemd.neutral_scope(tree.unit)
        return Done("none", why) if why is not None else Done("systemd")

    # --- acting on a tree --------------------------------------------------------------------

    def _freeze(self, tree: Tree, value: str, how: str) -> Done:
        assert tree.group is not None
        path = self._cgroup_root / tree.group.lstrip("/") / "cgroup.freeze"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
        except OSError as exc:
            return Done("none", f"could not write {path}: {exc}", empty=exc.errno in _GONE)
        try:
            os.write(fd, value.encode())
        except OSError as exc:
            return Done("none", f"could not write {path}: {exc}", empty=exc.errno in _GONE)
        finally:
            os.close(fd)
        return Done(how)

    def _each(self, tree: Tree, sig: int, *, stopped: bool | None = None) -> list[int]:
        """Signal `sig` to the tree's group (unless its keeper is in it) and to each member but
        its keeper; with `stopped` set, only to members whose state is not already that. The
        pids signalled."""
        pgid = self._group(tree)
        if pgid is not None:
            signal_group(pgid, sig)
        sent = []
        for pid in self._pids(tree):
            if pid == tree.keeper:
                continue
            if stopped is not None:
                stat = self._stat(pid)
                if stat is None or (stat[0] in ("T", "t")) == stopped:
                    continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
                sent.append(pid)
        return sent

    def pause(self, tree: Tree) -> Done:
        if tree.group is not None:
            return self._freeze(tree, "1", "freeze")
        if not self._pids(tree):
            return Done("none", "the tree has ended", empty=True)
        for _ in range(10):  # a member forked between a scan and its signal is caught next
            if not self._each(tree, signal.SIGSTOP, stopped=True):
                return Done("sigstop")
            time.sleep(0.01)
        return Done("none", "members kept starting while the tree was being stopped")

    def resume(self, tree: Tree) -> Done:
        if tree.group is not None:
            return self._freeze(tree, "0", "freeze")
        if not self._pids(tree):
            return Done("none", "the tree has ended", empty=True)
        self._each(tree, signal.SIGCONT)
        return Done("sigcont")

    def kill(self, tree: Tree, grace_s: float) -> Done:
        if tree.unit is not None and systemd.ours(tree.unit):
            try:
                left = systemd.stop_unit(tree.unit, grace_s, cgroup_root=self._cgroup_root)
            except systemd.CleanupError as exc:
                return Done("none", str(exc))
            return Done("systemd", empty=not left)
        if not self._pids(tree):
            return Done("signal", empty=True)
        self._each(tree, signal.SIGTERM)
        self._each(tree, signal.SIGCONT)
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and self._pids(tree):
            time.sleep(0.1)
        self._each(tree, signal.SIGKILL)
        return Done("signal")

    # --- how a command ended -----------------------------------------------------------------

    def mark(self) -> Mark:
        return Mark(oom_kills(self._proc))

    def ended(
        self, status: int, since: Mark, *, tree: Tree | None = None, counted: int | None = None
    ) -> Ending:
        now = oom_kills(self._proc)
        if since.oom_kills is None or now is None:
            base = Ending(reason="the kernel's OOM kill count cannot be read")
        else:
            kills = max(0, now - since.oom_kills)
            if sigkilled(status) and kills:
                base = Ending("kernel", "likely", kills, how="exact")
            else:
                base = Ending(kills=kills, how="exact")
        if tree is None or tree.unit is None:
            return base
        # A capped scope outlives its command, so systemd can say the OOM killer ended it.
        reached = tree.cap_bytes is not None and systemd.cap_result(tree.unit) == "oom-kill"
        if not (counted or reached):
            return base
        kind = _cap_or_kernel(tree)
        limit = tree.cap_bytes if kind == "cap" else tree.limit_bytes
        return Ending(kind, "certain", base.kills, how="exact", limit_bytes=limit)

    def memory_max(self, pid: int) -> int | None:
        path = cgroup.own_cgroup(self._proc, str(pid))
        if path is None:
            return None
        found = cgroup.limit(self._cgroup_root, path, files=("memory.max",))
        return None if found is None else found.limit_bytes


def _raise_oom_score(path: Path) -> None:
    """Raise a negative `oom_score_adj` to 0, and leave any other alone (`systemd.oom_reset_code`
    does the same for a command `systemd.spawn` starts)."""
    try:
        if int(path.read_text()) < 0:
            path.write_text("0")
    except (OSError, ValueError):
        pass
