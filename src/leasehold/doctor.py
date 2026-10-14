"""`leasehold doctor`: whether this machine and this user can run and reach a daemon, and what
to do about each problem.

Each check returns a `Check`. `fail` is something that stops leasehold working (a config that
does not parse, no daemon to reach); `warn` is something that makes it work less well (no PSI,
so lending falls back to the load average). The command exits 1 when any check fails.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from leasehold import contain, discovery, machine, paths
from leasehold.client import ApiError, Client, Unavailable

Level = Literal["ok", "warn", "fail"]

#: Clock skew between this process and the daemon above which leases look wrongly timed.
MAX_SKEW_S = 2.0
#: The longest path a unix socket may have: `sun_path` holds 108 bytes, the last a NUL.
MAX_SOCKET_PATH = 107


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str
    fix: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Roots:
    """Where the kernel's files are, so a test can point at a fake tree."""

    proc: Path = Path("/proc")
    cgroup: Path = Path("/sys/fs/cgroup")


def check_config(explicit: str | None, env: Mapping[str, str]) -> tuple[Check, object]:
    """The config's check, and the config resolved for its mode (None when there is none or it
    is broken)."""
    try:
        path = paths.find_config(explicit, env)
    except FileNotFoundError as exc:
        return Check("config", "fail", str(exc), "fix the path, or leave it out"), None
    if path is None:
        where = " or ".join(str(p) for p in paths.config_candidates(None, env))
        return (
            Check(
                "config",
                "ok",
                f"none ({where}); a daemon runs on defaults: one slot per CPU, the default pools",
                "`leasehold init` writes one to edit",
            ),
            None,
        )
    try:
        config = machine.resolve(machine.load(path), None, env)
    except (OSError, ValueError) as exc:
        return Check("config", "fail", f"{path}: {exc}", "correct it; see docs/reference.md"), None
    detail = f"{path} ({config.mode} daemon, {config.ceiling.slots} slots)"
    return Check("config", "ok", detail), config


def check_socket(
    socket: str | None, explicit: str | None, env: Mapping[str, str]
) -> tuple[Check, Path | None]:
    """The socket's check, and the socket a client would use (None: no daemon answers)."""
    try:
        found = discovery.candidates(socket, explicit, env)
    except discovery.DiscoveryError as exc:
        return Check("socket", "fail", str(exc), "fix the config, or name --socket"), None
    tried = "; ".join(f"{c.source} {c.path}: {c.reason}" for c in found)
    chosen = discovery.choose(found)
    if chosen is None:
        fix = (
            "start one: `leasehold serve` (yours), or `systemctl enable --now leasehold` "
            "(the machine's)"
        )
        return Check("socket", "fail", f"no daemon answers ({tried})", fix), None
    return Check("socket", "ok", f"{chosen} ({tried})"), chosen


def check_socket_paths(paths_: list[Path]) -> Check | None:
    """A fail naming each of `paths_` too long for a unix socket, or None when all fit."""
    long = [p for p in dict.fromkeys(paths_) if len(os.fsencode(p)) > MAX_SOCKET_PATH]
    if not long:
        return None
    named = ", ".join(f"{p} ({len(os.fsencode(p))} bytes)" for p in long)
    return Check(
        "socket",
        "fail",
        f"longer than a unix socket path may be ({MAX_SOCKET_PATH} bytes): {named}",
        "set `socket` in the config, or --socket / $LEASEHOLD_SOCKET, to a shorter path, such "
        "as one in $XDG_RUNTIME_DIR or /run",
    )


def check_socket_mode(config) -> Check | None:
    """A warning when the config's `socket_mode` lets every user reach the socket."""
    if config is None or config.socket_mode is None:
        return None
    mode = int(config.socket_mode, 8)
    if not mode & 0o007:
        return None
    return Check(
        "access",
        "warn",
        f"socket_mode {mode:04o} lets every user on the machine reach the daemon",
        "use 0660 and a socket_group for the users who may (0600 for your own daemon)",
    )


