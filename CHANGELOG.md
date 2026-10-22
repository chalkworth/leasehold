# Changelog

All notable changes to leasehold. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/) as described under "Stability" in
the README: before 1.0, a minor release may break the public API, and every break is listed
here.

## [Unreleased]

### Added

- A contained command's resource use is now recorded. While it runs, its client reads the
  scope's `memory.peak`, the most `anon` in `memory.stat`, `oom_kill` and `high` from
  `memory.events`, and `usage_usec` from `cpu.stat` about once a second. At release it sends
  these with the wall time, the exit status, the `[commands]` rule that sized the command and
  the git repository it ran in. The daemon keeps each outcome in a new ledger table, with the
  lease's size, range and declared memory: the newest 50 for each user, rule and repository,
  none older than 30 days. Nothing about admission uses them yet.
- `POST /v1/release` takes an optional `outcome`, and `leasehold.client.Client.release` an
  `outcome` argument. A 0.4 daemon refuses the field, so when a release with an outcome is
  answered 400, or with an error of 500 or more, the client sends it again without the outcome.
- `GET /v1/outcomes`, `Client.outcomes()` and `leasehold outcomes [--json]` list the outcomes
  kept, summed up for each rule and repository: runs, the 95th percentile and the most of
  `memory.peak`, the most anonymous memory, runs reached by the OOM killer or `MemoryHigh`, and
  mean parallelism (CPU time over wall time). A user sees their own outcomes; an administrator
  sees everyone's.
- A `maintenance` lease kind takes the whole machine for a while, for a benchmark window or an
  upgrade. While it waits, the machine drains. Requests that arrive after it wait behind it in
  every pool, the owner's own `work` included, and the leases already running finish. Once no
  lease is held, it is granted every slot that standing work does not hold. It is never
  revoked and revokes nothing but `lend` leases. With no `[kinds]` entry it goes to the `work`
  kind's pool. `[kinds]` accepts `maintenance`. On a machine whose `[[standing]]` work holds
  every slot, it is refused.
- `hold` and `acquire` take `--until HH:MM|ISO` and `--reason TEXT` with `--kind maintenance`.
  They become the lease's `until` (RFC 3339, UTC) and `reason` labels. The end is advisory:
  leasehold shows it and never ends the lease by it. Either option with another kind exits 1.
- A request held up by a maintenance lease gives `blocked` as `machine draining for maintenance
  until 07:00 UTC (benchmark window)`, or `machine in maintenance until ...` once it is held.
  A queued `/v1/acquire` answer then carries `maintenance`.
- The status object has a `maintenance` field: null, or the lease's `lease_id`, `state`
  (`held` or `draining`), `user`, `name`, `reason`, `until` and `message`. `leasehold status`
  prints the message.
- When `hold` or `acquire` gives up (exit 75) while a maintenance lease holds or drains the
  machine, it prints `held by NAME (maintenance) until TIME`, or `draining for ...` while the
  lease still waits, a line scripts can read.
- `hold --timeout` and `acquire --timeout` also take minutes and hours (`30m`, `2h`). A
  negative timeout is now refused.
- `leasehold.kinds.WHOLE`, `leasehold.maintenance`, `MachineConfig.kind_pool`, and in
  `leasehold.admission` the `whole` field of `Held` and `Waiting`, `AdmissionPolicy.maintenance`,
  `refusal(..., whole=)` and `draining()`.

## [0.4.0]

### Added

- A Python API for taking leases without running `leasehold hold`. `leasehold.lease()` waits in
  line and returns a `Lease`, which a thread of its own renews until it is released. Leaving
  its `with` block releases it. The lease reports `weight`, `min_weight`, `max_weight` and
  `jobs`. `lost` says whether it was revoked or lost for good, `on_lost` is called when that
  happens, `check()` raises `LeaseLost` after it, and `env()` gives the variables `hold` gives
  its command. A wait ends in `Busy` (timed out), `Cancelled` (a `cancel` event was set),
  `Refused` or `DaemonError`. A refusal holds no place in line; each of the others gives its
  place back. With no daemon, `if_unavailable="run"` returns an unleased `Lease` and `"fail"`
  raises `Unavailable`. All of these errors are subclasses of `LeaseError`. `find_daemon()` finds a daemon's socket as
  `Refused` or `DaemonError`, and each of these gives the place in line back. With no daemon,
  `if_unavailable="run"` returns an unleased `Lease` and `"fail"` raises `Unavailable`. All
  of these errors are subclasses of `LeaseError`. `find_daemon()` finds a daemon's socket as
  the command line does. The API is synchronous and installs no signal handlers. Its supported
  names are those in `leasehold.__all__`, which the README's "Stability" section covers. The
  code is in `leasehold.leasing`.
