"""The facts probe and the load reader (leasehold.facts, .load) over fixture trees: what they
find, and that anything missing is reported as unknown rather than failing."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from leasehold.facts import probe
from leasehold.load import LoadReader, parse_pressure

CPUINFO = "".join(
    f"processor\t: {n}\nphysical id\t: 0\ncore id\t\t: {n // 2}\nflags\t\t: fpu {flag}\n\n"
    for n, flag in enumerate(["hypervisor"] * 4)
)
MEMINFO = "MemTotal:       65536000 kB\nMemFree:        1000 kB\nMemAvailable:   32768000 kB\n"
PSI = (
    "some avg10=1.50 avg60=2.25 avg300=3.00 total=100\n"
    "full avg10=0.00 avg60=0.10 avg300=0.00 total=5\n"
)


def write(root: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def tree(tmp_path: Path) -> dict[str, Path]:
    proc = write(
        tmp_path / "proc",
        {
            "cpuinfo": CPUINFO,
            "meminfo": MEMINFO,
            "loadavg": "0.50 0.40 0.30 1/200 999\n",
            "sys/kernel/osrelease": "6.1.0-test\n",
            "pressure/cpu": PSI,
            "pressure/memory": PSI,
            "pressure/io": PSI,
        },
    )
    sys_root = write(tmp_path / "sys", {"fs/cgroup/cgroup.controllers": "cpu memory io\n"})
    dev = tmp_path / "dev"
    dev.mkdir()
    etc = write(tmp_path / "etc", {"os-release": 'NAME=x\nPRETTY_NAME="Test OS 1"\n'})
    return {"proc": proc, "sys": sys_root, "dev": dev, "etc": etc}


def no_tools(_name: str) -> None:
    return None


def test_facts_come_from_the_trees(tmp_path):
    roots = tree(tmp_path)
    (roots["dev"] / "kvm").write_text("")
    os.chmod(roots["dev"] / "kvm", 0o600)
    tools = {"docker": "/usr/bin/docker"}
    facts = probe(**roots, which=tools.get)
    assert facts["cpus_logical"] == 4 and facts["cpus_physical"] == 2
    assert facts["memory_bytes"] == 65536000 * 1024
    assert facts["kernel"] == "6.1.0-test" and facts["os_name"] == "Test OS 1"
    assert facts["kvm"] is True and facts["kvm_access"] is True
    assert facts["docker"] == "/usr/bin/docker" and facts["runsc"] is None
    assert facts["virtualization"] == "unknown-vm" and facts["vm"] is True  # cpuinfo's flag
    assert facts["cgroup_v2"] is True and facts["psi"] is True
    assert facts["schema"] == 1


def test_systemd_detect_virt_is_asked_when_installed(tmp_path):
    roots = tree(tmp_path)
    asked = []

    def run(argv, **_kw):
        asked.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="none\n")

    facts = probe(**roots, which={"systemd-detect-virt": "/bin/sdv"}.get, run=run)
    assert asked == [["/bin/sdv", "--vm"]]
    assert (facts["virtualization"], facts["vm"]) == ("none", False)


def test_an_empty_machine_is_all_unknown_never_an_error(tmp_path):
    empty = {k: tmp_path / k for k in ("proc", "sys", "dev", "etc")}
    facts = probe(**empty, which=no_tools)
    assert facts["memory_bytes"] is None and facts["kernel"] is None
    assert facts["kvm"] is False and facts["kvm_access"] is False
    assert facts["virtualization"] is None and facts["vm"] is None
    assert facts["cgroup_v2"] is False and facts["psi"] is False
    reading = LoadReader(["work.slice"], proc=empty["proc"], cgroup_root=empty["sys"]).read()
    assert reading["pressure"] == {"cpu": None, "memory": None, "io": None}
    assert reading["loadavg"] is None and reading["mem_available_bytes"] is None
    assert reading["cgroup_v2"] is False and reading["slices"] == {"work.slice": None}


def test_the_sysfs_topology_counts_cores_when_cpuinfo_has_no_core_ids(tmp_path):
    proc = write(tmp_path / "proc", {"cpuinfo": "processor\t: 0\n\nprocessor\t: 1\n"})
    sys_root = tmp_path / "sys"
    for n in range(2):
        write(sys_root, {f"devices/system/cpu/cpu{n}/topology/core_id": "0\n"})
    facts = probe(proc=proc, sys=sys_root, dev=tmp_path, etc=tmp_path, which=no_tools)
    assert (facts["cpus_logical"], facts["cpus_physical"]) == (2, 1)


def test_pressure_files_parse_and_garbage_is_unknown():
    assert parse_pressure(PSI) == {
        "some": {"avg10": 1.5, "avg60": 2.25},
        "full": {"avg10": 0.0, "avg60": 0.1},
    }
    assert parse_pressure("") is None and parse_pressure("nonsense\n") is None


def test_the_load_reader_measures_psi_memory_and_slice_cpu_rates(tmp_path):
    roots = tree(tmp_path)
    cgroup = roots["sys"] / "fs" / "cgroup"
    write(
        cgroup,
        {
            "work.slice/cpu.stat": "usage_usec 1000000\nuser_usec 1\n",
            "work.slice/memory.current": "4096\n",
            "work.slice/cpu.pressure": PSI,
        },
    )
    now = [100.0]
    reader = LoadReader(
        ["work.slice", "lend.slice"], proc=roots["proc"], cgroup_root=cgroup, clock=lambda: now[0]
    )
    first = reader.read()
    assert first["pressure"]["cpu"]["some"]["avg10"] == 1.5
    assert first["loadavg"] == [0.5, 0.4, 0.3]
    assert first["mem_available_bytes"] == 32768000 * 1024
    work = first["slices"]["work.slice"]
    assert work["cpus_used"] is None  # no rate from one reading
    assert work["memory_current_bytes"] == 4096
    assert work["pressure"]["cpu"]["some"]["avg10"] == 1.5 and work["pressure"]["memory"] is None
    assert first["slices"]["lend.slice"] is None  # not created yet
    (cgroup / "work.slice" / "cpu.stat").write_text("usage_usec 4000000\n")
    now[0] += 2.0
    assert reader.read()["slices"]["work.slice"]["cpus_used"] == 1.5  # 3 cpu-seconds in 2 s