#: What to do about each reason `contain.probe` gives for not containing, by its start.
_CONTAIN_FIXES = (
    (contain.ENV, f"unset {contain.ENV} to contain commands again"),
    ("no cgroup v2", "boot with systemd.unified_cgroup_hierarchy=1"),
    (
        "caller in",
        "expected from a service or CI job; from your own shell, log in (ssh, a desktop) so "
        "commands start under user.slice",
    ),
    ("no systemd user manager", "enable lingering (`loginctl enable-linger`) or log in again"),
    (
        "user manager is not delegated",
        "delegate them: `systemctl edit user@.service`, [Service] Delegate=cpu memory",
    ),
    ("the user manager cannot be reached", "set XDG_RUNTIME_DIR to /run/user/<uid>"),
    ("systemd-run", "install systemd (it provides systemd-run)"),
)


def check_contain(config, env: Mapping[str, str], probe=contain.probe) -> Check:
    """Whether commands `hold` starts here go in a systemd user scope sized to their lease."""
    if config is not None and config.contain.mode == "off":
        return Check("contain", "ok", 'off ([contain] mode = "off"): leases are advisory')
    found = probe(env=env)
    if found.enforced:
        return Check("contain", "ok", f"{found.describe()}: commands run in scopes sized to leases")
    fix = next((f for start, f in _CONTAIN_FIXES if found.reason.startswith(start)), None)
    detail = f"{found.describe()}; a lease counts its command but does not hold it back"
    return Check("contain", "warn", detail, fix or "see Containment in docs/reference.md")


#: A `[commands] wait_s` below this refuses most heavy commands on a busy machine before their
#: turn comes (docs/bench.md: at 8 agents the slowest waits were about 100 s).
SHORT_WAIT_S = 120.0


def check_commands(config) -> Check | None:
    """Whether agents' heavy commands wait long enough for their turn before being refused."""
    if config is None or config.commands.when_busy != "deny":
        return None
    wait = config.commands.wait_s
    if wait >= SHORT_WAIT_S:
        return Check("commands", "ok", f"a busy machine refuses a heavy command after {wait:g}s")
    return Check(
        "commands", "warn",
        f"[commands] wait_s = {wait:g}: on a busy machine most heavy commands are refused "
        "before their turn comes",
        "set `wait_s = 300` under [commands] in the config (0.2.0's `leasehold init` wrote 20)",
    )  # fmt: skip


def check_daemon(path: Path, now: Callable[[], float] = time.time) -> list[Check]:
    """Whether the daemon at `path` answers its status, and whether its clock agrees."""
    try:
        status = Client(path, timeout=3.0).status()
    except Unavailable as exc:
        if isinstance(exc.__cause__, PermissionError):
            fix = "ask to be added to the socket's group (usually `leasehold`), then log in again"
        else:
            fix = "check `journalctl -u leasehold` or the terminal it runs in"
        return [Check("daemon", "fail", f"{path}: {exc}", fix)]
    except ApiError as exc:
        return [Check("daemon", "fail", f"{path}: {exc}", None)]
    held, slots = status["held"], status["ceiling"]["slots"]
    checks = [
        Check(
            "daemon",
            "ok",
            f"{status['machine']['name']}, leasehold {status['version']}, {held} of {slots} "
            f"slots held, {len(status['queue'])} waiting",
        )
    ]
    skew = abs(status["time"] - now())
    if skew > MAX_SKEW_S:
        checks.append(
            Check(
                "clock",
                "warn",
                f"the daemon's clock is {skew:.1f}s from this one",
                "run NTP (systemd-timesyncd or chrony) on this machine",
            )
        )
    return checks


def check_psi(roots: Roots) -> Check:
    missing = [r for r in ("cpu", "memory", "io") if not (roots.proc / "pressure" / r).is_file()]
    if missing:
        return Check(
            "pressure",
            "warn",
            f"no PSI for {', '.join(missing)}: lending is judged by the load average instead",
            "boot a kernel with CONFIG_PSI=y (add psi=1 to the kernel command line if it is off)",
        )
    return Check("pressure", "ok", "PSI available for cpu, memory and io")


