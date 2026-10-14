"""What this machine is: cheap, side-effect-free facts a machine advertises.

Architecture, logical and physical cores, total memory, OS and kernel, whether `/dev/kvm` is
there (and usable by this process), whether `docker` and `runsc` are installed, whether it is a
VM, and whether cgroup v2 and PSI are there. Every root (`proc`, `sys`, `dev`, `etc`) is
injectable, so tests read fixture trees; anything that cannot be read is None, never an error.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

#: The version of `probe`'s shape, for whatever consumes it.
FACTS_SCHEMA = 1


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return None


def meminfo(proc: Path) -> dict[str, int]:
    """`/proc/meminfo` in bytes, by field name."""
    out: dict[str, int] = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            scale = 1024 if len(parts) > 1 and parts[1] == "kB" else 1
            out[name.strip()] = int(parts[0]) * scale
    return out


def _cpuinfo(proc: Path) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = [{}]
    for line in (_read(proc / "cpuinfo") or "").splitlines():
        if not line.strip():
            if blocks[-1]:
                blocks.append({})
            continue
        key, _, value = line.partition(":")
        blocks[-1][key.strip()] = value.strip()
    return [b for b in blocks if b]


def cores(proc: Path, sys: Path) -> tuple[int | None, int | None]:
    """(logical, physical) cores: from cpuinfo, else the sysfs topology."""
    blocks = [b for b in _cpuinfo(proc) if "processor" in b]
    logical = len(blocks) or None
    pairs = {(b.get("physical id"), b.get("core id")) for b in blocks if "core id" in b}
    physical = len(pairs) or None
    cpu_root = sys / "devices" / "system" / "cpu"
    if physical is None and cpu_root.is_dir():
        topo = set()
        for cpu in cpu_root.glob("cpu[0-9]*"):
            core = _read(cpu / "topology" / "core_id")
            package = _read(cpu / "topology" / "physical_package_id")
            if core is not None:
                topo.add(((package or "0").strip(), core.strip()))
        physical = len(topo) or None
        if logical is None:
            logical = len(list(cpu_root.glob("cpu[0-9]*"))) or None
    return logical, physical


def _os_name(etc: Path) -> str | None:
    for line in (_read(etc / "os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def _usable(path: Path) -> bool:
    return path.exists() and os.access(path, os.R_OK | os.W_OK)


def virtualization(
    proc: Path, which: Callable[[str], str | None], run: Callable[..., object]
) -> str | None:
    """The hypervisor this runs under: `systemd-detect-virt --vm`'s answer (`none`, `kvm`, ...;
    a container is not a VM) when it is installed, else
    `unknown-vm` when cpuinfo carries the `hypervisor` flag, `none` when it does not, or None."""
    tool = which("systemd-detect-virt")
    if tool:
        try:
            done = run([tool, "--vm"], capture_output=True, text=True, timeout=5, check=False)
            answer = (getattr(done, "stdout", "") or "").strip()
            if answer:
                return answer
        except (OSError, subprocess.SubprocessError):
            pass
    blocks = _cpuinfo(proc)
    if not blocks:
        return None
    flags = blocks[0].get("flags", "").split()
    return "unknown-vm" if "hypervisor" in flags else "none"


def probe(
    *,
    proc: Path = Path("/proc"),
    sys: Path = Path("/sys"),
    dev: Path = Path("/dev"),
    etc: Path = Path("/etc"),
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., object] = subprocess.run,
) -> dict:
    """This machine's facts, as plain JSON values."""
    logical, physical = cores(proc, sys)
    mem = meminfo(proc)
    kernel = _read(proc / "sys" / "kernel" / "osrelease")
    virt = virtualization(proc, which, run)
    return {
        "schema": FACTS_SCHEMA,
        "arch": platform.machine() or None,
        "cpus_logical": logical,
        "cpus_physical": physical,
        "memory_bytes": mem.get("MemTotal"),
        "os": platform.system() or None,
        "os_name": _os_name(etc),
        "kernel": kernel.strip() if kernel else None,
        # /dev/kvm is there; and whether this process may open it (a daemon user outside the
        # `kvm` group may not, though the work it leases out can).
        "kvm": (dev / "kvm").exists(),
        "kvm_access": _usable(dev / "kvm"),
        "docker": which("docker"),
        "runsc": which("runsc"),
        "virtualization": virt,
        "vm": None if virt is None else virt != "none",
        "cgroup_v2": (sys / "fs" / "cgroup" / "cgroup.controllers").is_file(),
        "psi": (proc / "pressure" / "cpu").is_file(),
    }