- `leasehold.hold.lease_request` and `child_env` now live in `leasehold.leasing`, and
  `leasehold.hold` re-exports them. `hold` waits in line through `leasing.wait`, with the same
  messages and exit codes as before.
- `PROGRESS_S` moved from `leasehold.hold` to `leasehold.leasing`, and is no longer on
  `leasehold.hold`.
- `Lease.popen(argv, ...)` runs a command under a lease and returns a `Command`, and
  `Lease.run(argv, ...)` waits for it and returns its exit status. The command leads a process
  group of its own and gets the lease's environment. Where the daemon allows, it runs in the
  lease's systemd scope (`contain=False` turns that off). With `jobserver=True` it gets a make
  jobserver, and a range then follows its target as `hold --jobserver` does. A revoked `lend`
  lease stops the group, SIGTERM then SIGKILL after `grace` seconds, and `Command.stopped_for`
  says why. Whatever the command leaves in its group is stopped when it ends. A command that
  cannot start raises `NotStarted`. The code is in `leasehold.supervise`.
- `hold` runs its command through `supervise.Command`, so `hold` and the Python API share one
  code path. Its messages and exit codes are unchanged. `leasehold.hold.run_leased` no longer
  takes `socket`: the command gets the lease's daemon as `LEASEHOLD_SOCKET`, and a PATH shim's
  command now gets it too. `hold.spawn`, `scope_prefix` and `jobserver_env` moved to
  `leasehold.supervise` (`spawn` is folded into `Command`).
- A lease released while its renewal thread is asking again for a lease the daemon forgot no
  longer gets that lease back. If the daemon grants it after the release, it is given back at
  once.
- `hold`, and so the PATH shims, sets two groups of variables. The job count goes to
  `LEASEHOLD_JOBS`, `CARGO_BUILD_JOBS`, `CMAKE_BUILD_PARALLEL_LEVEL` and
  `PYTEST_XDIST_AUTO_NUM_WORKERS`. The thread count goes to the variables that limit the
  parallelism inside each process. `hold` already set `OMP_NUM_THREADS`, `RUST_TEST_THREADS`
  and `GOMAXPROCS`. New ones are `LEASEHOLD_THREADS`, `OPENBLAS_NUM_THREADS`,
  `MKL_NUM_THREADS`, `POLARS_MAX_THREADS`, `RAYON_NUM_THREADS`, `NUMBA_NUM_THREADS`,
  `LOKY_MAX_CPU_COUNT`, `DASK_NUM_WORKERS` and `PYTHON_CPU_COUNT`.
- The thread count is the job count unless the new `threads` key of a `[[commands.rule]]`, or
  `hold --threads N|jobs`, gives fewer. `threads` is `"jobs"`, the default, or a count of at
  least 1, and a count above the job count is cut to it.
- A pytest run that starts xdist workers shares the job count among them, whichever rule it
  matched. A run with `-n N` gives each worker the job count divided by N, and one with
  `-n auto` or `logical` gives each one thread, or the job count divided by M with
  `--maxprocesses M`. The rule's own `threads` can only lower that. A built-in rule matches
  these runs. Whether a run starts workers is read from its arguments, `PYTEST_ADDOPTS` and
  its config file's `addopts`, as pytest and pytest-xdist 3 read them (`leasehold.xdist`).
  `@FILE` arguments past 10,000 words or 8 files deep count as `-n auto`.
- `LEASEHOLD_SET` lists each variable `hold` set, with its value. A `hold` inside a lease sets
  the variables that still have their recorded values to its own lease's counts.
- A PATH shim run inside a lease takes no second lease. When its own rule gives fewer threads,
  it lowers the lease's thread variables to that count, waiting at most 0.3 s for the
  daemon's rules.
- `GET /v1/commands` serves each rule's `threads`. `leasehold commands` shows it.
  `leasehold commands --test LINE` shows the thread count the whole line gets, and
  `--test --json` has it in a `threads` field.
- A `threads` value can also be a share of the job count: `"jobs/N"`, the job count divided
  by N, or `"jobs/N:K"`, that and at most K, never less than 1. `leasehold.jobs.Threads` and
  `thread_count` take these, `leasehold.shell.threads` and `leasehold.hold.auto_size(...).threads`
  return them, and a client reads one in a served rule. The config's `threads` and
  `hold --threads` still take only `"jobs"` or a count. A client reads a served `threads`
  it does not know as 1.
- `hold` sets `LEASEHOLD_SOCKET` for its command to the socket of the daemon it found, made
  absolute, so a nested `hold` or a PATH shim reaches the same daemon.
  `leasehold.hold.run_leased` and `child_env` take `socket`.
