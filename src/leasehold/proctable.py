"""One snapshot of the machine's processes, read from procfs: what `[[observe]]` rules match.

Each `Proc` has its pid and start time (`/proc/<pid>/stat` field 22, the birth token
`leasehold.process` fences holders by), parent, owner, `comm`, executable and command line. A
process that exits mid-scan is left out; one whose executable this user may not read has `exe`
None. Reading is cheap enough for every tick: `stat` and `status` for each process, `cmdline`
and `exe` only when a rule asks (`Proc.cmdline`, `Proc.exe` are read on first use).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Proc:
    pid: int
    ppid: int
    uid: int
    start: int
    comm: str
    root: Path = field(default=Path("/proc"), repr=False)
    _cmdline: str | None = field(default=None, repr=False)
    _exe: str | None = field(default=None, repr=False)
    _read: set[str] = field(default_factory=set, repr=False)

    @property
    def cmdline(self) -> str:
        """The command line, arguments joined by spaces ('' for a kernel thread)."""
        if "cmdline" not in self._read:
            self._read.add("cmdline")
            try:
                raw = (self.root / str(self.pid) / "cmdline").read_bytes()
                self._cmdline = raw.rstrip(b"\0").replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                self._cmdline = ""
        return self._cmdline or ""

    @property
    def exe(self) -> str | None:
        """The executable's path, or None when it cannot be read (another user's process)."""
        if "exe" not in self._read:
            self._read.add("exe")
            try:
                self._exe = os.readlink(self.root / str(self.pid) / "exe")
            except OSError:
                self._exe = None
        return self._exe


def _read_proc(root: Path, pid: int) -> Proc | None:
    try:
        stat = (root / str(pid) / "stat").read_text(encoding="utf-8", errors="replace")
        status = (root / str(pid) / "status").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None  # gone, or a thread group we may not see
    try:
        comm = stat[stat.index("(") + 1 : stat.rindex(")")]
        fields = stat[stat.rindex(")") + 2 :].split()
        ppid, start = int(fields[1]), int(fields[19])  # stat fields 4 and 22
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        uid = int(uid_line.split()[1])  # the real uid
    except (ValueError, IndexError, StopIteration):
        return None
    if fields[0] == "Z":
        return None  # a zombie runs nothing
    return Proc(pid=pid, ppid=ppid, uid=uid, start=start, comm=comm, root=root)


def scan(root: Path = Path("/proc")) -> dict[int, Proc]:
    """Every live process now, by pid."""
    procs = {}
    try:
        names = os.listdir(root)
    except OSError:
        return procs
    for name in names:
        if name.isdigit():
            proc = _read_proc(root, int(name))
            if proc is not None:
                procs[proc.pid] = proc
    return procs
