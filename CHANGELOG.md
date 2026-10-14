# Changelog

All notable changes to leasehold. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as described under "Stability" in
the README: before 1.0, a minor release may break the public API, and every break is listed
here.

## [Unreleased]

### Added

- `AdmissionPolicy.heaviest(pool)`: the most weight one item could ever be admitted with in a
  pool, with nothing held but the standing work.

### Changed

- **May reject a config that 0.2.1 accepted.** `[[standing]]` weight must leave every pool's
  reservation whole: each pool's standing weight or its `reserved`, whichever is more, must
  together fit `[ceiling] slots`. A config with standing work in some pools that eats into
  another pool's reservation now fails to load; lower the standing weight or the reservation.
- A pool's effective `max` (in `status`, `GET /v1/commands` and admission) is clamped to the
  slots less the other pools' reservations, which it could never pass; the observed pool is
  still clamped only to the slots. A per-pool `[[standing]]` total is checked against this
  clamped `max`.
- `leasehold pool set NAME --max N` (`POST /v1/pools/<name>`) refuses a max the pool could never
  reach beside the other pools' reservations and standing work.
- `GET /v1/commands` cuts each rule's weight to the most its pool could ever grant one lease,
  standing work and other pools' reservations included, not only to its `max` and the slots.

### Fixed

- A request that could not fit its pool even on an idle machine because other pools reserve
  the slots (8 slots, pool `a` reserving 4, weight 6 in pool `b`) is refused with a reason
  instead of queuing forever and, with no backfill, holding up every later item in its pool.
  `AdmissionPolicy.refusal` (and so `Ledger.try_admit`) applies the idle-machine test always,
  not only when there is standing work, and ignores a pool's memory-pressure cap, which is
  never permanent.
- `Ledger.try_admit` refuses a weight below 1 (`AdmissionPolicy.refusal`), and
  `Ledger.enqueue` raises `ValueError` for one: a negative weight was admitted and let its
  pool over-commit.

## [0.2.1]

An agent's heavy command waits its turn instead of being refused after 20 s. At 8 agents most
heavy commands waited longer than that (docs/bench.md), so most were refused and retried.

### Changed

- `[commands] wait_s` defaults to 300 (was 20), and `leasehold init --user` writes 300. A config
  written by 0.2.0's `init` says `wait_s = 20`: set it to 300 by hand; `leasehold doctor` warns
  (check `commands`) while it is under 120.
- A refusal says the command did not run (`busy.ADVICE`: "The command did not run. Do lighter
  work first, then run it again.").
- A wait (`hold`, `acquire`, the shims) says its place and why again at least every 20 s, with
  the time waited so far (`hold.PROGRESS_S`).
- The Claude Code hook tells the user when a command waited at least 5 s for its lease
  (`claude_code.WAIT_NOTED_S`).

## [0.2.0] - first public release

leasehold gives the heavy commands on one Linux machine (coding agents' builds and test runs, CI
jobs, people's work) a lease on a share of the machine before they start, sizes them to it, and,
where systemd allows it, holds them to it while they run.

### Added

**For coding agents**

- `leasehold setup` (and `--remove`): a config sized for the machine, a systemd user unit
  running your own daemon, the PATH shims, the agents found on PATH, and a containment and
  `doctor` check, in one step.
- `leasehold run -- AGENT`: the agent as one session (`LEASEHOLD_SESSION`), the PATH shims first
  on its PATH and, when contained, a slice of its own whose leftovers are stopped when it exits
  (`--leave-running`, `--session`, `--quiet`, `--no-contain`).
- PATH shims (`leasehold shims install|list|remove`) for the programs the `[commands]` rules
  name: a matching command runs under a lease, anything else passes straight through, and a
  command an agent hook has leased already runs in that lease.
- The Claude Code plugin: a hook that takes a lease around a heavy Bash command, with a shell
  pre-filter so ordinary commands start no Python, and a skill that tells the agent what a wait,
  a refusal and the exit codes mean.
- `[commands]` rules (built in, extendable): which programs and arguments are heavy, their
  weight and memory. A busy machine refuses after 20 s (`wait_s`, `when_busy = "deny"`), and the
  refusal names what holds the pool and the place in line. `leasehold commands --test` and
  `--programs`.
- `leasehold mcp` (experimental): MCP tools to read the machine's state and wait for a lease.

**Admission**

- Weighted leases on slots, renewed by their holder and expiring when it dies. Pools with
  reserved and maximum slots and a priority; lease kinds `work`, `external`, `lend` and
  `observed`. Turns go round by Unix user, then by session within a user; no backfill.
- Lending (experimental): when nothing waits and pressure stall information is quiet, `lend`
  leases use spare slots, and are revoked the moment other work queues or pressure rises.
- The memory guard (`[guard]`): while memory pressure or available memory is past its
  threshold, every pool admits only its reserved slots. On for a user's daemon.
- `[[observe]]` rules (experimental): a known program started outside leasehold counts as a
  lease while it runs.

**Running a command**

- `leasehold hold -- CMD`: wait for a lease, run the command in its own process group (the
  terminal's foreground when run from one), renew the lease, and release it when the command
  ends. With no `--weight`, the command is weighed by the `[commands]` rules. The command learns
  its size from `LEASEHOLD_JOBS` and the job-count variables cargo, make, CMake, pytest-xdist,
  Go and others read; `--jobserver` adds a GNU make jobserver.
- Containment: where the caller runs in the user's own session and the systemd user manager is
  delegated cpu and memory, `hold` and the shims start the command in a user scope with
  `CPUWeight` 100 per slot and, with `--memory` or a rule's `memory`, `MemoryHigh`. `[contain]
  mode` (`auto`, `off`), `hard_cpu`; `LEASEHOLD_CONTAIN=off`. Elsewhere leases are advisory.
- `hold --if-unavailable run|fail`: with no daemon, run the command without a lease (the
  default) or exit 69.
- Exit codes: 69 no daemon, 75 busy (gave up waiting), 76 revoked, 77 refused, 127 not started.

**The daemon and views**

- `leasehold serve`: your own daemon (`--user`, no root) or the machine's (`--system`, a
  `leasehold` group on its socket). One unix socket, a SQLite ledger, an HTTP API with a
  versioned status object (schema 2).
- `leasehold init` writes a commented config sized for the machine: two pools for a user's
  daemon, four for the machine's.
- `leasehold top` (terminal), `leasehold ui` (browser, experimental), `status`, `leases`,
  `metrics` (Prometheus, experimental), `report` (a webhook, experimental) and `demo`, which
  runs a made-up busy machine.
- `leasehold doctor`: the config, socket and its path length and permissions, the daemon, PSI,
  cgroups, the ledger and containment, each with a fix.
- `bench/`: a seeded benchmark of several agents starting heavy jobs on one machine, run
  unmanaged, under `nice`, in a low-weight systemd slice, with a fixed job count, and through
  `leasehold hold` advisory and enforced (`bench/run.py`); method and results in `docs/bench.md`.
- The admission policy as a library (`leasehold.model`, `admission`, `ledger`, `quota`,
  `client`), with no daemon.