- An `@FILE` argument larger than 1 MiB makes a pytest run count as `-n auto`.
- Library: `leasehold.jobs.WORKER_VARIABLES`, `THREAD_VARIABLES`, `BLAS_FAMILY`,
  `LEASEHOLD_SET`, `MAX_DIGITS`, `Threads`, `thread_count`, `count_of`, `recorded`,
  `leasehold_set`, `sized_env`, `unsized_env` and `resized`. The module `leasehold.xdist`.
  `leasehold.commands.XDIST_ARGS` and `CommandRule.threads`. `leasehold.shell.Rule.threads`,
  `leasehold.shell.threads` and `leasehold.shell.Found`. `leasehold.shims.within_lease` and
  `WITHIN_LEASE_TIMEOUT_S`.
- A request may ask for a range of slots, MIN-MAX. It waits in line for MIN, exactly as a
  request of MIN would. When admitted, it is granted a size from MIN to MAX that it keeps for
  its life (docs/reference.md, "Ranges: MIN-MAX slots"). A range is asked for by:
  - a `[[commands.rule]]` `weight = "MIN-MAX"` (a count, `weight = 4`, is as before);
  - `hold --weight MIN-MAX` and `acquire --weight MIN-MAX`;
  - `POST /v1/acquire`'s new `max_weight` (at least `weight`, which stays the least);
  - `LEASEHOLD_WANT=MIN-MAX` (or `=N`) in the environment of a command a PATH shim leases, which
    replaces the matching rule's weight;
  - the MCP server's `acquire_lease`, whose new optional `max_weight` makes a range.
- Extra slots past MIN are dealt one at a time to the requests admitted together, each up to
  its share of what the pool had free. A request that reaches its MAX leaves the rest of its
  share to the others: a 1-2 and a 1-8 on an idle pool of 8 with no headroom get 2 and 6.
  Every extra slot leaves room, in slots and in memory, for each request whose turn came in the
  same decision but which did not fit, and never takes another pool's reservation. No extra
  slot is dealt while the memory guard holds an allowance or holds the pools to their
  reservations, or while memory is under pressure: each request then gets its MIN.
- A waiting range keeps the MAX it asked for. Admission cuts it, when it admits the request, to
  the most one lease could be granted then (`max`, overrides, standing work and the memory
  ceiling), so a pool's `max` raised while it waits counts. Its queued answer, its `blocked`
  text, the status and leases queue, `top` and events all give its MAX cut the same way.
- A pool's new `headroom` (`[pools.<name>] headroom`, a count or `"auto"`) is how many slots
  extra slots leave free in the pool for requests that arrive later. `"auto"`, the
  default, is `max // 4`: 2 for a pool of 8, 1 for 4 to 7, 0 below 4, and 0 for the observed
  pool. On the machine, extra slots leave free the largest headroom among the pools an extra
  slot could land in. Those are the pools that are open, not capped and not observed, and
  whose headroom is below the most they could hold. Extra slots also leave free the memory
  that headroom would count, at `assumed_per_slot` or the range's own per-slot size if that
  is larger. They never take another pool's unused reservation either. A lone 1-8 on an idle
  pool of 8 gets 6. Headroom never holds back a MIN or a fixed weight, and helps only
  requests whose MIN fits in it. A waiting range holds its place at the head of its pool's
  queue by its MIN, as before, so a large MIN waits and the requests behind it wait too.
  `GET /v1/status` gives each pool's `headroom` as a count, `leasehold status` shows it
  beside the reservation when it is not 0, and `leasehold doctor` warns about a pool where a
  range alone is never granted more than its MIN, judging only pools that a range rule
  leases in or whose headroom the config sets.
- `memory_per_slot`, a `[[commands.rule]]` key, `hold --memory-per-slot`,
  `acquire --memory-per-slot` and a `POST /v1/acquire` field, declares memory for each slot
  granted instead of a total. It counts as the size granted times it, and, when contained, the
  command is held to that (`MemoryHigh`). A request gives `memory` or `memory_per_slot`, not
  both. `[ceiling] memory` limits a per-slot request with or without a memory budget: MIN
  times the size over it is refused, MAX is cut to fit it, and a rule whose MIN times the size
  is over it is served as `never`. A per-slot lease's `memory_bytes` in the status and leases
  is the total it was granted, its weight times the size.
