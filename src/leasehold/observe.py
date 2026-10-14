"""Programs that run outside leasehold but are known to be heavy: `[[observe]]` rules.

A rule names a program by what a process looks like (`match`: its `comm`, executable, command
line or user) and says what it costs (`weight`). On every tick the daemon takes one snapshot of
the process table (`leasehold.proctable`) and imposes an `observed` lease (`Ledger.impose`) for
each matching process it has not seen yet. The lease is fenced by that process's pid and start
time, so it ends when the process does, and a reused pid never keeps it.

Only a rule's **roots** count: a matching process none of whose ancestors matches the same rule.
A training job's worker processes are part of the one job its parent started. A process whose
parent vanished mid-scan counts as a root. `max_count` caps how many processes one rule
counts at once (the oldest first). A leasehold command, and anything run under one (`hold`,
the PATH shims), is never observed: it holds a lease of its own, or is waiting for one.

Observing is accounting, not control: the program has already started, so its weight is held
even past its pool's `max` or the machine's slots, and new work waits until enough of it ends.
It is seen within one tick (`tick_s`, 2 s by default).
"""

from __future__ import annotations

import pwd
import re
from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from leasehold.model import Name
from leasehold.proctable import Proc


class Match(BaseModel):
    """What a process must look like; every field given must match. `cmdline` and `exe` are
    regular expressions searched anywhere in the value (anchor them with `^`, `$`); `comm` (the
    kernel's 15-character process name) and `user` are compared exactly."""

    model_config = ConfigDict(extra="forbid")

    comm: str | None = None
    exe: str | None = None
    cmdline: str | None = None
    user: str | None = None

    @field_validator("exe", "cmdline")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"{value!r} is not a regular expression: {exc}") from None
        return value

    @model_validator(mode="after")
    def _something(self) -> Match:
        if not any((self.comm, self.exe, self.cmdline, self.user)):
            raise ValueError("match needs at least one of comm, exe, cmdline, user")
        return self


class ObserveRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    match: Match
    weight: int = Field(ge=1)
    #: The pool it is charged to; None: the observed pool (`[kinds] observed`).
    pool: Name | None = None
    #: The most processes this rule counts at once; None is no limit.
    max_count: int | None = Field(None, ge=1)

    def matches(self, proc: Proc, uid_of: Mapping[str, int | None]) -> bool:
        m = self.match
        if m.user is not None and uid_of.get(m.user) != proc.uid:
            return False
        if m.comm is not None and proc.comm != m.comm:
            return False
        if m.exe is not None and not (proc.exe and re.search(m.exe, proc.exe)):
            return False
        return m.cmdline is None or bool(re.search(m.cmdline, proc.cmdline))


def uid_table(rules: list[ObserveRule]) -> dict[str, int | None]:
    """The uid of every user a rule names (None for one that does not exist)."""
    out: dict[str, int | None] = {}
    for rule in rules:
        name = rule.match.user
        if name is not None and name not in out:
            try:
                out[name] = pwd.getpwnam(name).pw_uid
            except KeyError:
                out[name] = None
    return out


def is_leasehold(proc: Proc) -> bool:
    """Whether `proc` is a leasehold command (`hold`, a shim, the daemon): run as `leasehold`,
    as `python .../leasehold`, or as `python -m leasehold...`."""
    if proc.comm == "leasehold":
        return True
    names = [w.rsplit("/", 1)[-1] for w in proc.cmdline.split()]
    if not names:
        return False
    if names[0] == "leasehold":
        return True
    if not names[0].startswith("python"):
        return False
    rest = iter(names[1:])
    for word in rest:  # the interpreter's options, then its script or module
        if word == "-m":
            return next(rest, "").split(".")[0] == "leasehold"
        if word in ("-W", "-X", "--check-hash-based-pycs"):
            next(rest, None)  # an option with a value
        elif word == "-c" or not word.startswith("-"):
            return word == "leasehold"  # the installed script, run by its interpreter
    return False


def under_leasehold(procs: Mapping[int, Proc]) -> Callable[[int], bool]:
    """Whether a pid is a leasehold command or runs under one, remembered across calls. Work
    under `hold` already holds its lease, and a `hold` still waiting is not the program its
    arguments name, so neither is observed."""
    memo: dict[int, bool] = {}

    def under(pid: int) -> bool:
        chain = []
        while pid in procs and pid not in memo and pid not in chain:
            if is_leasehold(procs[pid]):
                memo[pid] = True
                break
            chain.append(pid)
            pid = procs[pid].ppid
        result = memo.get(pid, False)
        for p in chain:
            memo[p] = result
        return result

    return under


def roots(
    rule: ObserveRule,
    procs: Mapping[int, Proc],
    uid_of: Mapping[str, int | None],
    leased: Callable[[int], bool] = lambda pid: False,
):
    """The processes `rule` counts now, oldest first, at most `max_count`, none that `leased`
    (`under_leasehold`) says runs under leasehold."""
    memo: dict[int, bool] = {}

    def hit(pid: int) -> bool:
        if pid not in memo:
            proc = procs.get(pid)
            memo[pid] = proc is not None and rule.matches(proc, uid_of)
        return memo[pid]

    found = []
    for pid, proc in procs.items():
        if not hit(pid) or leased(pid):
            continue
        parent, seen = proc.ppid, {pid}
        while parent in procs and parent not in seen and not hit(parent):
            seen.add(parent)
            parent = procs[parent].ppid
        if parent in procs and parent not in seen and hit(parent):
            continue  # part of an ancestor's run
        found.append(proc)
    found.sort(key=lambda p: (p.start, p.pid))
    return found if rule.max_count is None else found[: rule.max_count]


def item_id(rule: ObserveRule, proc: Proc) -> str:
    """The lease id an observed process is held under: unique to that process's lifetime."""
    return f"observed.{rule.name}.{proc.pid}.{proc.start}"
