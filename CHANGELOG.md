# Changelog

All notable changes to leasehold. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as described under "Stability" in
the README: before 1.0, a minor release may break the public API, and every break is listed
here.

## [Unreleased]

### Added

- **Thread counts apart from job counts.** `hold` sets more variables, each only where the
  environment leaves it unset: `LOKY_MAX_CPU_COUNT` (joblib, scikit-learn's `n_jobs=-1`),
  `DASK_NUM_WORKERS` (Dask's threaded and multiprocessing schedulers) and `PYTHON_CPU_COUNT`
  (Python 3.13 and later: `os.cpu_count()`, `os.process_cpu_count()`, `multiprocessing`,
  `ProcessPoolExecutor`) to the job count; `LEASEHOLD_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS`, `POLARS_MAX_THREADS`, `RAYON_NUM_THREADS` and `NUMBA_NUM_THREADS`, with
  `OMP_NUM_THREADS`, to the thread count. The thread count is the job count unless a
  `[[commands.rule]]`'s new `threads` key (`"jobs"`, the default, or a count of at least 1) or
  `hold --threads N|jobs` says otherwise; a count above the job count is cut to it.
  `GET /v1/commands` serves each rule's `threads`, and `leasehold commands --test` shows it.
  Library: `leasehold.jobs.WORKER_VARIABLES`, `THREAD_VARIABLES`, `Threads` and
  `thread_count`; `job_env` takes `threads`; `leasehold.shell.Rule.threads`;
  `leasehold.commands.CommandRule.threads`; `leasehold.hold.hold`, `run_leased` and
  `child_env` take `threads`. `JOB_VARIABLES` is now both groups together.

### Changed

- The built-in pytest rule (pytest, py.test, tox, nox) has `threads = 1`: `hold` and the shims
  give its commands `OMP_NUM_THREADS=1` and the other thread-pool variables 1, while
  `PYTEST_XDIST_AUTO_NUM_WORKERS` still gets the job count. With the job count in both, each
  of N xdist workers could start an N-thread BLAS or OpenMP pool: N × N threads on a lease of
  N. A test run that is one process now gets one BLAS thread unless you set
  `OMP_NUM_THREADS` yourself, pass `hold --threads jobs`, or add a rule of your own.
- `leasehold.hold.auto_size` returns the matching `leasehold.shell.Rule` (weight 1, no memory
  and `"jobs"` threads when none matches) instead of a `(weight, memory)` pair. `hold` with
  `--weight` but no `--threads` still reads the rules, for the rule's `threads`.

### Compatibility

- A client reading rules from a daemon that does not serve `threads` gives every rule
  `"jobs"`, the thread count before the setting; so does a served `threads` that is not a
  count of at least 1. The Claude Code hook never changes a command's environment, so the
  thread count reaches only commands run by `hold` and the PATH shims.

## [0.3.0]

Memory-aware admission, a memory guard that no longer caps pools to their reservations, and an
upgrade path for configs leasehold 0.2 wrote. Without the new `[memory]` table, slot admission
and the memory guard decide as they did before memory was counted; the stricter validation
listed under "May reject" applies with or without it. The built-in rules keep their weights
(one build or test run at a time on 8 CPUs); two at once is an opt-in setting.

### Added

- **Memory admission.** A `[memory]` table (`budget`, default `"auto"`: the machine's memory
  less `kept`, default `"25%"`; `assumed_per_slot`, default `"auto"`: the budget over the
  slots) makes admission count memory as well as slots. A request counts the memory it
  declares, or its weight times `assumed_per_slot`. One that counts more than the budget, or
  declares more than `[ceiling] memory`, is refused. `[[standing]]` memory counts as held.
- `hold --memory` and a `[[commands.rule]]`'s `memory` are now declared with the request and
  counted; `acquire --memory` declares memory for a lease held across processes.
  `POST /v1/acquire` takes `memory` (a size such as `"6g"`).
- `[commands] heavy_weight` (default 4) and `light_weight` (default 2): the weights of the
  built-in build and test rules and of the JavaScript tooling rules. Lowering them (3 and 2 on
  6 slots) runs two builds or test runs at once: opt-in, shown commented out in the config
  `init --user` writes where 4 lets only one run. Work that declares no memory is then counted
  at its weight times the budget over the slots; declare memory for memory-heavy work.
- `leasehold setup --upgrade-config` (with `--dry-run`, and `--config PATH` for another file):
  brings a user config from 0.2's defaults to today's, showing the diff and keeping the old
  file as `<name>.0.2.bak`. An unedited 0.2.0 or 0.2.1 config is written afresh for the same
  machine; an edited one gains only a `[memory]` table. The built-in weights are never
  changed. It exits 0 when it wrote the file, found nothing to change or (`--dry-run`) could
  write it, and 1 when the config does not load, cannot be edited automatically, or changed
  while it worked. It is not edited automatically when it is a symbolic or hard-linked file,
  or when, with the budget the new table would have on this machine, work could not start as
  before: `[[standing]]` memory fills the budget, a `[[commands.rule]]` declares memory that
  never fits beside the standing memory or is more than `[ceiling] memory`, or the built-in
  heavy rules would be cut to a lighter weight; it says what to change. The backup is a
  byte-for-byte copy and CRLF line endings are kept. The file is checked again just before
  the new one is renamed over it; a write to it landing in that instant is lost (neither the
  new file nor the backup has it). `--dry-run` without `--upgrade-config` is refused (exit 1).
- `leasehold doctor`: check `defaults` says when a user's own config (`mode = "user"`, or no
  `mode`, which the daemon reads as user) lacks a `[memory]` table, as a warning for a config
  exactly as 0.2.0 or 0.2.1 wrote it and as information (ok) for one edited since; check
  `hard_cpu` warns, for a user's own daemon with `[contain] hard_cpu` off, that the CPUs kept
  for you are kept only from tools that respect the job count; check `guard` warns when no
  built-in build or test rule could start while the memory guard is on; check `memory` fails
  when the `[memory]` budget worked out for the machine is 0 or filled by `[[standing]]`
  memory, and warns when standing memory cuts the built-in rules' weight or `assumed_per_slot`
  is 0; check `rules` fails for each `[commands]` rule, config or built-in, that no lease could
  ever be granted for on this machine, saying that a command matching it is refused.
  `leasehold setup` says what the config it leaves in place lacks.
- `GET /v1/commands`: a rule no lease could ever be granted for carries `never`, the reason:
  its pool is not there; with `[memory]`, its declared memory is more than one lease may be
  given (`[ceiling] memory`, else the machine's); or no weight of it fits the slots,
  reservations, standing work and the budget beside standing memory. It is served at its own
  weight and still matches its commands; the Claude Code hook denies such a command with the
  reason and a shim exits 77 without running it, rather than taking a lighter rule's lease.
  `leasehold commands` shows the reason.
- Status (schema 2, additive): a `memory` object (`counted`, `machine_bytes`, `budget_bytes`,
  `kept_bytes`, `assumed_per_slot_bytes`, `held_bytes`, `free_now_bytes`); per pool
  `memory_counted_bytes`; per lease and queued item `memory_bytes` (declared) and
  `memory_counted_bytes`; `guard.pressing`. `leasehold status` prints the budget, and
  `leasehold top` shows it in the header and what each waiting item counts.
- Blockers say which limit holds an item back: "the memory budget is used: ...", "memory is
  short (...): nothing new starts until the pressure eases", "memory is short (...): ... may
  start now; this counts ...", beside the slot reasons.
- Library: `AdmissionPolicy` gains `memory`, `assumed_memory`, `standing_memory`, `memory_free`,
  `memory_short`, `memory_pressing` and `memory_overdue` (all empty or None by default) and
  `counted()`; `from_pools` takes them as keywords (`memory_pressing` a bool,
  `memory_overdue` a frozenset of item keys); `Held`, `Waiting` and `LedgerItem` gain `memory` (bytes,
  default None; `LedgerItem` raises ValueError outside 0 to `MEMORY_MAX`); `fits` and
  `AdmissionPolicy.refusal` take an optional `memory`. `AdmissionPolicy.heaviest(pool,
  memory=None)`: the most weight one item declaring `memory` bytes could ever be admitted with
  in a pool, with nothing held but the standing work (0 when never). `leasehold.admission.gib`.
  `leasehold.machine.MemorySettings`, `MemoryBudget`, `MachineConfig.memory`,
  `MachineConfig.memory_budget()`, `MachineConfig.standing_memory()`,
  `MachineConfig.memory_problems()`, `MachineConfig.for_user`, `MachineConfig.rule_pool()`,
  `MachineConfig.never_reason()`, `MachineConfig.never_rules()`, and `MachineConfig.policy`
  keywords `memory_bytes`, `memory_free`, `memory_short`, `memory_pressing`,
  `memory_overdue`. `leasehold.shell.Rule.never`. `leasehold.hold.wait_for_grant` takes
  `fault`, the exit code for an error answer of 500 or more. `leasehold.commands.default_rules`, `HEAVY_WEIGHT`, `LIGHT_WEIGHT`.
  `leasehold.guard.Guard.pressing` and `Reading.at`. `leasehold.agent.MachineAgent` takes
  `monotonic`. `leasehold.upgrade`. `leasehold.units.MEMORY_MAX` and `declared_to_bytes`.
  `leasehold.doctor.check_upgrade`, `check_memory`, `check_rules`, `check_hard_cpu` and
  `check_guard_weights`. `leasehold.status.counted`, `with_counted` and `memory`.
  `leasehold.template.weights` (the opt-in weights that run two at once), `USER_0_2` and
  `render_user_0_2`.
- The benchmark (`bench/run.py`): conditions `lh-enforced-declared` (memory jobs declare their
  size) and `lh-enforced-v020` (0.2.0's generated config), `--memory-budget`, `--rounds`
  (every condition again on the same plan, alternate rounds reversed), and `--weights`
  defaulting to the generated config's `heavy_weight`.

### Changed

- The config `init --user` and `setup` write gains a `[memory]` table and today's comment above
  `[guard]`; the built-in rules keep their weights (4 and 2).
- With `[memory]`, the memory guard no longer holds pools to their reservations: while it is on,
  new work may count at most the memory measured free (none while pressure is over the
  threshold or memory under the floor; while it settles, what is available above the floor less
  what was granted in the last `settle_s`). A request heavier than its pool's reservation is no
  longer held back until the guard goes off. Without `[memory]`, the guard is unchanged.
- The ledger keeps each item's declared memory; a 0.2 ledger gains the column in place, so 0.2
  and 0.3 processes can share one file.
- With `[memory]`, while a guard reading is over a threshold nothing new starts at all, whatever
  it counts or declares (`AdmissionPolicy.memory_pressing`); while the guard settles, an item
  whose turn it is that fails only the memory measured free no longer holds up its pool:
  lighter items behind it that fit may start, its own session's later items still wait, but
  only until it has waited `settle_s`: after that it holds up its pool like any other item
  whose turn it is, so a stream of small items in its own pool cannot keep it waiting (other
  pools' items may still start around it, as with slots alone). How long it has waited
  is measured on the monotonic clock from when the daemon first saw it in line (its
  wall-clock `queued_at` taken back from that moment), so a step of the wall clock changes
  nothing.
- The guard's dwell and settling, and the settle window, are measured on a monotonic clock, not
  the wall clock. A daemon started again counts a lease it finds as granted at its wall-clock
  `acquired_at`, so one granted within `settle_s` of the restart is still recent. Observed
  leases are not counted in the settle window: `MemAvailable` already leaves out their memory.
- The Claude Code hook and the PATH shims follow one rule, for listing the rules and for
  asking for the lease alike. When the daemon says no (a `refused` answer, such as a request
  that could never be granted or a per-user limit; an error answer under 500, such as 403
  `forbidden`; a matching rule with `never`), the hook denies the command with the daemon's
  text, as with `when_busy = "deny"`, and a shim exits 77 without running the program. When
  the daemon cannot answer (none is reached, or an error answer of 500 or more, such as 503
  `ledger_unavailable` on a full disk), the command runs without a lease, and the hook or shim
  says why with the daemon's text. This changes what the refusal of requests that could never
  fit (under Fixed) did to them on `main`: run unleased. In 0.2.1 such a request waited in
  line for `wait_s` and was then handled as busy. `hold` and `acquire` still exit 77 on an
  error answer of 500 or more, and never run the command unleased on it.
- `GET /v1/commands` no longer serves a rule at weight 1 when no weight of it could ever be
  granted; it serves it at its own weight with `never`.
- **May reject a config or request that was accepted.** A memory size over 1 EiB
  (`leasehold.units.MEMORY_MAX`) is an error everywhere sizes are read: a config fails to load,
  and `POST /v1/acquire` answers 400 `bad_request` (it used to fail with 500 `internal` past
  2^63 bytes, with or without `[memory]`). A `[memory] budget` of 0, or one that `[[standing]]`
  memory fills, fails to load; so does a fixed budget beside which `[[standing]]` memory leaves
  no room for a `[[commands.rule]]`'s declared memory. With `[memory]`, a `[[commands.rule]]`
  declaring more than `[ceiling] memory` fails to load; with or without it, so does one naming
  no pool when its kind's pool (`work`) is not under `pools`. An `auto` budget that comes to 0 (`kept`
  at or over the machine's memory) stops the daemon starting. A declared size of 0 is refused:
  `hold --memory 0g` exits 2, a rule with `memory = "0g"` fails to load, and `POST
  /v1/acquire` with `"memory": "0g"` answers 400 (`assumed_per_slot = "0g"` is still allowed).
- **May refuse a command that ran.** With `[memory]` (which `init` and `setup` now write),
  `hold --memory` and a `[[commands.rule]]`'s `memory` are counted against the budget: a size
  over the budget (beside the standing memory) or over `[ceiling] memory` is refused, where
  before it was only a `MemoryHigh` cap on a contained command.

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

- A `[[commands.rule]]` naming no pool no longer makes the config fail to load ("only observed
  programs go in None") when there is no observed pool.

- A request that could not fit its pool even on an idle machine because other pools reserve
  the slots (8 slots, pool `a` reserving 4, weight 6 in pool `b`) is refused with a reason
  instead of queuing forever and, with no backfill, holding up every later item in its pool.
  `AdmissionPolicy.refusal` (and so `Ledger.try_admit`) applies the idle-machine test always,
  not only when there is standing work, and ignores a pool's memory-pressure cap, which is
  never permanent.
- `Ledger.try_admit` refuses a weight below 1 (`AdmissionPolicy.refusal`), and
  `Ledger.enqueue` raises `ValueError` for one: a negative weight was admitted and let its
  pool over-commit.
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
- Ctrl-C (or SIGTERM, SIGHUP) while `hold` waits in line, or while its command is being started,
  no longer prints a traceback, leaves the place in line on the daemon, or leaves the command
  running without a lease: `hold` gives back the place or the lease, stops anything it started,
  and exits 128 + the signal (SIGINT: it ends by SIGINT, which a shell reports as 130). Any
  other command interrupted by Ctrl-C (`acquire` waiting in line, `serve`) ends by SIGINT too,
  without a traceback, instead of exiting 1.
- A place in line given back (`release`) while a blocking acquire for it is still being served
  stays given back: the acquire ends `refused` instead of queueing it again.
- Ctrl-C on a terminal stops a shell loop running `hold`: when the command dies of SIGINT or
  SIGQUIT, `hold`, once it has released the lease, ends by the same signal instead of exiting
  130 or 131 normally, which the shell took as the interrupt handled. Without a terminal, a
  command killed by a signal still makes `hold` exit 128 + the signal.
- One renewal the daemon does not answer no longer costs a `lend` lease (and its command):
  each renewal waits at most TTL/3 for an answer, and renewals are due every TTL/3 counted from
  the start of the last one, not from its end. While the group is stopped, the lease is renewed
  every TTL/3 instead of every 0.1 s.
- A `work` or `external` lease the daemon lost and then refuses when asked for again is said
  once, and the command runs on without a lease and without asking again; asked for again and
  put in line, it says its place once.

### Compatibility

- Nothing is removed or renamed. Without a memory budget, slot admission is unchanged:
  `decide`, `positions`, `blocker`, `fits` and `refusal` answer exactly as they did before
  memory was counted (`test_without_a_budget_slot_admission_is_as_before_memory` compares them
  against a copy of that module on random states), and a config without `[memory]` keeps the
  guard that holds pools to their reservations. The validation added under "May reject"
  applies with or without `[memory]`. `AdmissionPolicy` with several hosts in `slots`, and `PoolPolicy.host`, are
  unchanged; a memory budget is per host.
- A 0.2 config keeps 0.2's defaults until it is upgraded (`setup --upgrade-config`); nothing
  rewrites it on its own.
- A 0.3 client asking a 0.2 daemon for a lease with `memory` is refused for the unknown field;
  `leasehold.client.Client.acquire` then asks again without it, so `hold --memory` and the
  shims keep working while an old daemon runs.

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