- A granted lease, in the acquire answer, `GET /v1/leases`, `GET /v1/status` (`leases` and
  `queue`) and `acquire`'s JSON line, has `min_weight`, `max_weight` and
  `memory_per_slot_bytes` beside `weight`, the size granted; a queued item's `weight` is its
  least. A queued answer to `POST /v1/acquire` gives the `min_weight` and `max_weight` the
  request waits with, its MAX cut as admission would cut it now. `POST /v1/renew` answers a
  lease's `weight`, `min_weight`, `max_weight`, `pool` and `jobs`. Events carry `min_weight`
  and `max_weight`. `GET /v1/commands` serves each rule's `max_weight`, `memory_per_slot`,
  `ceiling` (the most one lease under the rule could ever be granted) and `hook_ceiling` (what
  the rule's range alone would be granted on an idle machine), computed when served. The
  status `schema` stays 2.
- `leasehold top` shows a ranged lease's weight as `3/2-4` (granted 3 of 2-4) and a waiting
  range as `needs 2-4 slots`; `leasehold leases` the same in its `W` column. A waiting range's
  `blocked` says `this needs at least 2 (asks up to 4)`. `leasehold commands` shows each
  rule's range and memory per slot.
- A PATH shim that runs inside a Claude Code hook's lease (under `leasehold run`) sizes its
  program to that lease: the hook notes the job count, the `threads` the command line gives
  (`leasehold.shell.threads`: a count, "jobs", or an xdist run's share) and the size granted.
  The shim sizes the variables `hold` would to the hook's lease, `LEASEHOLD_WEIGHT` among
  them, and keeps a value the user set. A note from an older hook
  with four fields gives no sizing, and the program runs as before. A note with six fields
  gives the job count and threads but no weight, and the shim drops any `LEASEHOLD_WEIGHT`
  the program would inherit.
- The MCP server shows a lease's range only when the daemon's answer gives one, so a daemon
  from before ranges shows the single weight it granted. A lease granted while the agent was
  away takes its weight, range, pool and job count from the renewal.
- Library: `leasehold.weights` (`parse`, `text`, `asked`, `ENV_WANT`);
  `leasehold.admission.Grant` and `grants`; `Waiting.max_weight`, `Waiting.memory_per_slot`
  and `Waiting.most`; `Held.memory_per_slot`; `PoolPolicy.headroom`;
  `leasehold.admission.alone`; `AdmissionPolicy.most`, `lease_memory` and `observed`, which
  `from_pools` takes, and `lease_memory`, which `leasehold.machine.MachineConfig.policy`
  takes; `AdmissionPolicy.counted`, `refusal` and
  `heaviest`, and `fits`, take `memory_per_slot`;
  `leasehold.model.Pool.headroom` and `headroom_slots`;
  `leasehold.ledger.LedgerItem.max_weight` and `memory_per_slot`; `Renewal.weight`,
  `min_weight`, `max_weight` and `pool`; `leasehold.agent.AcquireRequest.max_weight` and
  `memory_per_slot`; `leasehold.commands.CommandRule.max_weight`, `memory_per_slot` and
  `most`; `leasehold.shell.Rule.max_weight`, `memory_per_slot`, `ceiling` and
  `hook_ceiling`;
  `leasehold.machine.rule_memory`; `leasehold.hold.hold`, `lease_request` and `run_leased`,
  and `leasehold.handoff.acquire`, take `max_weight` and `memory_per_slot` (`run_leased` only
  the latter); `leasehold.hold.own_env`; `leasehold.hooknotes.write` takes `jobs`,
  `threads` and `weight`, and `leasehold.hooknotes.sizing` and `weight` read them;
  `leasehold.hooknotes.under_agent`; `leasehold.machine.lease_memory_limit`; `leasehold.jobs.is_share`;
  `leasehold.shims.covered_env`; `leasehold.doctor.check_headroom`;
  `leasehold.mcp_server.Held.waiting`, and `Held.sizes` returns None when the daemon has said
  no range; `leasehold.client.OPTIONAL`.
- The benchmark (`bench/`, not part of the package):
  - The leasehold conditions take parameters, as in `lh-enforced@w=2-4,h=1,kept=3`: the weight
    or range jobs ask for, the work pool's headroom, CPUs kept back, `mem=declared`,
    `budget=SIZE`, and `arm=TAG` to run one condition as two arms.
  - Two new conditions: `lh-jobserver`, whose jobs are make jobserver clients
    (`job.py --jobserver`), and `lh-elastic`, which stops with an error unless the installed
    leasehold has elastic leases.
  - `lh-elastic` takes `grow=keep|leave-one|use`, the daemon's `[elastic] headroom`.
    `lh-jobserver` asks `hold --no-elastic` when the installed leasehold has elastic leases,
    so a range there stays at the size it was granted.
  - Each round runs on its own seed, `--seed-base` plus the round, in a Williams order.
    `--round N` with `--dir DIR` runs one round of a schedule into a results directory.
  - Before each run, the harness waits for swap-in, memory pressure and dirty pages to settle
    as well as load. A wait that times out is retried once and then leaves the run invalid.
    A source the wait cannot read counts as not quiet, and the harness stops before the first
    run if it cannot read them all. `--drop-caches` empties the page cache first.
  - A run is invalid if its inner process failed, if an agent or sampler thread raised (which
    aborts the run), or if the number of jobs that ran differs from its plan.
  - `--machine-class`, `--note` and `--workload`. Each run gets a `manifest.json`: the commit,
    the leasehold version, the condition and its config, the seed, and the machine, without
    its name.
  - Pre-registered gate files (`bench/gates/aa.toml`, `pilot.toml`, `defaults.toml`), each
    pinning its workloads' `args`, and `bench/run.py compare`, which judges results by one.
    It uses paired log ratios over exactly the first N valid rounds, one-sided t bounds, an
    intersection-union verdict, hard rules on every candidate run (memory pressure relative to
    the control's, and never judged without it), and a sign-permutation check. Runs replaced by
    a rerun are listed, and their hard-rule breaks still count. It refuses results with another
    seed base, other `args`, duplicate rounds, mixed commits, checkout states, leasehold
    versions, machine classes, machines or harness settings, or a pair with two seeds.
  - `compare --plan GATE --arms N` applies a gate's `[plan]` rule to an A/A run's SDs: the
    fewest rounds from 8 to 12 that are whole Williams cycles of N arms and whose joint power
    over every metric and workload is 0.8, with the throughput margins widened to 1.10 if
    needed. `--write FILE` freezes that plan, with the A/A results' SHA-256. A confirmatory
    gate names the file in `[plan] frozen`, gives no verdict without it, and takes its rounds
    and margins from it. `compare` refuses a plan file that its gate's rule does not give at
    the file's own SDs, and, when the A/A results are at hand (`--aa DIR`), one whose SHA-256
    or SDs they do not match.
  - `--blind-sd` gives each metric's pooled SD for the blind look, which may add whole cycles
    to the frozen rounds. The look is fixed the first time it is ready (`blind-<gate>.json`).
  - `spec.json` records `harness` (`--no-scope`, `--no-wait`, `--drop-caches`,
    `--allow-dirty`) and whether tracked files had uncommitted changes. A run refuses to start
    on a checkout with uncommitted changes unless `--allow-dirty` is given; only a `--tiny`
    run without `--dir` or `--round` is exempt.
- A policy simulator for the benchmark, `python -m bench.sim` (`run`, `calibrate`, `study`).
  Simulated agents run the benchmark's plans, or replay a measured run, and leasehold's own
  `admission.grants` decides when each job starts and at what size. It is calibrated against
  the runs under `bench/results/8cpu-16g/` and compares weight defaults (docs/sim.md). It needs
  SimPy, from a new `bench` dependency group that only `uv sync --group bench` installs; the
  package's own dependencies are unchanged.
- Elastic leases: a range can grow and shrink after it is admitted (`elastic: true` in
  `POST /v1/acquire`, or `POST /v1/elastic` for a range already held). Its holder promises to
  take slots when they are offered and to give them back when asked. The daemon works out
  each one's `target` in every decision. The target rises by one slot per decision, up to the
  range's most, while nothing waits in its pool and memory is neither short nor under
  pressure, once the lease's size has stayed put for `[elastic] settle_s` (new, 15 s by
  default, at least 1 s). How much of the pools' `headroom` it leaves free is
  `[elastic] headroom` (new): `"keep"` all of it (the default), `"leave-one"` one slot, or
  `"use"` none. The target falls, largest extras first, when a request whose turn has come
  would then fit, and falls to the least for every elastic lease while memory is under
  pressure. Nothing is taken from a lease that ignores its target, and nothing is killed
  (docs/reference.md, "Elastic leases").
- `POST /v1/resize` (`lease_id`, `weight`) moves an elastic lease: giving slots back always
  succeeds and admits whatever then fits; taking slots succeeds up to the target while they
  are still free, and is answered 409 `conflict` (a new error code) otherwise. A refused
  resize changes only that lease's target. `POST /v1/elastic` (`lease_id`) makes a held range
  elastic, and is answered 409 `conflict` for a lease not held and 400 for one with no range.
  `leasehold.client.Client.resize` and `make_elastic`.
- `hold --jobserver` with a range is elastic unless `--no-elastic` is given. It asks for a
  plain range and makes the lease elastic once a job takes a jobserver token, so a command that
  never takes one (a `make -jN`, which runs its own jobserver) keeps its size. It renews at
  least every `settle_s`, puts tokens in its jobserver and resizes its scope as the lease grows.
  When the lease shrinks, a thread blocked reading the jobserver takes each token a job gives
  back, and a slot is given back only once its jobs' tokens have come back, however long they
  take. A job killed while it holds a token keeps that slot held until the command ends. Its
  calls to the daemon for this wait at most a renewal interval. Only jobserver clients (make,
  ninja 1.13 and later, cargo and rustc, GCC's LTO) see the size change: the job and thread
  variables keep their first values, except that an elastic lease sets no
  `CMAKE_BUILD_PARALLEL_LEVEL` and sets `CARGO_BUILD_JOBS` to the job count of its most. `hold
  --jobserver` warns when the command line gives its own `-j`, with a number or without one.
- `hold --elastic` without `--jobserver` is refused and exits 2.
- The grant and the renewal answers carry `elastic`, `target` and `target_jobs`, and the grant
  the daemon's `[elastic]` settings and the lease's `max_jobs`; the status object's leases
  carry `elastic`, `target` and `giving_back` (the slots asked back and not yet given), and
  its queued items `elastic` (`schema` stays 2). A new event, `resized`, carries the lease's
  new `weight` and its `previous_weight`. `top` shows a target that differs from the weight
  as `3>4/2-4`, and so does `leasehold leases`; `top` says beside a lease that it is giving
  slots back. A request first in line while elastic leases give slots back says so in its
  `blocked` text and in `hold`'s waiting line.
- Library: `leasehold.elastic` (`Elastic`, `targets`, `Headroom`, `HEADROOM_MODES`,
  `DEFAULT_HEADROOM`); `leasehold.follow.Follower` and `WATCH_S`;
  `leasehold.jobs.Jobserver`, `own_job_count`, `ELASTIC_SKIPPED` and `ELASTIC_MOST`, and
  `job_env` and `sized_env` take `elastic_jobs`; `leasehold.contain.update_scope`,
  `scope_properties` and `lease_unit`; `leasehold.ledger.Ledger.resize`, `make_elastic`,
  `Resize`, `ELASTIC_SETTLE_S`, and `Ledger.settle_s` and `Ledger.headroom` (constructor
  arguments); `LedgerItem.elastic`; `Renewal.target` and `elastic`;
  `leasehold.agent.AcquireRequest.elastic`, `MachineAgent.resize`, `make_elastic` and
  `Conflict`; `leasehold.machine.ElasticSettings` and `MachineConfig.elastic`;
  `leasehold.events.EventType.RESIZED`; `leasehold.hold.hold` and `run_leased` take
  `elastic`, and `child_env` takes `elastic_jobs`.
- Status reports the cgroup memory limit (see Changed): `memory.limit_bytes` and
  `memory.limit_cgroup` give the limit the daemon started under and the cgroup that sets it,
  and `load.memory_limit` (`cgroup`, `limit_bytes`, `used_bytes`) the limit as last read. All
  are null without a limit.
- New public names for it: the module `leasehold.cgroup` (`own_cgroup`, `limit`, `used`,
  `MemoryLimit`, `LIMIT_FILES`); `leasehold.load.available`, `LoadReader.limit` and its
  `memory_limit` argument; `limit_bytes` for `MemorySettings.resolve` and
  `MachineConfig.memory_budget`, and `memory_limit` for `MachineConfig.policy`;
  `MachineAgent.memory_limit`; `memory_limit` for `leasehold.status.report` and `limit` for
  `leasehold.status.memory`.

### Changed

- On 4 to 7 slots, the config `init` and `setup` write for your own daemon sets
  `[commands] heavy_weight` and `light_weight` to run two builds or test runs at once (3 and 2
  on 6 slots), where the built-in weight 4 runs one at a time. The pre-registered `defaults-h1`
  bench gate passed this setting against weight 4 on both of its workloads; its verdict is in
  `bench/results/8cpu-16g/defaults-h1/`. The built-in weights stay 4 and 2, so a config
  written before 0.4 runs as it did. `setup --upgrade-config` rewriting a config exactly as 0.2
  wrote it offers the two lines as comments and keeps weight 4.
- `bench/run.py` resuming a run, and `compare` pooling runs, accept a machine whose memory
  differs by up to 1%: a guest booted afresh reports a few KiB more or less.
- Your own daemon's `"auto"` memory budget follows the cgroup memory limit its work runs
  under: the lowest `memory.max` or `memory.high` on the daemon's cgroup and the cgroups above
  it (cgroup v2). When that limit is below the machine's memory, the budget is the limit less
  the same share of it that `kept` is of the machine, so `assumed_per_slot = "auto"` shrinks
  with it. Inside a 7 GiB scope on a 16 GiB machine the budget was 12 GiB and is now 5.25 GiB. A
  budget given as a size is left alone, and the machine's daemon (`serve --system`) still
  budgets the whole machine.
- The memory guard's available memory, and so what may start while it is on, is the smaller of
  `MemAvailable` and what that limit leaves: the limit less what the limiting cgroup uses now,
  without its inactive file cache.
- A client's `--config` outranks `$LEASEHOLD_SOCKET`: it takes the socket of the config the
  flag names before the environment's, so a command inside `hold` can still name another
  daemon. `serve` is unchanged.
- The benchmark's `--rounds` gives each round a new seed; it used to run every round on one
  plan. With three or more conditions, the order is a Williams design instead of every other
  round reversed. `spec.json` says whether `git status` was timed instead of naming the
  repository, holds the config `init` wrote (named `bench`) instead of the plan, and records
  the run's sizes (`args`), commit and leasehold version. A resumed run must match all of them,
  and the harness settings, the checkout state and the machine (class, CPUs, memory, swap,
  kernel, virtualisation) it started with.
  The leasehold conditions' config and ledger now sit beside the daemon's socket, with a copy
  in each run's `config.toml`.
- The benchmark's wait for a quiet machine needs memory pressure under 0.5%, not 1%. The
  default results directory is named after `--machine-class` when one is given.
- A variable already set is the user's, and `hold` leaves it alone unless the lease the
  command runs in set it to the value it still has.
- A user's `OMP_NUM_THREADS` is copied to `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` where
  the user left them unset, and neither a nested `hold` nor a PATH shim lowers it or them,
  even inside a lease already at one thread.
- A command line that runs several programs takes the fewest threads any of them allows,
  so `cargo test && pytest -n 4` gets the job count divided by 4. Programs on it that no rule
  names get that count too, and a pytest run with xdist workers limits it to its share even
  when no rule names pytest.
- `hold` without `--weight` reads its command as the agent hooks do, so `hold -- uv run pytest`,
  `hold -- uvx pytest` and `hold -- python -m pytest` match the pytest rules. It used to match
  on the first word only. Finding the program skips the options of `uv` (before and after
  `run`) and `uvx` that take a value, and Python's options before `-m`. Free-threaded Pythons
  (`python3.13t`), full versions (`python3.13.1`) and PyPy count as Python.
- `hold --no-job-env` also removes the enclosing lease's sizing: `LEASEHOLD_SET`,
  `LEASEHOLD_JOBS`, `LEASEHOLD_THREADS`, and every job variable the lease set that still has
  its value. With `--weight` it no longer asks the daemon for the rules.
- `leasehold.jobs.job_env` takes `threads` and `cap`; `JOB_VARIABLES` is both groups together.
  `leasehold.hold.hold`, `run_leased` and `child_env` take `threads`, and `child_env` takes
  `env`. `leasehold.hold.auto_size` takes `env` and `cwd`, and returns the matching
  `leasehold.shell.Rule` instead of a `(weight, memory)` pair. `leasehold.shell.programs`
  yields `Found` pairs.
- `leasehold.admission.decide` is `grants` without the sizes. Admission, the ledger and every
  view count a lease at the size it was granted (`leases.weight`), which is its least unless
  it asked a range.
- `hold` removes `LEASEHOLD_WANT` from its command's environment, with a lease or without one.
- `LEASEHOLD_WANT` past the most a rule's lease could ever be granted is cut to it, and
  leasehold says what it cut; a shim or the hook no longer has the command refused for it
  (with a daemon that serves `ceiling`).
- The Claude Code hook asks for a rule's range, or a range in `LEASEHOLD_WANT`, as a range,
  with both ends cut to the rule's `hook_ceiling`. It waits for MIN only, and admission sizes
  it like any other range. Its note records the size granted, the job count and the threads,
  so a PATH shim the command runs through under `leasehold run` sizes the command to the
  grant. Outside `leasehold run` the hook cannot size the command, which uses what it finds,
  as under a fixed heavy rule in 0.3. When a range is granted less than its most, the hook
  also tells the agent the size, as `additionalContext`: "leasehold: pytest was granted 3 of
  2-6 slots; size it to 3 jobs (or run the agent under `leasehold run`)". A fixed weight, or
  a range granted its most, tells it nothing, as in 0.3. When the hook cuts either end to
  `hook_ceiling`, its log says so. A daemon from before ranges grants the hook its MIN, as it
  does any range.
- When a hook or shim gives up waiting, its busy message says it gave up its place in line
  (`gave up its place in line (#1)`) where it said `queued #1`.
- A request refused for asking more memory than one lease may be given gives the total in
  GiB, and names the limit: `[ceiling] memory` when the config sets it, else the machine's
  memory in GiB, where it gave the machine's size in bytes and called it `[ceiling]
  memory`. A rule served as `never` for the same reason says the same.
- A PATH shim run under an agent whose hook holds a lease now, for any program, ignores
  `LEASEHOLD_WANT`: in the agent's environment it was what the hook asked for.
- `leasehold.client.Client.acquire` asks a daemon that refuses any of `memory`, `max_weight`
  and `memory_per_slot` as unknown again without them, one round per refusal, where it retried
  only `memory` before.
- Among one source's `[commands]` rules that match, the one with the greatest weight (the
  least of a range) wins, the first of equals, as before; the docs now say so.
- Asking again under a lease id already waiting keeps the first request's range: to change
  it, give the place up and ask under a new id (docs/reference.md).
- A lease's `weight` is the size it holds now: an elastic lease's moves, and every reader of
  the ledger, the status object and events sees the size it holds. A lease that is not
  elastic keeps the size it was granted, as before.
- `leasehold.client.OPTIONAL` includes `elastic`.
- `AdmissionPolicy.most` cuts a range that declares no memory, on a machine with a memory
  budget, to the slots whose assumed memory fits `[ceiling] memory`, as it already cut a
  range declaring memory per slot.

### Fixed

- Finding the daemon no longer crashes when the system socket sits in a directory the user may
  not enter, as on a machine whose system daemon keeps `/run/leasehold` to its own group. That
  socket is passed over with "permission denied", and every command that finds a daemon (`leases`,
  `status`, `doctor`, `hold`) goes on to the next candidate.

### Compatibility

- A daemon that does not serve `threads` gives every rule `"jobs"`, the thread count of 0.3.0,
  and serves no xdist rule; a pytest run with xdist workers still shares the job count among
  them. A client older than this one matches the xdist rule's `args` as a
  regular expression over the joined arguments.
- A lease started by leasehold 0.3.0 records no `LEASEHOLD_SET`, so a newer `hold` inside it
  takes its values for the user's.
- A `[[commands.rule]]` for pytest without `args` is tried before the built-in xdist rule. It
  sets the weight; its `threads` can only lower the workers' share.
- The xdist rule does not reach `python -m pytest` or a virtualenv's `pytest` that a script
  runs inside a lease, commands leased by the Claude Code plugin, or tox and nox environments,
  which do not pass these variables in by default.
- The ledger's tables gain columns in place, at the same schema version (4), as 0.3 added
  `memory`: `queue.max_weight` and `queue.memory_per_slot`; `leases.min_weight`,
  `leases.max_weight` and `leases.memory_per_slot`, all NULL for rows written before. A 0.3
  process sharing the ledger reads each lease's `weight` as the size it holds, and admits a
  waiting range at its least. A lease that declares `memory_per_slot` also records the total
  it was granted (its weight times the size) as `memory`, which is what a 0.3 process counts,
  so both count the same bytes. `AdmissionPolicy.counted` takes the per-slot size when an item
  has both. Two daemons on one ledger is unusual, but if a 0.3 process admits a queued
  per-slot request, it writes no memory for it, and this version counts that lease at the
  assumed size.
- A client asking a daemon from before ranges is granted the least of its range; a
  `memory_per_slot` is sent to it as the `memory` of that least. A client reading rules from
  such a daemon (no `max_weight` served) asks for each rule's `weight` alone, and, with no
  `ceiling` served, does not cut `LEASEHOLD_WANT`.
- A rule that declares only `memory_per_slot` is also served a `memory`, its least weight's
  worth, so a 0.3 client still declares memory for it. A newer client reads the
  `memory_per_slot` and ignores that `memory`.
- With every request asking a single weight (no range anywhere), admission decides exactly as
  before, at the same sizes. Built-in rules and the configs `init` writes keep fixed weights.
- A config from 0.3 has no `headroom`, so its pools get `"auto"`; that only affects ranges.
- A daemon from before elastic leases refuses `elastic`; `Client.acquire` asks again without
  it, and the lease keeps the size it is granted. Such a daemon has no `/v1/resize` or
  `/v1/elastic`, and `hold` keeps the lease at its size. A client from before elastic
  leases never asks for one, and reads an elastic lease's `weight` as the size it holds.
- The ledger gains more columns in place, at the same schema version (4):
  `queue.elastic`, and `leases.elastic`, `leases.target` and `leases.resized_at`, all NULL for
  rows written before (a lease from before is fixed). `leases.weight` is always the size held,
  and a per-slot lease's `memory` the total at that size, so a 0.3 process sharing the ledger
  counts an elastic lease at its size now. A 0.3 process neither works out targets nor
  resizes. It admits around them as it would around any lease.
- With no elastic lease, admission decides exactly as before.

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
