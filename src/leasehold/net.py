"""The memory net: what a user's own daemon does when memory under a limit runs out, so that a
contained command is paused instead of killed by the OOM killer (`[memory_net] mode = "park"`).

Admission (`leasehold.admission`) starts work by what each lease counts, and a count can be
wrong. When it is, the commands under one memory limit need more than the limit together and
spill into swap; once the limit's swap fills, the kernel kills one of them. The net acts on the
spill: it pauses one command and moves its memory out to swap until another command has ended,
and holds new work back meanwhile.

The net watches the scope of every contained command (`lease-<id>.scope`, `leasehold.contain`)
of a lease the daemon holds, found under this user's `leasehold.slice`, directly or in a
`leasehold run` session slice. It groups scopes by their cap: the cgroup above the scope that
sets the lowest memory limit (`leasehold.cgroup.limit`), or the machine when none does. When it
first sees a scope, it sets the scope's `memory.swap.max` to `overflow`, the swap the command
may spill into before the net acts.

A park is due in a cap when a running scope's swap grew by `SPILL_BYTES` or more over the last
`WINDOW_S`, or over the last `SLOW_WINDOW_S`, and the cap spent `STALL_SHARE` or more of that
same time stalled on memory (PSI `some`, from its running total). The short window catches a
fast spill, and the long one a command that spills slowly because it is already stalled. The
net then parks the cap's youngest heavy scope: the one granted last, of those holding
`HEAVY_BYTES` or more in memory and swap. It never parks the only heavy scope running in a cap,
never within `THAW_GAP_S` of a resume in that cap, and never once parking has written
`write_per_day` to swap in the last 24 hours.

To park a scope, the net freezes it (`cgroup.freeze`), lifts its swap limit and asks the kernel
to reclaim what it uses (`memory.reclaim`, at most what the day's writes leave), which moves its
memory out to swap. A reclaim that stops short leaves the scope frozen all the same. While the
reclaim runs, the cap's other running scopes have their swap limits lifted too, so that none
fills its overflow while room is made; the limits go back once it ends.

The net resumes one scope a step, the one parked longest first, once no other heavy scope of
its cap runs, once the cap's use plus what the scope holds in swap is under `THAW_AT` of the
limit, or once it has been parked `MAX_PARK_S` (a forced resume, after which it may be parked
again). Its swap limit goes back to `overflow`, so it takes no more swap than it already holds.
It then settles: until the moving average (over `SETTLE_TAU_S`) of its growth has stayed under
the larger of `SETTLE_FLOOR` and `SETTLE_SHARE` of its use a second for `SETTLE_COOLDOWN_S`,
until it ends, or for `SETTLE_MAX_S` at most.

While any lease is parked or settling, `held` says why, and the machine agent holds back new
work that counts memory (the memory net's hold in `leasehold.admission`). A request granted the
moment a peer ended would otherwise start beside the resumed command as it reads its memory
back, and the two would overflow the cap again.

Without swap there is nowhere to put a parked command's memory, and without cgroup v2 there is
no scope to pause: `reason` says why, and nothing is parked while it holds. A scope of a lease
the daemon no longer holds is resumed without settling, since its memory is no longer counted.
A scope found frozen that this net did not park (its daemon stopped mid-park) is thawed, and
`stop` resumes every scope the net parked.

The net steps every `TICK_S`, and sooner when a cap's `memory.events` changes (the cap reached
its `memory.high` or `memory.max`), but never more often than every `MIN_STEP_S`. Roots, clocks
and the leases are injectable, so a test can run it on a fake cgroup tree.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import math
import os
import select
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from leasehold import cgroup, contain
from leasehold.machine import MemoryNetSettings

SPILL_BYTES = 32 << 20
WINDOW_S = 0.25
SLOW_WINDOW_S = 4.0
STALL_SHARE = 0.05
HEAVY_BYTES = 256 << 20
THAW_AT = 0.80
THAW_GAP_S = 2.0
MAX_PARK_S = 120.0
SETTLE_FLOOR = 16 << 20
SETTLE_SHARE = 0.01
SETTLE_TAU_S = 1.0
SETTLE_COOLDOWN_S = 5.0
SETTLE_MAX_S = 60.0
TICK_S = 0.25
MIN_STEP_S = 0.05
#: The window `write_per_day` is counted over.
DAY_S = 86400.0

#: A lease the agent holds, as the net needs it: (lease id, wall-clock time it was granted).
Held = tuple[str, float]


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def _int(path: Path) -> int | None:
    text = (_read(path) or "").strip()
    return int(text) if text.isdigit() else None


def _stall_us(path: Path) -> int | None:
    """The `some` total, in microseconds, of the PSI file `path`."""
    for line in (_read(path) or "").splitlines():
        if line.startswith("some "):
            for part in line.split()[1:]:
                name, _, value = part.partition("=")
                if name == "total" and value.isdigit():
                    return int(value)
    return None


def _write(path: Path, value: str) -> bool:
    """Write `value` to the cgroup file `path`; False when the cgroup is gone. A reclaim that
    could not reclaim all it was asked (`EAGAIN`) counts as written."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    except (FileNotFoundError, NotADirectoryError):
        return False
    try:
        os.write(fd, value.encode())
    except OSError as exc:
        if exc.errno == errno.EAGAIN:
            return True
        if exc.errno in (errno.ENOENT, errno.ENODEV):
            return False
        raise
    finally:
        os.close(fd)
    return True


