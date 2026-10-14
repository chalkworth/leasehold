"""`leasehold setup`: everything a user's own leasehold needs, in one step, and its undoing.

Each step says what it did, and each is safe to repeat:

1. **Config.** `~/.config/leasehold/machine.toml` (`leasehold init`'s, sized for the machine),
   unless one is there already; an existing config is never touched, only checked for
   leasehold 0.2's defaults (`leasehold.upgrade`), which `setup --upgrade-config` replaces
   when asked.
2. **Daemon.** A systemd user unit, `~/.config/systemd/user/leasehold.service`, running this
   installation's `leasehold serve --user`, enabled and started. Without a user manager, it
   says how to start a daemon by hand.
3. **Shims.** The PATH shims for the heavy programs the config's rules name and PATH has, in
   `~/.local/share/leasehold/shims`. `leasehold run` puts them first on its agent's PATH.
4. **Agents.** The coding agents found on PATH, and how to give each one its integration.
5. **Check.** Whether commands are contained here, and what `leasehold doctor` finds wrong.

`remove` stops and deletes the unit and the shims, and leaves the config and the ledger.
`upgrade_config` brings a config from 0.2's defaults to today's, the old file kept beside it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from leasehold import contain, doctor, machine, paths, shims, template, upgrade
from leasehold.client import Client
from leasehold.facts import probe

Say = Callable[[str], None]
UNIT = "leasehold.service"
#: Agents `setup` looks for, and the line that integrates each.
AGENTS = {
    "claude": "in Claude Code: /plugin marketplace add chalkworth/leasehold, then "
    "/plugin install leasehold@leasehold; start it with `leasehold run -- claude`",
    "codex": "start it with `leasehold run -- codex`",
    "gemini": "start it with `leasehold run -- gemini`",
    "cursor-agent": "start it with `leasehold run -- cursor-agent`",
    "aider": "start it with `leasehold run -- aider`",
}


def unit_path(env: Mapping[str, str]) -> Path:
    config = env.get("XDG_CONFIG_HOME") or os.path.join(env.get("HOME", "~"), ".config")
    return Path(config).expanduser() / "systemd" / "user" / UNIT


def unit_text(executable: str) -> str:
    """The user unit that runs `executable serve --user`."""
    return (
        "# Written by `leasehold setup`; `leasehold setup --remove` deletes it.\n"
        "[Unit]\n"
        "Description=leasehold daemon (your own)\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={executable} serve --user\n"
        "Restart=on-failure\n"
        "RestartSec=2\n"
        "TimeoutStopSec=15\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def executable() -> str:
    """The `leasehold` this is, for the unit to run."""
    found = shutil.which("leasehold")
    if found and Path(found).resolve().parent == Path(sys.executable).resolve().parent:
        return found
    beside = Path(sys.executable).parent / "leasehold"
    return str(beside) if beside.exists() else (found or str(beside))


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False, timeout=60
    )


def _manager(env: Mapping[str, str]) -> bool:
    if shutil.which("systemctl", path=env.get("PATH", os.defpath)) is None:
        return False
    try:
        return _systemctl("show", "--property=Version").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def write_config(env: Mapping[str, str], say: Say) -> Path:
    path = paths.user_config(env)
    if path.exists():
        say(f"config: {path} is there already; left as it is")
        try:
            lacks = upgrade.outdated(machine.load(path))
        except (OSError, ValueError):
            lacks = []  # `check` reports a config that does not load
        if lacks:
            say(f"config: it has leasehold 0.2's defaults: {'; '.join(lacks)}")
            say(
                "config: `leasehold setup --upgrade-config --dry-run` shows the change; "
                "without --dry-run it makes it, keeping the old file beside it"
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o644), "w") as out:
        out.write(template.sized("user", probe()))
    config = machine.load(path)
    kept = (probe().get("cpus_logical") or os.cpu_count() or 1) - config.ceiling.slots
    slots = config.ceiling.slots
    say(f"config: wrote {path}: {slots} slots for leased work, {kept} CPUs kept for you")
    return path


def start_daemon(env: Mapping[str, str], say: Say) -> bool:
    """Install, enable and start the unit; whether a daemon answers afterwards."""
    if not _manager(env):
        say("daemon: no systemd user manager here; start one with `leasehold serve &`")
        return False
    unit = unit_path(env)
    unit.parent.mkdir(parents=True, exist_ok=True)
    text = unit_text(executable())
    if not unit.exists() or unit.read_text() != text:
        unit.write_text(text)
    for args in (("daemon-reload",), ("enable", "--now", UNIT), ("restart", UNIT)):
        done = _systemctl(*args)
        if done.returncode != 0:
            say(f"daemon: systemctl --user {' '.join(args)} failed: {done.stderr.strip()}")
            return False
    socket = paths.user_socket(env)
    deadline = time.monotonic() + 10
    while socket is not None and time.monotonic() < deadline:
        try:
            Client(socket).status()
            say(f"daemon: running ({UNIT}, socket {socket})")
            return True
        except Exception:  # noqa: BLE001 - not up yet, whatever the reason
            time.sleep(0.2)
    say(f"daemon: started, but nothing answers on {socket}; see `journalctl --user -u {UNIT}`")
    return False


def install_shims(env: Mapping[str, str], config_path: Path, say: Say) -> None:
    rules = machine.load(config_path).commands.shell_rules()
    named = sorted({p for rule in rules for p in rule.programs if "/" not in p})
    directory = shims.default_dir(env)
    path = env.get("PATH", os.defpath)
    present = [p for p in named if shims.real(p, path, directory) is not None]
    done = shims.install(directory, present, python=sys.executable)
    made = [program for program, what in done if what == "installed"]
    say(f"shims: {len(made)} in {directory} ({', '.join(made) or 'none'})")


def find_agents(env: Mapping[str, str], say: Say) -> None:
    path = env.get("PATH", os.defpath)
    found = [name for name in AGENTS if shutil.which(name, path=path)]
    if not found:
        say("agents: none found on PATH; start any with `leasehold run -- AGENT`")
    for name in found:
        say(f"agents: {name}: {AGENTS[name]}")


def check(env: Mapping[str, str], say: Say) -> int:
    found = contain.probe(env=env)
    say(f"containment: {found.describe()}")
    problems = [c for c in doctor.run(socket=None, config=None, env=env) if c.level == "fail"]
    for c in problems:
        say(f"doctor: {c.name}: {c.detail}" + (f" (fix: {c.fix})" if c.fix else ""))
    return 1 if problems else 0


def setup(env: Mapping[str, str], say: Say) -> int:
    """Every step in order (module docstring); 0 when `doctor` then finds nothing failing."""
    config_path = write_config(env, say)
    start_daemon(env, say)
    install_shims(env, config_path, say)
    find_agents(env, say)
    code = check(env, say)
    say("undo with `leasehold setup --remove`")
    return code


def upgrade_config(path: Path | None, env: Mapping[str, str], say: Say, *, dry_run: bool) -> int:
    """Bring the config at `path` (default: the user's) to today's defaults: say what changes
    and show the diff, then, unless `dry_run`, write it with the old file kept beside it. 0
    when it was written, lacked nothing or (`dry_run`) could be; 1 when it cannot be edited
    automatically or does not load."""
    path = path or paths.user_config(env)
    try:
        config = machine.load(path)
    except (OSError, ValueError) as exc:
        say(f"config: {exc}")
        return 1
    found = upgrade.plan(path, config)
    if found is None:
        say(f"config: {path} has today's defaults; nothing to change")
        return 0
    if found.text is None:
        say(f"config: {path} cannot be changed automatically: {found.manual}")
        return 1
    for change in found.changes:
        say(f"config: {change}")
    for line in found.diff().splitlines():
        say(line)
    if dry_run:
        say("config: nothing written (--dry-run)")
        return 0
    try:
        backup = upgrade.apply(found)
    except (OSError, ValueError) as exc:
        say(f"config: not written: {exc}")
        return 1
    say(f"config: {path} upgraded; the old file is {backup}")
    say(f"config: restart your daemon to use it (`systemctl --user restart {UNIT}`)")
    return 0


def remove(env: Mapping[str, str], say: Say) -> int:
    unit = unit_path(env)
    if unit.exists():
        if _manager(env):
            _systemctl("disable", "--now", UNIT)
        unit.unlink()
        if _manager(env):
            _systemctl("daemon-reload")
        say(f"daemon: stopped, and {unit} removed")
    else:
        say("daemon: no unit of setup's to remove")
    shim_dir = shims.default_dir(env)
    gone = shims.remove(shim_dir)
    for directory in (shim_dir, shim_dir.parent):  # only when empty: never what else is there
        with contextlib.suppress(OSError):
            directory.rmdir()
    say(f"shims: removed {len(gone)}")
    with_config = paths.user_config(env)
    if with_config.exists():
        say(f"config: {with_config} left in place (delete it yourself if you want it gone)")
    return 0
