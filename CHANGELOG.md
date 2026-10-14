# Changelog

All notable changes to leasehold. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as described under "Stability" in
the README: before 1.0, a minor release may break the public API, and every break is listed
here.

## [Unreleased]

### Fixed

- An `acquire` naming a pool that does not exist is `refused` (`there is no pool 'NAME'`;
  `hold` and `acquire` exit 77). It used to drop the connection, which `hold` took for no
  daemon and ran its command without a lease.
- The API answers any fault in the daemon with 500 and `error: "internal"` (the traceback on the
  daemon's stderr) instead of dropping the connection. `hold` exits 77 on it, as on every error
  answer, and never runs the command unleased.
- A SQLite error after the ledger is opened (locked past the busy timeout, a full disk, a
  read-only file system) is `LedgerUnavailable` with the new `problem` `"failing"`, answered 503
  `ledger_unavailable`, instead of a raw `sqlite3.Error` that dropped the connection.
- A negative or non-numeric `Content-Length` is 400 `bad_request`; a negative one used to read
  the socket to its end, with no bound. A body shorter than its `Content-Length` is 400 too.
  413 `too_large` (a body over 64 KiB) now carries a `detail` and is documented.
- The daemon gives up on a client that stops sending part way through a request after 30
  seconds, closing the connection, so a stalled client no longer keeps a thread. A long-polling
  `acquire` (`wait_s`) is not affected.

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