class _Window:
    """Readings of one counter over the last `SLOW_WINDOW_S` or so: `rise(span)` is how much it
    grew over at least `span` (None until there are readings that far apart)."""

    def __init__(self) -> None:
        self.points: collections.deque[tuple[float, int]] = collections.deque()

    def add(self, t: float, value: int) -> None:
        self.points.append((t, value))
        while len(self.points) > 2 and t - self.points[1][0] >= SLOW_WINDOW_S:
            self.points.popleft()

    def rise(self, span: float) -> tuple[int, float] | None:
        if len(self.points) < 2:
            return None
        t1, v1 = self.points[-1]
        for t0, v0 in reversed(self.points):
            if t1 - t0 >= span:
                return v1 - v0, t1 - t0
        return None

    def clear(self) -> None:
        self.points.clear()


@dataclass
class _Cap:
    #: The cgroup that sets the limit, as a directory; None: the machine.
    path: Path | None
    limit: int
    stall: _Window = field(default_factory=_Window)
    last_thaw: float = -math.inf


@dataclass
class _Scope:
    lease_id: str
    path: Path
    granted_at: float
    cap: str
    swap: _Window = field(default_factory=_Window)
    parked_at: float | None = None
    parked_wall: float | None = None
    pre_park: int = 0
    thawed_at: float | None = None
    thawed_wall: float | None = None
    calm_since: float | None = None
    rate: float = 0.0
    last: tuple[float, int] | None = None
    #: The memory it used at the last step, and the swap it held.
    current: int = 0
    swapped: int = 0
    #: Its memory and swap at its last park: what it needs to run again.
    resident: int = 0

    @property
    def footprint(self) -> int:
        return self.current + self.swapped


