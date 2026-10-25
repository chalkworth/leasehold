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
sets the lowest memory limit (`leasehold.cgroup.limit`), or the machine when none does.

A cap is armed while a park could happen there: the net can park (`reason` is None), the day's
writes are not used up, and two or more heavy scopes run in it (heavy: holding `HEAVY_BYTES` or
more in memory and swap). In an armed cap each running scope's `memory.swap.max` is `overflow`
more than the swap it held when the cap was armed, the room it may spill into before the net
acts. Everywhere else the net sets no swap limit, and puts `max` back where it set one, so a
command the net would not park swaps as freely as without the net.

A park is due in an armed cap that is near its limit (its use is `NEAR` of the limit or more,
or its `memory.events` counted a `high` or `max` event in the last `SLOW_WINDOW_S`) and stalled
on memory (PSI `some`, from its running total, `STALL_SHARE` or more of the time), when a
running scope's swap grew by `SPILL_BYTES` or more over the last `WINDOW_S` or `SLOW_WINDOW_S`,
or has used `FULL_SHARE` or more of its overflow. The short window catches a fast spill, the
long one a slow spill, and the last a scope whose swap is at its limit and can no longer grow.
The net then parks the cap's youngest heavy scope, the one granted last, which has done the
least work, and no other in that cap for `GAP_S`.