def check_cgroups(roots: Roots, config) -> list[Check]:
    if not (roots.cgroup / "cgroup.controllers").is_file():
        return [
            Check(
                "cgroups",
                "warn",
                "not cgroup v2: per-slice load and lend.pressure_cgroups are unavailable",
                "boot with systemd.unified_cgroup_hierarchy=1",
            )
        ]
    checks = [Check("cgroups", "ok", "cgroup v2")]
    wanted = list(config.lend.pressure_cgroups) + list(config.load.slices) if config else []
    absent = [name for name in wanted if not (roots.cgroup / name).is_dir()]
    if absent:
        checks.append(
            Check(
                "slices",
                "warn",
                f"configured but absent: {', '.join(absent)}",
                "install the slice units (deploy/*.slice) and `systemctl daemon-reload`; a "
                "slice appears once something runs in it",
            )
        )
    return checks


def check_proc(roots: Roots, config) -> Check:
    """Whether this process sees other users' processes, which `/proc` mounted `hidepid` hides
    from all but root and the mount's `gid=` group. A hidden holder is judged by its pid alone
    (its birth cannot be read), and `[[observe]]` rules cannot see it at all."""
    try:
        mounts = (roots.proc / "mounts").read_text()
    except OSError:
        return Check("proc", "warn", "cannot read /proc/mounts to see what /proc hides")
    options: list[str] = []
    for line in mounts.splitlines():
        fields = line.split()
        if len(fields) > 3 and fields[2] == "proc" and fields[1] == "/proc":
            options = fields[3].split(",")
    hidepid = next((o.split("=", 1)[1] for o in options if o.startswith("hidepid=")), "0")
    if hidepid in ("0", "off") or os.geteuid() == 0:
        return Check("proc", "ok", "/proc shows every user's processes")
    gid = next((o.split("=", 1)[1] for o in options if o.startswith("gid=")), None)
    if gid is not None and gid.isdigit() and int(gid) in os.getgroups():
        return Check("proc", "ok", f"/proc is hidepid={hidepid}; this user is in its group")
    observing = config is not None and bool(getattr(config, "observe", None))
    effect = "[[observe]] rules see only this user's processes, and " if observing else ""
    return Check(
        "proc",
        "warn",
        f"/proc is mounted hidepid={hidepid}: {effect}other users' lease holders are judged "
        "by pid alone",
        fix="run the daemon as root, or in the group named by the /proc mount's gid= option",
    )


def check_ledger(config) -> Check | None:
    """Whether a daemon run by this user could keep its ledger where the config says. The
    system daemon's runs as another user, so only its default directory is vouched for."""
    if config is None:
        return None
    directory = Path(config.ledger_dir)
    if config.mode == "system":
        if directory == paths.SYSTEM_LEDGER_DIR:
            return Check("ledger", "ok", f"{directory} (systemd's StateDirectory makes it)")
        return Check(
            "ledger",
            "warn",
            f"{directory}: a custom ledger_dir, which this check cannot verify for the "
            "leasehold user",
            f"make {directory} owned by the leasehold user, mode 0700",
        )
    probe = directory if directory.exists() else directory.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return Check(
            "ledger",
            "fail",
            f"{directory} is not writable by uid {os.getuid()}",
            f"make {directory} yours, or set ledger_dir",
        )
    return Check("ledger", "ok", f"{directory}")


def run(
    *,
    socket: str | None,
    config: str | None,
    env: Mapping[str, str],
    roots: Roots | None = None,
) -> list[Check]:
    """Every check, in the order they are worth reading."""
    roots = roots or Roots()
    config_check, resolved = check_config(config, env)
    checks = [config_check]
    ledger = check_ledger(resolved)
    if ledger is not None:
        checks.append(ledger)
    socket_check, chosen = check_socket(socket, config, env)
    checks.append(socket_check)
    named = [Path(resolved.socket)] if resolved is not None else []
    with contextlib.suppress(discovery.DiscoveryError):
        named += [c.path for c in discovery.candidates(socket, config, env) if str(c.path) != "-"]
    for extra in (check_socket_paths(named), check_socket_mode(resolved)):
        if extra is not None:
            checks.append(extra)
    if chosen is not None:
        checks += check_daemon(chosen)
    checks.append(check_psi(roots))
    checks.append(check_proc(roots, resolved))
    checks += check_cgroups(roots, resolved)
    checks.append(check_contain(resolved, env))
    commands = check_commands(resolved)
    if commands is not None:
        checks.append(commands)
    return checks