class MemoryNet:
    """Parks and resumes the contained commands of the leases `leases()` names (see the module's
    docstring). `on_change` is called, outside the net's lock, whenever a lease is parked,
    resumed or has settled; `events()` then gives what happened."""

    def __init__(
        self,
        settings: MemoryNetSettings,
        *,
        leases: Callable[[], Iterable[Held]],
        on_change: Callable[[], object] = lambda: None,
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        proc: Path = Path("/proc"),
        uid: int | None = None,
        clock: Callable[[], float],
        monotonic: Callable[[], float],
    ) -> None:
        self.settings = settings
        self.overflow = settings.overflow_bytes()
        self.write_cap = settings.write_per_day_bytes()
        self._leases = leases
        self._on_change = on_change
        self._root = cgroup_root
        self._proc = proc
        self._slice = cgroup_root / contain.root_slice_cgroup(uid)
        self._clock = clock
        self._now = monotonic
        self._lock = threading.Lock()
        self._scopes: dict[str, _Scope] = {}
        self._caps: dict[str, _Cap] = {}
        #: (when, bytes) of each park's writes to swap in the last `DAY_S`.
        self._writes: collections.deque[tuple[float, int]] = collections.deque()
        #: Per lease: [parks, seconds parked], until `take` hands them over.
        self._counts: dict[str, list[float]] = {}
        self._events: list[dict] = []
        self.parks = 0
        self.forced = 0
        self.written = 0
        self.reason: str | None = None
        self._poller: _Watch | None = None

    # --- what others ask -----------------------------------------------------------------

    def held(self) -> str | None:
        """Why new memory-counting work waits now, in words; None while nothing is parked or
        settling."""
        with self._lock:
            parked = [s.lease_id for s in self._scopes.values() if s.parked_at is not None]
            settling = [s.lease_id for s in self._scopes.values() if s.thawed_at is not None]
        if parked:
            return f"{_leases(parked)} parked until memory frees"
        if settling:
            return f"{_leases(settling)} resuming after a park"
        return None

    def state(self, lease_id: str) -> str | None:
        """`parked`, `settling` or None, for the lease `lease_id`."""
        with self._lock:
            scope = self._scopes.get(lease_id)
            if scope is None:
                return None
            if scope.parked_at is not None:
                return "parked"
            return "settling" if scope.thawed_at is not None else None

    def take(self, lease_id: str) -> dict:
        """How often the lease `lease_id` was parked and for how long in all (`parks`,
        `parked_s`), forgotten once asked; empty when it never was."""
        with self._lock:
            scope = self._scopes.get(lease_id)
            counts = self._counts.pop(lease_id, None)
            if counts is not None and scope is not None and scope.parked_at is not None:
                counts[1] += self._now() - scope.parked_at
        if counts is None:
            return {}
        return {"parks": int(counts[0]), "parked_s": round(counts[1], 3)}

    def events(self) -> list[dict]:
        """What happened since the last call: `parked` and `resumed` events, each naming its
        `lease_id`."""
        with self._lock:
            out, self._events = self._events, []
        return out

    def status(self) -> dict:
        with self._lock:
            now = self._now()
            parked = [
                {"lease_id": s.lease_id, "since": s.parked_wall, "memory_bytes": s.pre_park}
                for s in self._scopes.values()
                if s.parked_at is not None
            ]
            settling = [
                {"lease_id": s.lease_id, "since": s.thawed_wall}
                for s in self._scopes.values()
                if s.thawed_at is not None
            ]
            return {
                "mode": self.settings.mode,
                "active": self.reason is None,
                "reason": self.reason,
                "overflow_bytes": self.overflow,
                "write_per_day_bytes": self.write_cap,
                "written_day_bytes": self._written_since(now - DAY_S),
                "watched": len(self._scopes),
                "parked": parked,
                "settling": settling,
                "parks": self.parks,
                "forced_resumes": self.forced,
                "written_bytes": self.written,
            }

    # --- the loop --------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Step every `TICK_S`, and at once when a cap's `memory.events` changes, until `stop`;
        then resume everything parked."""
        self._poller = _Watch()
        last = self._now()
        try:
            while not stop.is_set():
                self._poller.wait(TICK_S)
                # A cap at its limit changes `memory.events` all the time: step no faster.
                if stop.wait(max(0.0, MIN_STEP_S - (self._now() - last))):
                    break
                last = self._now()
                try:
                    self.step()
                except Exception as exc:  # noqa: BLE001 -- a bad step must not end the net
                    os.write(2, f"leasehold: memory net step failed: {exc!r}\n".encode())
        finally:
            self._poller.close()
            self.stop()

    def stop(self) -> None:
        """Resume every scope this net parked, with no settling."""
        with self._lock:
            for scope in self._scopes.values():
                if scope.parked_at is not None:
                    self._thaw(scope, self._now(), settle=False)
                scope.thawed_at = scope.thawed_wall = None

    def step(self) -> None:
        """Read, then park or resume what is due (the module's docstring)."""
        held = list(self._leases())
        with self._lock:
            before = self._shape()
            now = self._now()
            self.reason = self._unable()
            self._track(held, now)
            caps = self._read_caps(now)
            if self._poller is not None:
                self._poller.watch([c.path for c in self._caps.values() if c.path is not None])
            for scope in self._scopes.values():
                self._read_scope(scope, now)
            victim = None
            for key, cap in self._caps.items():
                if self._resume(key, cap, caps.get(key), now):
                    continue
                victim = victim or self._due(key, cap, now)
            peers = None if victim is None else self._freeze(victim, now)
            changed = self._shape() != before
        if victim is not None and peers is not None:
            self._reclaim(victim, peers)
        if changed:
            self._on_change()

    # --- inside a step (under the lock) ---------------------------------------------------

    def _shape(self) -> tuple:
        return tuple(
            sorted(
                (s.lease_id, s.parked_at is not None, s.thawed_at is not None)
                for s in self._scopes.values()
                if s.parked_at is not None or s.thawed_at is not None
            )
        )

    def _unable(self) -> str | None:
        if not (self._root / "cgroup.controllers").is_file():
            return "no cgroup v2 here, so no scope to pause"
        swap = None
        for line in (_read(self._proc / "meminfo") or "").splitlines():
            name, _, value = line.partition(":")
            if name == "SwapTotal":
                parts = value.split()
                swap = int(parts[0]) * 1024 if parts and parts[0].isdigit() else None
        if not swap:
            return "no swap, so nowhere to put a parked command's memory"
        return None

    def _track(self, held: list[Held], now: float) -> None:
        """Start watching the scopes of new leases; let go of those whose lease or scope is
        gone."""
        live = dict(held)
        for lease_id in list(self._scopes):
            scope = self._scopes[lease_id]
            if lease_id in live and scope.path.is_dir():
                continue
            if scope.parked_at is not None:
                self._thaw(scope, now, settle=False)
            del self._scopes[lease_id]
        for lease_id in list(self._counts):
            if lease_id not in live:
                del self._counts[lease_id]
        for lease_id, granted_at in held:
            if lease_id in self._scopes:
                continue
            path = self._find(lease_id)
            if path is None:
                continue
            if (_read(path / "cgroup.freeze") or "").strip() == "1":
                _write(path / "cgroup.freeze", "0")  # left frozen by a daemon that stopped
            if self.overflow is not None:
                _write(path / "memory.swap.max", str(self.overflow))
            self._scopes[lease_id] = _Scope(lease_id, path, granted_at, self._cap_of(path))

    def _find(self, lease_id: str) -> Path | None:
        unit = contain.lease_unit(lease_id)
        for path in (self._slice / unit, *self._slice.glob(f"leasehold-*.slice/{unit}")):
            if path.is_dir():
                return path
        return None

    def _cap_of(self, path: Path) -> str:
        """The key of the cap above the scope at `path`, made when new."""
        relative = "/" + str(path.parent.relative_to(self._root))
        found = cgroup.limit(self._root, relative)
        key = "" if found is None else found.cgroup
        if found is None:
            total = _meminfo(self._proc).get("MemTotal") or 0
            self._caps.setdefault(key, _Cap(None, total))
        else:
            cap = self._caps.setdefault(
                key, _Cap(self._root / found.cgroup.lstrip("/"), found.limit_bytes)
            )
            cap.limit = found.limit_bytes
        return key

    def _read_caps(self, now: float) -> dict[str, int]:
        """Each cap's use now, in bytes; its stall total goes in its window."""
        used: dict[str, int] = {}
        keys = {s.cap for s in self._scopes.values()}
        for key in list(self._caps):
            if key not in keys:
                del self._caps[key]
        for key, cap in self._caps.items():
            if cap.path is None:
                info = _meminfo(self._proc)
                total, free = info.get("MemTotal"), info.get("MemAvailable")
                if total is not None and free is not None:
                    used[key] = total - free
                stall = _stall_us(self._proc / "pressure" / "memory")
            else:
                current = _int(cap.path / "memory.current")
                if current is not None:
                    used[key] = current
                stall = _stall_us(cap.path / "memory.pressure")
            if stall is not None:
                cap.stall.add(now, stall)
        return used

    def _read_scope(self, scope: _Scope, now: float) -> None:
        scope.current = _int(scope.path / "memory.current") or 0
        scope.swapped = _int(scope.path / "memory.swap.current") or 0
        if scope.parked_at is None:
            scope.swap.add(now, scope.swapped)
        if scope.thawed_at is not None:
            self._settling(scope, now)

    def _settling(self, scope: _Scope, now: float) -> None:
        if scope.last is None:
            scope.last = (now, scope.current)
        elif now - scope.last[0] >= WINDOW_S:
            dt = now - scope.last[0]
            weight = 1 - math.exp(-dt / SETTLE_TAU_S)
            scope.rate += weight * ((scope.current - scope.last[1]) / dt - scope.rate)
            scope.last = (now, scope.current)
            calm = scope.rate < max(SETTLE_FLOOR, SETTLE_SHARE * scope.current)
            scope.calm_since = (scope.calm_since or now) if calm else None
        assert scope.thawed_at is not None
        if (scope.calm_since is not None and now - scope.calm_since >= SETTLE_COOLDOWN_S) or (
            now - scope.thawed_at >= SETTLE_MAX_S
        ):
            scope.thawed_at = scope.thawed_wall = scope.calm_since = scope.last = None
            scope.rate = 0.0

    def _in(self, key: str) -> list[_Scope]:
        return [s for s in self._scopes.values() if s.cap == key]

    def _resume(self, key: str, cap: _Cap, used: int | None, now: float) -> bool:
        """Resume the cap's longest-parked scope when it is due; whether one was."""
        parked = sorted(
            (s for s in self._in(key) if s.parked_at is not None),
            key=lambda s: s.parked_at or 0.0,
        )
        if not parked:
            return False
        first = parked[0]
        assert first.parked_at is not None
        running = [s for s in self._in(key) if s.parked_at is None and s.footprint >= HEAVY_BYTES]
        forced = now - first.parked_at >= MAX_PARK_S
        fits = used is not None and used + first.swapped < THAW_AT * cap.limit
        if not (forced or not running or fits):
            return False
        if running and not fits:
            self.forced += 1
        self._thaw(first, now, settle=True)
        cap.last_thaw = now
        return True

    def _due(self, key: str, cap: _Cap, now: float) -> _Scope | None:
        """The scope to park in the cap now, if a park is due there."""
        if self.reason is not None or now - cap.last_thaw < THAW_GAP_S:
            return None
        if self._written_since(now - DAY_S) >= self.write_cap:
            return None
        running = [s for s in self._in(key) if s.parked_at is None]
        heavy = [s for s in running if s.footprint >= HEAVY_BYTES]
        if len(heavy) < 2:
            return None
        for span in (WINDOW_S, SLOW_WINDOW_S):
            spilling = any(
                (rise := s.swap.rise(span)) is not None and rise[0] >= SPILL_BYTES for s in running
            )
            stalled = cap.stall.rise(span)
            if spilling and stalled is not None and stalled[0] / 1e6 >= STALL_SHARE * stalled[1]:
                return max(heavy, key=lambda s: s.granted_at)
        return None

    def _freeze(self, scope: _Scope, now: float) -> list[_Scope] | None:
        """Freeze `scope` and lift its swap limit and its cap's other running scopes'; those
        others. None when the scope is gone (its command has just ended)."""
        if not _write(scope.path / "cgroup.freeze", "1"):
            return None
        scope.parked_at, scope.parked_wall = now, self._clock()
        scope.thawed_at = scope.thawed_wall = scope.calm_since = scope.last = None
        scope.pre_park, scope.resident = scope.footprint, scope.current
        scope.swap.clear()
        self.parks += 1
        self._counts.setdefault(scope.lease_id, [0, 0.0])[0] += 1
        self._events.append({"type": "parked", "lease_id": scope.lease_id})
        peers = [s for s in self._in(scope.cap) if s.parked_at is None]
        if self.overflow is not None:
            for s in [scope, *peers]:
                _write(s.path / "memory.swap.max", "max")
        return peers

    def _thaw(self, scope: _Scope, now: float, *, settle: bool) -> None:
        if self.overflow is not None:
            _write(scope.path / "memory.swap.max", str(self.overflow))
        _write(scope.path / "cgroup.freeze", "0")
        if scope.parked_at is not None:
            counts = self._counts.setdefault(scope.lease_id, [0, 0.0])
            counts[1] += now - scope.parked_at
        scope.parked_at = scope.parked_wall = None
        if settle:
            scope.thawed_at, scope.thawed_wall = now, self._clock()
        scope.swap.clear()
        self._events.append({"type": "resumed", "lease_id": scope.lease_id})

    def _written_since(self, since: float) -> int:
        while self._writes and self._writes[0][0] < since:
            self._writes.popleft()
        return sum(n for _, n in self._writes)

    # --- outside the lock ------------------------------------------------------------------

    def _reclaim(self, scope: _Scope, peers: list[_Scope]) -> None:
        """Move the parked `scope`'s memory out to swap (blocking until the kernel is done),
        then put its peers' swap limits back."""
        with self._lock:
            left = self.write_cap - self._written_since(self._now() - DAY_S)
            want = min(scope.resident, max(0, left))
        before = _int(scope.path / "memory.swap.current") or 0
        try:
            if want > 0:
                _write(scope.path / "memory.reclaim", str(want))
        finally:
            if self.overflow is not None:
                for peer in peers:
                    _write(peer.path / "memory.swap.max", str(self.overflow))
        after = _int(scope.path / "memory.swap.current")
        written = max(0, (after or before) - before)
        with self._lock:
            self._writes.append((self._now(), written))
            self.written += written


def _leases(ids: list[str]) -> str:
    if len(ids) == 1:
        return f"lease {ids[0]} is"
    return f"{len(ids)} leases are"


def _meminfo(proc: Path) -> dict[str, int]:
    out = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        name, _, value = line.partition(":")
        parts = value.split()
        if parts and parts[0].isdigit():
            out[name] = int(parts[0]) * 1024
    return out


class _Watch:
    """What wakes the net early: a change to a cap's `memory.events`. Files are opened again
    when the caps change (systemd makes a slice's cgroup anew when a scope starts in it
    again)."""

    def __init__(self) -> None:
        self.poller = select.poll()
        self.fds: dict[Path, int] = {}
        self.inodes: dict[Path, int] = {}

    def watch(self, caps: list[Path]) -> None:
        wanted = {}
        for path in caps:
            with contextlib.suppress(OSError):
                wanted[path] = os.stat(path).st_ino
        for path in list(self.fds):
            if wanted.get(path) != self.inodes.get(path):
                self._drop(path)
        for path, inode in wanted.items():
            if path in self.fds:
                continue
            with contextlib.suppress(OSError):
                fd = os.open(path / "memory.events", os.O_RDONLY)
                os.read(fd, 4096)  # a change is news from here on
                self.fds[path], self.inodes[path] = fd, inode
                self.poller.register(fd, select.POLLPRI)

    def wait(self, timeout: float) -> None:
        for fd, _ in self.poller.poll(timeout * 1000):
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                os.read(fd, 4096)

    def _drop(self, path: Path) -> None:
        fd = self.fds.pop(path)
        self.inodes.pop(path, None)
        self.poller.unregister(fd)
        os.close(fd)

    def close(self) -> None:
        for path in list(self.fds):
            self._drop(path)