To park a scope, the net freezes it (`cgroup.freeze`), lifts its swap limit and asks the kernel
to reclaim what it uses (`memory.reclaim`, at most what the day's writes leave), which moves its
memory out to swap. A reclaim that stops short leaves the scope frozen all the same. While the
reclaim runs, the cap's other running scopes have their swap limits lifted too, so that none
fills its overflow while room is made. Every swap and stall reading of the cap starts afresh
after a park, so the next park needs a spill seen after it.

The net resumes one scope a step, the one parked longest first, once no other heavy scope of
its cap runs or once the cap's use plus what the scope holds in swap is under `THAW_AT` of the
limit. A parked scope waits as long as neither holds. It then settles: until the moving average
(over `SETTLE_TAU_S`) of its growth has stayed under the larger of `SETTLE_FLOOR` and
`SETTLE_SHARE` of its use a second for `SETTLE_COOLDOWN_S`, until it ends, or for
`SETTLE_MAX_S` at most. No park follows in its cap for `GAP_S`.

While any lease is parked or settling, `held` says why, and the machine agent holds back new
work that counts memory (the memory net's hold in `leasehold.admission`), under every cap. A
request granted the moment a peer ended would otherwise start beside the resumed command as it
reads its memory back, and the two would overflow the cap again.

The net keeps a record (`state`, a JSON file beside the ledger) of the scopes it froze, the
scopes whose swap limit it set, and what parking wrote to swap in the last day. A net that
stops leaves what it parked frozen and puts every swap limit it set back to `max`. A net that
starts adopts each scope the record says it froze as parked, if its lease is still held, and
resumes the others. With the net off, `release` resumes and unlimits whatever the record names.
Of the scopes of leases the daemon does not hold, the net touches only those its record names.

Without swap there is nowhere to put a parked command's memory, without `memory.reclaim` (Linux
5.19 and later) no way to move it there, and without cgroup v2 no scope to pause: `reason` says
why, and the net parks nothing while it holds. A scope of a lease the daemon no longer holds is
resumed without settling, since its memory is no longer counted. A cgroup file the net cannot
write is reported once on stderr and skipped.

The net steps every `TICK_S`, and sooner when a cap's `memory.events` changes, but never more
often than every `MIN_STEP_S`. Roots, clocks and the leases are injectable, so a test can run
it on a fake cgroup tree.
"""

from __future__ import annotations

import collections
import contextlib
import errno
import json
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
FULL_SHARE = 0.75
STALL_SHARE = 0.05
NEAR = 0.90
HEAVY_BYTES = 256 << 20
THAW_AT = 0.80
GAP_S = 2.0
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

_reported: set[tuple[str, int]] = set()


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


def _events(path: Path) -> int | None:
    """The `high` and `max` counts of the `memory.events` file `path`, summed."""
    text = _read(path)
    if text is None:
        return None
    total = 0
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name in ("high", "max") and value.strip().isdigit():
            total += int(value)
    return total


def _write(path: Path, value: str) -> bool:
    """Write `value` to the cgroup file `path`; whether it was written. A reclaim that could
    not reclaim all it was asked (`EAGAIN`) counts as written. A cgroup that is gone is False
    quietly; any other failure is False and said once on stderr."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    except OSError as exc:
        return _failed(path, exc)
    try:
        os.write(fd, value.encode())
    except OSError as exc:
        return exc.errno == errno.EAGAIN or _failed(path, exc)
    finally:
        os.close(fd)
    return True


def _failed(path: Path, exc: OSError) -> bool:
    if exc.errno not in (errno.ENOENT, errno.ENODEV, errno.ENOTDIR):
        key = (str(path), exc.errno or 0)
        if key not in _reported:
            _reported.add(key)
            os.write(2, f"leasehold: memory net could not write {path}: {exc}\n".encode())
    return False


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
    #: No park here before this time (after a park or a resume).
    quiet_until: float = -math.inf
    #: Its `memory.events` count at the last step, and when it last grew.
    events: int | None = None
    pressed_at: float = -math.inf


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
    #: Its memory when parked: what the reclaim moves out.
    resident: int = 0
    #: The `memory.swap.max` the net last wrote (None: none yet), and the swap it held when
    #: its cap was armed, which its limit is `overflow` above.
    limit: str | None = None
    base: int = 0

    @property
    def footprint(self) -> int:
        return self.current + self.swapped

    @property
    def limited(self) -> bool:
        return self.limit not in (None, "max")


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
        state: Path | None = None,
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
        self._state = state
        self._root = cgroup_root
        self._proc = proc
        self._slice = cgroup_root / contain.root_slice_cgroup(uid)
        self._clock = clock
        self._now = monotonic
        self._lock = threading.Lock()
        self._scopes: dict[str, _Scope] = {}
        self._caps: dict[str, _Cap] = {}
        record = _load(state)
        #: Scopes a net before this one froze or limited, until the first step settles them.
        self._adopt = {Path(p) for p in record.get("frozen", [])}
        self._unlimit = {Path(p) for p in record.get("limited", [])}
        #: (wall-clock time, bytes) of each park's writes to swap in the last `DAY_S`.
        self._writes: collections.deque[tuple[float, int]] = collections.deque(
            (float(t), int(n)) for t, n in record.get("writes", [])
        )
        self._saved: dict | None = None
        #: Scopes whose reclaim is under way: their swap limits stay lifted until it ends.
        self._reclaiming: set[str] = set()
        #: Per lease: [parks, seconds parked], until `take` hands them over.
        self._counts: dict[str, list[float]] = {}
        self._events: list[dict] = []
        self.parks = 0
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
                "written_day_bytes": self._written_day(),
                "watched": len(self._scopes),
                "parked": parked,
                "settling": settling,
                "parks": self.parks,
                "written_bytes": self.written,
            }

    # --- the loop --------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        """Step every `TICK_S`, and sooner when a cap's `memory.events` changes, until `stop`;
        then `stop()`."""
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
        """Put every swap limit the net set back to `max`, and leave what it parked frozen for
        the next net to adopt (its record says which)."""
        with self._lock:
            for scope in self._scopes.values():
                if scope.limited or scope.lease_id in self._reclaiming:
                    _write(scope.path / "memory.swap.max", "max")
                scope.limit = "max"
            self._save()

    def step(self) -> None:
        """Read, then park or resume what is due (the module's docstring)."""
        held = list(self._leases())
        with self._lock:
            before = self._shape()
            now = self._now()
            self.reason = self._unable()
            self._track(held, now)
            used = self._read_caps(now)
            if self._poller is not None:
                self._poller.watch([c.path for c in self._caps.values() if c.path is not None])
            for scope in self._scopes.values():
                self._read_scope(scope, now)
            victim = None
            for key, cap in self._caps.items():
                if self._resume(key, cap, used.get(key), now):
                    continue
                victim = victim or self._due(key, cap, used.get(key), now)
            peers = None if victim is None else self._freeze(victim, now)
            self._arm()
            self._save()
            changed = self._shape() != before
        if victim is not None and peers is not None:
            self._reclaim(victim)
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
        if not _meminfo(self._proc).get("SwapTotal"):
            return "no swap, so nowhere to put a parked command's memory"
        if self._slice.is_dir() and not (self._slice / "memory.reclaim").exists():
            return (
                "no memory.reclaim (Linux 5.19 or later has it), so no way to move a parked "
                "command's memory to swap"
            )
        return None

    def _track(self, held: list[Held], now: float) -> None:
        """Start watching the scopes of new leases; let go of those whose lease or scope is
        gone. A scope a net before this one froze is adopted as parked while its lease is
        held, and resumed otherwise."""
        live = dict(held)
        for lease_id in list(self._scopes):
            scope = self._scopes[lease_id]
            if lease_id in live and scope.path.is_dir():
                continue
            if scope.parked_at is not None:
                self._thaw(scope, now, settle=False)
            if scope.limited:
                _write(scope.path / "memory.swap.max", "max")
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
            scope = _Scope(lease_id, path, granted_at, self._cap_of(path))
            self._scopes[lease_id] = scope
            if path in self._unlimit or path in self._adopt:
                scope.limit = "?"  # a net before set it: `_arm` writes it anew
            self._unlimit.discard(path)
            if path in self._adopt and (_read(path / "cgroup.freeze") or "").strip() == "1":
                self._read_scope(scope, now)
                scope.parked_at, scope.parked_wall = now, self._clock()
                scope.pre_park, scope.limit = scope.footprint, "max"
                self._counts.setdefault(lease_id, [1, 0.0])
            self._adopt.discard(path)
        # What the record names and no held lease owns: the first step resumes and unlimits it.
        for path in self._adopt:
            _write(path / "cgroup.freeze", "0")
        for path in self._adopt | self._unlimit:
            _write(path / "memory.swap.max", "max")
        self._adopt, self._unlimit = set(), set()

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
        """Each cap's use now, in bytes; its stall total goes in its window, and a new
        `memory.events` count marks it pressed."""
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
                events = _events(cap.path / "memory.events")
                if events is not None and cap.events is not None and events > cap.events:
                    cap.pressed_at = now
                cap.events = events
            if stall is not None:
                cap.stall.add(now, stall)
        return used

    def _read_scope(self, scope: _Scope, now: float) -> None:
        scope.current = _int(scope.path / "memory.current") or 0
        scope.swapped = _int(scope.path / "memory.swap.current") or 0
        scope.base = min(scope.base, scope.swapped)
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

    def _heavy(self, key: str) -> list[_Scope]:
        """The cap's running heavy scopes."""
        return [s for s in self._in(key) if s.parked_at is None and s.footprint >= HEAVY_BYTES]

    def _armed(self, key: str) -> bool:
        return (
            self.reason is None
            and self._written_day() < self.write_cap
            and len(self._heavy(key)) >= 2
        )

    def _arm(self) -> None:
        """Set each running scope's swap limit: `overflow` above its base in an armed cap,
        `max` elsewhere. A scope whose reclaim is under way keeps its limit lifted."""
        if self.overflow is None:
            return
        for key in self._caps:
            armed = self._armed(key)
            for scope in self._in(key):
                if scope.parked_at is not None or scope.lease_id in self._reclaiming:
                    continue
                if armed and not scope.limited:
                    scope.base = scope.swapped
                want = str(scope.base + self.overflow) if armed else "max"
                if want == "max" and scope.limit is None:
                    scope.limit = want  # never limited: leave it as it was found
                elif want != scope.limit and _write(scope.path / "memory.swap.max", want):
                    scope.limit = want

    def _resume(self, key: str, cap: _Cap, used: int | None, now: float) -> bool:
        """Resume the cap's longest-parked scope when it is due; whether one was."""
        parked = sorted(
            (s for s in self._in(key) if s.parked_at is not None),
            key=lambda s: s.parked_at or 0.0,
        )
        if not parked:
            return False
        first = parked[0]
        fits = used is not None and used + first.swapped < THAW_AT * cap.limit
        if self._heavy(key) and not fits:
            return False
        self._thaw(first, now, settle=True)
        cap.quiet_until = now + GAP_S
        return True

    def _due(self, key: str, cap: _Cap, used: int | None, now: float) -> _Scope | None:
        """The scope to park in the cap now, if a park is due there."""
        if now < cap.quiet_until or not self._armed(key):
            return None
        near = used is not None and used >= NEAR * cap.limit
        if not near and now - cap.pressed_at > SLOW_WINDOW_S:
            return None
        running = [s for s in self._in(key) if s.parked_at is None]
        due = False
        for span in (WINDOW_S, SLOW_WINDOW_S):
            spilling = any(
                (rise := s.swap.rise(span)) is not None and rise[0] >= SPILL_BYTES for s in running
            )
            due = due or (spilling and _stalled(cap.stall.rise(span)))
        if not due and self.overflow is not None:
            full = any(
                s.limited and s.swapped - s.base >= FULL_SHARE * self.overflow for s in running
            )
            stall = cap.stall.rise(SLOW_WINDOW_S) or cap.stall.rise(WINDOW_S)
            due = full and _stalled(stall)
        if not due:
            return None
        return max(self._heavy(key), key=lambda s: s.granted_at)

    def _freeze(self, scope: _Scope, now: float) -> list[_Scope] | None:
        """Freeze `scope` and lift its swap limit and its cap's other running scopes'; those
        others. None when the scope could not be frozen (its command has just ended)."""
        if not _write(scope.path / "cgroup.freeze", "1"):
            return None
        scope.parked_at, scope.parked_wall = now, self._clock()
        scope.thawed_at = scope.thawed_wall = scope.calm_since = scope.last = None
        scope.pre_park, scope.resident = scope.footprint, scope.current
        self.parks += 1
        self._counts.setdefault(scope.lease_id, [0, 0.0])[0] += 1
        self._events.append({"type": "parked", "lease_id": scope.lease_id})
        cap = self._caps[scope.cap]
        cap.stall.clear()
        cap.quiet_until = now + GAP_S
        peers = [s for s in self._in(scope.cap) if s.parked_at is None]
        for s in [scope, *peers]:
            s.swap.clear()
            if s.limited:
                _write(s.path / "memory.swap.max", "max")
            s.limit = "max"
            self._reclaiming.add(s.lease_id)
        self._reclaiming.discard(scope.lease_id)
        return peers

    def _thaw(self, scope: _Scope, now: float, *, settle: bool) -> None:
        _write(scope.path / "cgroup.freeze", "0")
        if scope.parked_at is not None:
            counts = self._counts.setdefault(scope.lease_id, [0, 0.0])
            counts[1] += now - scope.parked_at
        scope.parked_at = scope.parked_wall = None
        if settle:
            scope.thawed_at, scope.thawed_wall = now, self._clock()
        scope.swap.clear()
        scope.limit = "max"  # as `_freeze` left it; `_arm` sets it anew
        self._events.append({"type": "resumed", "lease_id": scope.lease_id})

    def _written_day(self) -> int:
        since = self._clock() - DAY_S
        while self._writes and self._writes[0][0] < since:
            self._writes.popleft()
        return sum(n for _, n in self._writes)

    def _save(self) -> None:
        """Write the record when what it says has changed."""
        if self._state is None:
            return
        record = {
            "frozen": sorted(str(s.path) for s in self._scopes.values() if s.parked_at is not None),
            "limited": sorted(
                str(s.path)
                for s in self._scopes.values()
                if s.limited or s.lease_id in self._reclaiming
            ),
            "writes": [list(w) for w in self._writes],
        }
        if record == self._saved:
            return
        with contextlib.suppress(OSError):
            temp = self._state.with_suffix(".tmp")
            temp.write_text(json.dumps(record))
            temp.replace(self._state)
            self._saved = record

    # --- outside the lock ------------------------------------------------------------------

    def _reclaim(self, scope: _Scope) -> None:
        """Move the parked `scope`'s memory out to swap (blocking until the kernel is done),
        then let `_arm` set its peers' swap limits again."""
        with self._lock:
            want = min(scope.resident, max(0, self.write_cap - self._written_day()))
        before = _int(scope.path / "memory.swap.current") or 0
        try:
            if want > 0:
                _write(scope.path / "memory.reclaim", str(want))
        finally:
            after = _int(scope.path / "memory.swap.current")
            written = max(0, (after or before) - before)
            with self._lock:
                self._writes.append((self._clock(), written))
                self.written += written
                self._reclaiming.clear()
                self._arm()
                self._save()


def release(state: Path | None) -> None:
    """With the net off: resume every scope the record at `state` says a net froze, put every
    swap limit it set back to `max`, and forget the record."""
    record = _load(state)
    for path in record.get("frozen", []):
        _write(Path(path) / "cgroup.freeze", "0")
    for path in {*record.get("frozen", []), *record.get("limited", [])}:
        _write(Path(path) / "memory.swap.max", "max")
    if state is not None and record:
        with contextlib.suppress(OSError):
            state.unlink()


def _load(state: Path | None) -> dict:
    if state is None:
        return {}
    try:
        record = json.loads(state.read_text())
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def _stalled(rise: tuple[int, float] | None) -> bool:
    return rise is not None and rise[0] / 1e6 >= STALL_SHARE * rise[1]


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
