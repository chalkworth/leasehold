# leasehold reference

The detail behind the [README](../README.md): how leases are admitted, every config key, the
command line, the HTTP API, and the integrations.

- [Leases](#leases): kinds, pools, turns, memory, lending, observed programs
- [Installing](#installing): your own daemon, the machine's daemon
- [The config file](#the-config-file)
- [Running work under a lease](#running-work-under-a-lease): `hold`, sizing, exit codes, `acquire`
- [GitHub Actions runner hooks](#github-actions-runner-hooks)
- [Coding agents](#coding-agents): `[commands]`, the Claude Code plugin, shims, MCP
- [Seeing the machine](#seeing-the-machine): `top`, `ui`, `metrics`, `report`, `doctor`
- [The HTTP API](#the-http-api)
- [The command line](#the-command-line)

## Leases

A lease is a claim on some of the machine's **slots** (its `weight`) in one **pool**, for as
long as its holder keeps renewing it. Every lease has a TTL (`lease_ttl_s`, 15 s by default):
`hold` renews every TTL/3, and a holder that stops renewing loses the lease, wherever it runs. A
place in line has the same TTL, kept by asking again or by `renew`. Leases live in the ledger, a
SQLite file, so a restarted daemon still has them. Each grant carries an increasing `grant`
number, a fencing token.

### Kinds

| Kind | For | Preempted |
|---|---|---|
| `work` | the machine owner's own work | never |
| `external` | someone else's bounded job: a CI runner, a test gate | never |
| `lend` | spare capacity lent out (a batch job, a volunteer-computing client) | always: revoked when the machine needs it back |
| `observed` | a known program seen running that never asked for a lease (`[[observe]]`) | never; only the daemon makes them |

Each kind goes to a pool (`[kinds]`, by default the pool of the same name). A pool listed in
`[lend] pools` takes only `lend` leases, and reserves nothing.

### Pools and turns

The ceiling's slots are shared by the pools. A pool **reserves** some slots that only its own
work may take, and has a **max** it never goes past. Without `[pools]`, a machine with S slots
gets this split:

- `work`, served first, reserves half (at least 1 once S >= 2) and may use all S;
- `external` reserves nothing and may use what `work` does not reserve;
- `lend`, served last, reserves nothing, may use the same, and is the only pool preempted;
- `observed` reserves nothing and is never asked for.

A request fits when its pool has room under its max, and the slots not reserved for other
pools have room for it (and, where [memory is counted](#memory), the memory budget too). When it does not fit, it waits in its pool's queue. Within a pool,
**turns go round by Unix user**: the user with the fewest leases running goes next, then the one
served longest ago. Within a user's turn, the user's **sessions** take turns the same way
(`hold --session`, `LEASEHOLD_SESSION`, or the label `session`), so splitting work into
sessions only divides that user's own share. There is no backfill: a small request does not
jump a large one at the head of the queue, so a large one is never starved. The one exception
is short-lived: while the [memory guard](#memory-guard) settles, a request held back only by
the memory measured free lets lighter work behind it start, for at most `settle_s`.

Each waiting item says why it waits (`blocked`), for example "pool work holds 6 of 6; this
needs 1" or "2 of 12 slots are free; this needs 4", and, with memory counted, "the memory
budget is used: 9.0 of 11.7 GiB counted; this counts 5.9 GiB" or "memory is short (...): 0.4
GiB may start now; this counts 5.9 GiB". `hold` and `acquire` print it once while they wait.

### Memory

A config with a `[memory]` table (the one `init` and `setup` write for your own daemon) counts
memory as well as slots. A request starts only when its slots fit and the memory it counts fits
the **budget**: the machine's memory less a share kept back for you.

```toml
[memory]
budget = "auto"            # the machine's memory less `kept`; or a size such as "12g"
kept = "25%"               # with "auto": a share of the memory, or a size such as "4g"
assumed_per_slot = "auto"  # what a request that declares no memory counts, per slot of weight
```

A request counts the memory it **declares**: `hold --memory 6g` (or `acquire --memory`), or the
`memory` of the [`[commands]` rule](#which-commands-take-a-lease-commands) it matches. One that
declares none counts its weight times `assumed_per_slot`; `"auto"` is the budget divided by the
slots, so a machine whose slots are all held has its budget all counted, and a request is
assumed to need memory in proportion to the CPU share it asked for. That is the most cautious
estimate that never refuses what the slots would admit: with nothing declared, memory and slots
run out together, and a declared size, larger or smaller, replaces the estimate. It also means
that with nothing declared the budget never binds: a default config holds back only declared
and `[[standing]]` memory with it, and undeclared work by its slots and the guard. Set it to a
size to assume a fixed amount a slot, or `"0g"` to count only what is declared (`leasehold
doctor` warns about that).

A budget given as a size must be more than 0 and more than the `[[standing]]` memory, and
leave room beside it for every `[[commands.rule]]`'s declared memory, or the config does not
load. An `"auto"` budget worked out as 0 (`kept` at or over the machine's memory) stops the
daemon starting; one that standing memory fills, or that leaves a rule's declared memory no
room, fails `leasehold doctor`'s `memory` check. A rule no lease could ever be granted for
(its declared memory over the room beside standing memory or over what one lease may be
given, no weight of it fitting the slots, or its pool missing) is marked `never` with the
reason in `leasehold commands`, and `doctor`'s `rules` check fails it. Its commands are
refused by the agent hook and the shims, not run under another rule's lease. When standing memory leaves room for less than a rule's weight
at `assumed_per_slot`, the rule is served at the weight that fits (so what it asks for is
granted, not refused), and `doctor` warns when that cuts the built-in rules. A declared size
(`hold --memory`, a rule's `memory`, `POST /v1/acquire`'s `memory`) is more than 0; a size
anywhere is at most 1 EiB.

A declared size is also what a contained command is held to (`MemoryHigh`, see
[Containment](#containment)), which throttles it past that size but never kills it: the budget
decides what starts, it does not bound what runs. A request that counts more than the whole
budget, or declares more than `[ceiling] memory`, is refused. The budget counts only what
leasehold admits (and `[[standing]]` memory); memory other programs use is the
[guard](#memory-guard)'s to notice.

Without a `[memory]` table no memory is counted, as in leasehold 0.2, and a declared size only
sets `MemoryHigh`. A 0.2 config gains the table with `leasehold setup --upgrade-config` (see
[Upgrading a 0.2 config](#upgrading-a-02-config)).

### Memory guard

Running out of memory is what freezes a machine that runs too much at once. Your own daemon
watches memory pressure (PSI `some avg10`) and available memory. The guard goes on when
pressure passes `[guard] memory_pressure_on` (20%) or available memory drops under
`min_available`, and off once pressure has stayed at most `memory_pressure_off` (5%), with
memory above the floor, for `settle_s` (30 s). While it is on, what runs keeps running, and
what new work may start depends on whether memory is counted:

- **With `[memory]`**, nothing new starts while a reading is over a threshold, whatever it
  counts or declares (what waits says "memory is short (...): nothing new starts until the
  pressure eases"). While the guard settles, new work may count at most the memory measured
  free: what is available above `min_available` less what leases granted in the last `settle_s`
  count (they may not have grown to it yet). A request heavier than its pool's reservation
  starts as soon as its memory fits. One whose turn it is but that does not fit what is free,
  though its slots and the budget would take it, does not hold up its pool: lighter work behind
  it that fits may start (its own session's later work keeps waiting behind it), until it has
  waited `settle_s`; after that it holds up its pool, so a stream of small requests in its pool
  cannot keep it waiting (other pools' requests may still start around it, as with slots
  alone). A daemon started again counts a lease granted within `settle_s` of the restart
  as recent; observed leases are not counted (their memory is already out of what is
  available). What waits
  says "memory is short". The settling and the settle window are measured on a clock that never
  steps, so changing the system time does not end or stretch them.
- **Without it** (a 0.2 config), every pool admits only its reserved slots, and the rest waits,
  saying "the machine is under pressure". A request heavier than its pool's reservation then
  waits until the guard goes off; `leasehold doctor` warns when that is every built-in build
  and test rule.

The machine's daemon (`serve --system`) runs without the guard unless `[guard] enabled = true`.

### Lending

A `lend` lease is granted only while the machine is idle, and has been for `quiet_s` (10 min by
default):

- no item of any other kind waits for capacity;
- CPU and memory pressure (PSI `some avg10`) are at most `cpu_pressure_max` and
  `memory_pressure_max`. Without PSI the 1-minute load average per CPU is used instead
  (`load_per_cpu_max`), and unknown load is never idle;
- the local time is inside `window`, when one is set.

When any of these stops holding, every lend lease is revoked at once, and the quiet period
starts again from zero. A non-lend request that has to queue revokes lending in the same call;
pressure and the window are checked every tick (`tick_s`). Revocation reaches the holder
through its next renewal, which answers `revoked`. The lease keeps its weight while the holder
stops (at most `revoke_grace_s` plus one TTL), so the work that triggered the revocation is
admitted once the lent work has really stopped.

By default pressure is the whole machine's (`/proc/pressure`), so lent work's own load can
revoke it. Set `pressure_cgroups` to the other slices to measure only them, and run lent work
in `lend.slice` (see [Installing](#installing)).

An administrator can switch lending off and on (`leasehold lend off|on`), for maintenance or a
benchmark; switched off, it stays off until switched on or the daemon restarts.

### Observed programs

An `[[observe]]` rule names a program that runs outside leasehold but is known to be heavy:

```toml
[[observe]]
name = "training-prep"
match = { comm = "python3", cmdline = 'prep_dataset\.py' }   # also exe = "...", user = "..."
weight = 6
max_count = 2          # count at most two at once
```

On every tick the daemon reads the process table and gives each matching process an
`observed` lease of `weight` in the `observed` pool. The lease is fenced by the process's pid and
start time: it ends when the process does, and a reused pid never keeps it. Only a rule's
roots count, a matching process with no matching ancestor, so a job's worker processes are part
of the one job. A leasehold command, and anything run under one (`hold`, the PATH shims), is
never observed: it already holds a lease, or is waiting for one. A command the Claude Code plugin
leases is not under leasehold, so a rule that also matches it counts it twice; aim `[[observe]]`
at programs started some other way.

Observing is accounting, not control. The program has already started, so its weight is held
even past its pool's max or the machine's slots, and `status` marks the machine and the pool
`overcommitted`. New work waits until enough of it ends. A program is seen within one tick (2 s
by default). `leasehold observe` shows what the rules match right now.

Where `/proc` is mounted with `hidepid`, the daemon sees only the processes its user may see,
and process fencing falls back to the pid alone; `leasehold doctor` says so.

## Installing

leasehold runs on Linux with Python 3.12 or later. PSI (`/proc/pressure`, kernel 4.20 and later)
and cgroup v2 are used when present.

### Your own daemon

```sh
uv tool install leasehold
leasehold setup
```

`setup` does everything a daemon of your own needs, says what it did at each step, and is safe
to repeat:

1. writes `~/.config/leasehold/machine.toml`, sized as `init` sizes it, unless a config is there
   (it never touches one);
2. installs `~/.config/systemd/user/leasehold.service`, running this installation's
   `leasehold serve --user`, and enables and starts it (with no systemd user manager, it says
   to run `leasehold serve &` instead);
3. installs the [PATH shims](#path-shims) for the heavy programs on your PATH;
4. names the coding agents it finds on PATH and how to start each under `leasehold run`;
5. reports whether commands are [contained](#containment) here, and anything `doctor` fails.

`leasehold setup --remove` stops and deletes the unit and the shims, and leaves the config and
the ledger. By hand instead:

```sh
uv tool install leasehold
leasehold serve &          # or: systemctl --user enable --now leasehold (deploy/leasehold-user.service)
leasehold status
```

It runs as you, with its socket in `$XDG_RUNTIME_DIR/leasehold/` (0600) and its ledger in
`~/.local/state/leasehold/`. With no config file it takes one slot per CPU and the default pools,
and says so; `leasehold init` writes a commented config sized for the machine to
`~/.config/leasehold/machine.toml`: one slot per CPU less two kept for you (one on a machine of
3 to 7 CPUs), two pools (`work` for your agents and commands, `background` for `--kind
external`, which never takes work's reserved half), no lending, memory counted against
three quarters of the machine's (`[memory]`), the memory guard on, and the built-in build and
test rules at their weight of 4, so on 8 CPUs (6 slots) one build or test run runs at a time.

The two kept CPUs are kept only from commands that run as many jobs as their lease gives them
(`MAKEFLAGS`, `CARGO_BUILD_JOBS` and the others under [Containment](#containment)); a tool that
ignores the job count can still fill every CPU. `[contain] hard_cpu = true` caps each contained
command at its weight in CPUs; it is off by default, and `leasehold doctor` warns while it is.

**Two at once (opt-in).** `[commands] heavy_weight = 3` and `light_weight = 2` (on 6 slots; the
generated config shows the values for its slots, commented out) let two builds or test runs
run at once, each with fewer jobs. A command that declares no memory is then counted at its
weight times the budget over the slots, which may be less than it uses: declare memory for
memory-heavy work (a rule's `memory`, or `hold --memory`), or two large builds can overlap.
[Measured](bench.md#two-heavy-jobs-at-once-opt-in).

#### Upgrading a 0.2 config

`setup` never changes a config that is there, so one that leasehold 0.2 wrote keeps counting no
memory after an upgrade (the guard then holds pools to their reservations). The `[memory]`
table is optional. For a user's own config (`mode = "user"`, or no `mode`, which the daemon
reads as user), `leasehold doctor` (check `defaults`) and `leasehold setup` say what it lacks:
`doctor` as a warning for a config exactly as 0.2 wrote it, and as information for one you
have edited since.

```sh
leasehold setup --upgrade-config --dry-run   # what would change, as a diff
leasehold setup --upgrade-config             # change it; the old file is kept as machine.toml.0.2.bak
systemctl --user restart leasehold
```

A config exactly as 0.2's `init` wrote it is written afresh from today's template for the same
machine (name, slots, CPUs, per-lease memory; the built-in rules keep their weights). One you
have edited keeps every line, and gains only a `[memory]` table at the end (and today's comment
above `[guard]`, if 0.2's is there unchanged). `--config PATH` upgrades another file.

Line endings are kept (CRLF in, CRLF in what is added), and the backup is a byte-for-byte
copy. It refuses (exit 1, saying what to do) a config that is a symbolic link or has other hard
links, and one that, with the budget the new table would have on this machine, would not let
work start as before: `[[standing]]` memory that fills the budget, a `[[commands.rule]]` whose
declared memory never fits beside the standing memory or is more than `[ceiling] memory` (with
memory counted, such a rule's commands would be refused, not only held to it), or the built-in
rules cut to a lighter weight. It also refuses one that changes while it works: the file is
checked again after the new one is written to a temporary file and synced to disk, and its
identity once more just before the rename. A write landing in the instant between that last
check and the rename is lost: neither the new file nor the backup has it.

### The machine's daemon

For a machine several people share, one daemon runs as its own account, and everyone who may
take leases is in its socket's group. As root:

1. `useradd --system --no-create-home --shell /usr/sbin/nologin leasehold`, and add each user
   who may ask for leases to its group: `usermod -aG leasehold <user>` (they must log in again).
2. Install a tagged release into its own venv, from a clone checked out at the tag:
   `python3 -m venv /opt/leasehold && /opt/leasehold/bin/pip install .` (or `uv venv` and
   `uv pip install`). Link `/opt/leasehold/bin/leasehold` into `/usr/local/bin` for users.
3. `leasehold init --system` writes `/etc/leasehold/machine.toml`, sized for the machine
   (`leasehold facts` shows what it found). Edit it.
4. Copy `deploy/leasehold.service` and the slices you want (`work.slice`, `gates.slice`,
   `runner.slice`, `lend.slice`; size their placeholders) to `/etc/systemd/system/`, then
   `systemctl daemon-reload && systemctl enable --now leasehold`.
5. Check: `leasehold doctor`, then `leasehold status`.

The service makes `/run/leasehold` (0750, the socket inside 0660) and `/var/lib/leasehold`
(0700), and runs `serve --system`. `CPUWeight=idle` in `lend.slice` needs systemd 252 or later.

Clients find a daemon from `--socket`, then `LEASEHOLD_SOCKET`, then the config file's `socket`;
told none, they try the system socket, then their own, and use the first that answers.
`leasehold doctor` lists each candidate and why it was taken or passed over.
`serve` listens on `--socket`, then `LEASEHOLD_SOCKET`, then the config's `socket`, then its
mode's own.

Run lent work in `lend.slice`, so `pressure_cgroups` leaves it out and the slice's limits
apply:

```
sudo systemd-run --slice=lend.slice --uid=<user> --gid=<user> --same-dir --pty --wait --collect \
  -E HOME=/home/<user> -E PATH="$PATH" \
  leasehold hold --kind lend -- ./batch-job
```

`hold` stops the command's process group on revocation; under `systemd-run` the unit's cgroup
also catches anything that left the group.

## The config file

`--config` or `LEASEHOLD_CONFIG`, else `~/.config/leasehold/machine.toml` for your own daemon and
`/etc/leasehold/machine.toml` for the machine's (`serve --system`). With none, a daemon runs on
one slot per CPU and the default pools. `deploy/machine.toml` is the template `init` writes, for
an example machine. An unknown key is an error.

| Key | Default | Meaning |
|---|---|---|
| `name` | the hostname | the machine's name |
| `[labels]` | none | free-form `key = "value"` labels, shown in `status` |
| `[ceiling] slots` | required | the slots the pools share |
| `[ceiling] cpus`, `memory` | the machine | the most one lease may be given (`memory` as `48g`) |
| `mode` | `user` (or `serve --system`) | which daemon the config is for, `system` or `user` |
| `ledger_dir` | `/var/lib/leasehold`, or `~/.local/state/leasehold` | where `ledger.sqlite3` lives |
| `socket` | `/run/leasehold/leasehold.sock`, or `$XDG_RUNTIME_DIR/leasehold/leasehold.sock` | the daemon's socket |
| `socket_group`, `socket_mode` | the daemon's group; `0660` (system) or `0600` (user) | who may use it |
| `admin_group` | none | members may switch lending and change pool limits, as root and the daemon's own user always may |
| `tick_s`, `lease_ttl_s` | `2`, `15` | the tick period and the lease TTL |
| `[pools.<name>]` | the default split | `reserved`, `max`, `priority`, `queue_timeout_s`, and per-user limits: `per_user_max_running`, `per_user_max_queued`, `create_rate_per_min` |
| `[kinds]` | each kind to its namesake | the pool each kind goes to |
| `[lend]` | see [Lending](#lending) | `pools`, `quiet_s` (600), `cpu_pressure_max` (10), `memory_pressure_max` (5), `load_per_cpu_max` (0.25), `window` (as `"22:00-07:00"`), `revoke_grace_s` (30), `pressure_cgroups` |
| `[load] slices` | none | the cgroups (systemd slices) whose CPU and memory `status` reports |
| `[jobs] per_slot` | `1` | jobs per slot, for [sizing](#sizing-the-command-to-its-lease) |
| `[[standing]]` | none | long-lived work outside leasehold that permanently holds capacity: `name`, `pool`, `weight` (required), `kind` (`"workload"`), `cpus`, `memory`, `note` |
| `[[observe]]` | none | see [Observed programs](#observed-programs): `name`, `match` (`comm`, `exe`, `cmdline`, `user`), `weight`, `pool` (`observed`), `max_count` |
| `[commands]` | the built-in rules | see [`[commands]`](#which-commands-take-a-lease-commands): `defaults`, `[[commands.rule]]`, `heavy_weight` (4), `light_weight` (2), `wait_s`, `when_busy` |
| `[memory]` | none: no memory counted | see [Memory](#memory): `budget` (`"auto"`), `kept` (`"25%"`), `assumed_per_slot` (`"auto"`) |
| `[guard]` | on for your own daemon | see [Memory guard](#memory-guard): `enabled`, `memory_pressure_on` (20), `memory_pressure_off` (5), `min_available` (5% of memory, at least 512 MiB), `settle_s` (30) |
| `[contain]` | `mode = "auto"`, `hard_cpu = false` | see [Containment](#containment): whether commands go in systemd user scopes, and whether a scope is also capped at its weight in CPUs |

The ceiling clamps the pools: a pool's `max` is clamped to what the pool could ever hold, the
ceiling's slots less the other pools' reservations (the observed pool's only to the slots), and
a request that could not fit even on an idle machine is refused rather than queued. Standing
weight must leave every pool's reservation whole: each pool's standing weight or its
reservation, whichever is more, must together fit the slots. An administrator can change a
pool's max until the daemon restarts (`leasehold pool set NAME --max N`, or `--reset`), never
above what the pool could ever hold beside the other pools' reservations and standing work.

## Running work under a lease

### `hold`

```sh
leasehold hold -- cargo test --workspace
```

Without `--weight`, `hold` weighs the command as the [`[commands]` rules](#which-commands-take-a-lease-commands)
weigh it (cargo test: `heavy_weight` slots, cut to what its pool may hold), and anything they
do not name as 1 slot. `--memory 6g` declares the memory it will use (default: its rule's
`memory`), counted against the [memory budget](#memory) and held to when contained.
`--threads N` sets the parallelism inside each of its processes (default: its rule's
`threads`, else the job count; see [sizing](#sizing-the-command-to-its-lease)). `hold` reads
the rules when no `--weight` is given, or for the threads when the job variables are on; with
`--weight` and `--no-job-env` it does not ask the daemon for them.

`hold` waits for a lease, runs the command in its own process group, renews while it runs
(every TTL/3, each renewal waiting at most TTL/3 for an answer), forwards SIGTERM, SIGINT and
SIGHUP to the group, releases when the command ends, and exits with its code. The lease covers
the group: anything left in it when the command ends is stopped too. The command sees
`LEASEHOLD_LEASE_ID`, `LEASEHOLD_GRANT`, `LEASEHOLD_POOL` and `LEASEHOLD_WEIGHT`.

SIGTERM, SIGINT or SIGHUP before the command runs (while `hold` waits in line, or while it
starts the command) gives back the place in line or the lease, stops anything already started,
and exits 128 + the signal; SIGINT ends `hold` by SIGINT, which the shell reports as 130.

Run from a terminal, in the foreground, `hold` hands the command's group the terminal, so it can
read it and Ctrl-C reaches it as if the shell had run it. Ctrl-Z stops the command and `hold`
with it, and `fg` or `bg` continues both. When Ctrl-C (or Ctrl-\) kills the command, `hold`
releases the lease and then ends by the same signal, so the shell sees the interrupt and stops
a loop or script that runs `hold` (the shell still reports 130, or 131). Run any other way, the
command gets a session of its own, and a command killed by a signal makes `hold` exit 128 + the
signal.

### Containment

Where it can, `hold` (and so the PATH shims) starts the command in a systemd user scope sized to
the lease, so the lease holds the command back instead of only counting it:

- `CPUWeight` is 100 × the weight. Weights share the CPU among siblings when it is contended,
  and leave any CPU nobody else wants to whoever wants it: a weight-4 command gets four times
  the CPU of a weight-1 command beside it. All of leasehold's scopes sit in `leasehold.slice`,
  which shares with the rest of your session as one ordinary unit; inside it, each `leasehold
  run` session's slice shares equally with the others. Weights decide shares only when the CPU
  is contended: a command that ignores its job count still runs on every idle CPU, the ones
  kept for you included, and on this benchmark's machine weights did not keep a shell's wakeup
  latency down ([bench](bench.md)). `[contain] hard_cpu = true` caps each command at weight
  CPUs (`CPUQuota`); it is off by default, and `leasehold doctor` warns while it is.
- `hold --memory 4g`, or a rule's `memory`, sets `MemoryHigh`: past it the kernel throttles the
  command and reclaims its memory. It is soft. It never kills, and it does not bound the
  command outright: nothing sets `MemoryMax`, and the machine can still run out of memory.
  The same size is what [memory admission](#memory) counts before the command starts, and the
  [memory guard](#memory-guard) is what stops new work starting while memory is short.

The scope is `lease-<id>.scope` in `leasehold.slice`, or in the slice of the `leasehold run`
session it was started from. Every process the command starts stays in it. If the user manager
refuses the scope or does not answer within 30 s, the command has not run yet; it then runs
uncontained, and `hold` says so.

It is used when the daemon's `[contain] mode` is `auto` (the default) and the caller runs in your
own session (anything under `/user.slice/user-<uid>.slice/`) with a systemd user manager that
answers and is delegated the cpu and memory controllers, the default on current Debian, Ubuntu
and Fedora.
A command started from a system service, a CI runner's job or an operator's slice is left where
it is, under that service's limits and clean-up. Elsewhere, or with `LEASEHOLD_CONTAIN=off`, the
command runs as before and the lease is advisory. `leasehold doctor` says which applies.

### Sizing the command to its lease

A lease of weight W allows W × `[jobs] per_slot` jobs. `hold` sets two groups of variables
(`--no-job-env` sets none).

How many jobs or worker processes a tool fans out to is set to the job count:

| Variable | Read by |
|---|---|
| `LEASEHOLD_JOBS` | anything: the job count itself |
| `CARGO_BUILD_JOBS` | cargo build, test and check |
| `CMAKE_BUILD_PARALLEL_LEVEL` | `cmake --build` |
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | `pytest -n auto` (pytest-xdist) |

The parallelism inside each process is set to the thread count:

| Variable | Read by |
|---|---|
| `LEASEHOLD_THREADS` | anything: the thread count in effect |
| `OMP_NUM_THREADS` | OpenMP: numpy and other BLAS users, PyTorch, compilers with `-fopenmp` |
| `OPENBLAS_NUM_THREADS` | OpenBLAS (numpy's and scipy's wheels), which reads it before `OMP_NUM_THREADS` |
| `MKL_NUM_THREADS` | Intel MKL; PyTorch reads `OMP_NUM_THREADS`, then `MKL_NUM_THREADS` overrides it |
| `RUST_TEST_THREADS` | test binaries built by cargo (`cargo test` runs them one at a time) |
| `GOMAXPROCS` | Go programs, `go build` and `go test` included |
| `POLARS_MAX_THREADS` | Polars |
| `RAYON_NUM_THREADS` | Rust programs using rayon |
| `LOKY_MAX_CPU_COUNT` | joblib and loky process pools: scikit-learn's `n_jobs=-1` |
| `DASK_NUM_WORKERS` | Dask's threaded and multiprocessing schedulers (the `num_workers` setting) |
| `PYTHON_CPU_COUNT` | Python 3.13 and later: `os.cpu_count()` and `os.process_cpu_count()`, and so `multiprocessing`, `ProcessPoolExecutor` and Dask's default worker count |

`NUMBA_NUM_THREADS` is not set: Numba takes it as a ceiling, and `numba.set_num_threads()`
above it is an error.

The thread count is the job count unless the command's `threads` says otherwise: `hold
--threads N` (or `--threads jobs`), else the matching [`[commands]` rule's](#which-commands-take-a-lease-commands)
`threads`. A count above the job count is cut to it. With the default, every variable gets the
job count.

**A value you chose wins.** A variable already set is left alone, unless a lease around this
one set it: `hold` lists the names it set in `LEASEHOLD_SET`, and a `hold` inside it sets those
to its own lease's values. `OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS` are
one family: when you set any of them, `hold` sets none of them, since OpenBLAS would read its
own variable before your `OMP_NUM_THREADS` and PyTorch would let `MKL_NUM_THREADS` override
it. `LEASEHOLD_JOBS` and `LEASEHOLD_THREADS` are always this lease's: the job count, and the
thread count in effect (your value of the family when you set one: `OMP_NUM_THREADS`, else
`OPENBLAS_NUM_THREADS`, else `MKL_NUM_THREADS`).

**Worker processes and N × N threads.** A command whose jobs are worker processes would, with
the job count in both groups, let each of N workers start an N-thread BLAS or OpenMP pool: N × N
threads on N jobs' worth of CPU. The built-in rules give an xdist run (`pytest` or `py.test`
with `-n N`, `-nN`, `-n=N`, `--numprocesses N` or `--numprocesses=N`, N a count, `auto` or
`logical`) `threads = 1`; a serial `pytest`, tox, nox and every other built-in rule have the
job count. This covers an xdist run through `hold`, through a PATH shim, and through a shim
under a lease that set the variables (`hold -- make test`, whose `pytest -n 4` the pytest shim
runs with one thread per process, in the same lease). It does not cover an xdist run leased by
the [Claude Code plugin](#the-claude-code-plugin), which never changes a command's
environment, nor one tox or nox starts in its own virtualenv, whose `pytest` comes before the
shim on PATH: set `threads` in a rule of your own for tox or nox, or `OMP_NUM_THREADS` in
their config.

`hold --jobserver` also hands the command a GNU make jobserver: a named pipe holding one token
fewer than the job count, announced in `MAKEFLAGS` as `--jobserver-auth=fifo:PATH` (the protocol
of GNU make 4.4). make, ninja 1.13 and later, cargo and rustc, and GCC's LTO take a token before
each extra job, so a whole tree of builds, sub-makes included, runs at most that many jobs at
once. The command must not pass its own `-jN`: make then runs its own jobserver instead. A tool
that does not speak the protocol, or make before 4.4, still has the variables. steve and
guildmaster run one jobserver for a whole machine; here it is one per lease.

### Exit codes

`hold`, `acquire`, and the commands that talk to a daemon exit with these codes of their own;
otherwise `hold` exits with its command's code. Any `leasehold` command interrupted by Ctrl-C
ends by SIGINT (130 to the shell); `acquire` gives back its place in line first.

When no daemon answers, or none can be found, `hold` says `no daemon answers; running without a
lease` on stderr and runs the command as it would under a lease, but with no lease, scope or
job-count variables. `--if-unavailable fail` (or `LEASEHOLD_IF_UNAVAILABLE=fail`) makes it exit
69 instead, as `acquire` does.

| Code | Meaning |
|---|---|
| 75 | still waiting at `--timeout`: busy, not failed. The place in line is given back |
| 76 | the lease was revoked (or, for `lend`, lost): the group got SIGTERM, then SIGKILL after `--grace` (30 s) |
| 77 | refused: the request could never be granted (heavier than its pool or the ceiling, or a pool that does not exist), or the daemon answered it with an error (such as 500 `internal` or 503 `ledger_unavailable`). The command does not run |
| 69 | no daemon answered (`hold` only with `--if-unavailable fail`) |
| 127 | the command could not be started |

### A lease across processes

`hold` ties a lease to one command. When the process that asks for a lease ends before the work
does, and a later process gives it back (a CI runner's job-started and job-completed hooks),
use `acquire` and `release` instead:

- `acquire --lease-id ID` waits in line as `hold` does, with the same exit codes (75, 77, 69),
  prints the granted lease as one JSON line and exits 0. The caller picks the id, so the
  releasing process can name it.
- `--keep` first starts a detached renewer (`leasehold keep ID`: its own session, stdio on
  /dev/null, so it keeps no pipe of the caller open), whose pid the JSON line carries as
  `keeper_pid`. It renews every TTL/3.
- `release ID` gives the lease back (exit 0; 69 without a daemon). The renewer finds it gone at
  its next renewal and exits.

The TTL stays the safety net. The renewer stops when the lease is released, revoked or unknown,
when `--watch-pid`'s process is gone (pid and start time, so a reused pid does not count), or
after `--max-hold` (6 h by default) whatever happens; the lease then ends one TTL later. A
killed renewer ends it the same way. A renewer rides out a daemon that does not answer, so a
restart within the TTL loses nothing. Only the user who acquired a lease, or root, may release
it.

## GitHub Actions runner hooks

`deploy/github-runner/` makes a self-hosted runner take an `external` lease per job:

- `job-started.sh` (the runner's `ACTIONS_RUNNER_HOOK_JOB_STARTED`) runs `acquire --keep` with
  the id `gh.<runner>.<run id>.<attempt>.<job>`, weight `LEASEHOLD_RUNNER_WEIGHT` (4) and the
  labels `consumer=github-runner`, `runner`, `repository`, `run` and `job`. A runner hook cannot
  hand a job back (it runs after the job is assigned, with no timeout), so the hook *is* the
  queue: while the machine is busy the job waits in line here, before its first step, until
  capacity frees or the job's own `timeout-minutes` ends it. The renewer watches the job's
  `Runner.Worker` (an ancestor of the hook) and stops after `LEASEHOLD_RUNNER_MAX_HOLD` (21600 s)
  in any case.
- `job-completed.sh` (`ACTIONS_RUNNER_HOOK_JOB_COMPLETED`) releases it.
- `runner-slice.conf` is a systemd drop-in putting the runner service in `runner.slice`.

Both hooks **fail open**: with leasehold not installed, its daemon unreachable, or the request
refused, the job runs without a lease, and the hook writes a `WARNING ... WITHOUT a lease
(unaccounted)` line to the job's log and, through `logger` (tag `leasehold-runner`), the journal.
A machine with leasehold down is no worse off than one without it. Neither hook ever fails a
job. Gitea and Forgejo's act_runner read the same hook variables.

Installing them, as root, for a runner `<svc>` (`actions.runner.<owner>-<repo>.<name>`) run by
user `<runner-user>` from `<runner-dir>`:

1. The machine's daemon is installed and `leasehold` is on the runner user's PATH.
2. `usermod -aG leasehold <runner-user>`. The service picks the group up when it restarts (step 5).
3. `install -d /opt/leasehold/github-runner && install -m 0755 deploy/github-runner/*.sh
   /opt/leasehold/github-runner/`
4. Add to `<runner-dir>/.env` (the runner reads it at start):
   ```
   ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/leasehold/github-runner/job-started.sh
   ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/leasehold/github-runner/job-completed.sh
   ```
   and optionally `LEASEHOLD_RUNNER_WEIGHT=...` or `LEASEHOLD_RUNNER_MAX_HOLD=...`.
5. Install `deploy/runner.slice` (if not yet) to `/etc/systemd/system/`, and the drop-in:
   `install -D -m 0644 deploy/github-runner/runner-slice.conf
   /etc/systemd/system/<svc>.service.d/runner-slice.conf`, then `systemctl daemon-reload &&
   systemctl restart <svc>.service`.
6. Check: `systemctl show -p Slice <svc>.service` says `runner.slice`; during a job,
   `leasehold leases` lists `gh.<runner>....` with `consumer=github-runner`.

[`examples/github-runner/`](../examples/github-runner/) shows the ephemeral-runner pattern too.

## Coding agents

### `leasehold run`

```sh
leasehold run -- claude
```

`run` starts an agent (or any command) as one session. It sets `LEASEHOLD_SESSION`, so the
session's leases take turns with your other sessions, and puts the [PATH shims](#path-shims)
first on its PATH when they are installed, so the heavy commands it runs take leases. The agent
keeps the terminal, as under a shell.

Where commands can be [contained](#containment), the agent runs in a scope of a slice of its
own, `leasehold-<session>_<token>.slice`, where the token is this run's alone (the slice is named
to its commands in `LEASEHOLD_SLICE`), and the scopes of its leased commands go in the same
slice. When the agent exits, whatever it left running there gets SIGTERM, and SIGKILL 10 s
later; nothing outside the slice is touched. A clean-up that fails is reported, and the exit
code stays the agent's. `--leave-running` keeps what is left (a dev server you still want).
`--session` names the session; `--no-contain`, or `[contain] mode = "off"` in the config, runs
the agent uncontained.

### Which commands take a lease: `[commands]`

The Claude Code plugin and the PATH shims decide from the same rules whether a command is heavy
and what it weighs. A rule names programs by basename and, optionally, a regular expression
their arguments must contain:

```toml
[commands]
wait_s = 300           # how long an agent's hook or a shim waits in line (at most 540)
when_busy = "deny"     # then: "deny" the command, or "run" it without a lease and say so
# heavy_weight = 3     # the built-in build and test rules' weight (default 4)
# light_weight = 2     # the built-in JavaScript tooling rules' weight (default 2)

[[commands.rule]]
programs = ["pytest"]
args = "--benchmark"   # only benchmark runs
weight = 6
memory = "8g"          # counted against [memory]; held to this when contained (MemoryHigh)
threads = 1            # parallelism inside each process (OMP_NUM_THREADS ...); default "jobs"

[[commands.rule]]
programs = ["make"]
weight = 2
pool = "external"
```

The config's rules are tried first, then the built-in ones (`defaults = false` drops those):
pytest and py.test (with xdist workers, and without), tox and nox; cargo build, test, check, clippy, bench, run and doc; make, ninja and
bazel; `cmake --build`; go build and test; mvn and gradle; docker and podman build; npm, pnpm,
yarn and bun install, test and build; jest, vitest, playwright, tsc, webpack and vite. The
builds and test runs weigh `heavy_weight` (4), the JavaScript tooling `light_weight` (2); lower
them to run more at once ([two at once](#your-own-daemon), opt-in).
A weight is both what a command costs in slots and the job count it is given. A rule's
`threads` sets the parallelism inside each process ([sizing](#sizing-the-command-to-its-lease)):
`"jobs"`, the job count (the default), or a count of at least 1; the built-in xdist rule has
1. Among
one source's rules the heaviest match wins. The daemon clamps each weight to the most its pool
could ever grant one lease, beside the other pools' reservations and standing work. `leasehold commands` lists the rules; `leasehold commands --test "CMD"` says which
one a command matches.

A command line is read as the shell would split it, without running anything: `&&`, `||`,
`;`, pipes, groups, redirections, `VAR=1` assignments, wrappers (`env`, `time`, `nice`,
`timeout`, `sudo`, `xargs`, `npx`, `uv run`, `poetry run` and others, with their options) and
`sh -c '...'` are seen through. What a literal `false &&` or `true ||` skips is left out. A
program reached through a variable, an alias, a function or a script is not seen.

### The Claude Code plugin

```
/plugin marketplace add chalkworth/leasehold
/plugin install leasehold@leasehold
```

It needs `leasehold` on the PATH Claude Code runs with, and a daemon. Before each Bash command,
its `PreToolUse` hook (`leasehold hook claude-code pre`) checks the command against
`[commands]`. For a heavy one it asks for a lease, labelled with the agent session and the
command, in that session's turn, and waits up to `wait_s`; a light one goes straight through.
The command is never rewritten, so Claude Code's permission rules apply to what the agent
wrote. When the command ends, `PostToolUse` or `PostToolUseFailure` gives the lease back.

- **Sub-agents** run in their parent's session, so a session's agents share its turns.
- **The lease outlives nothing.** A renewer keeps it while the command runs, watches the Claude
  Code process, and stops at the Bash call's own timeout plus a minute, so an interrupted call
  cannot hold slots.
- **A busy machine.** The command waits in line, keeping its place, for up to `wait_s` (300 s
  by default). One that waited at least 5 s says so as it starts (`leasehold: pytest waited
  42s in line for its lease`), so a slow start is not taken for a slow command. Still waiting
  after `wait_s`, it is refused with a message that says the command did not run and tells
  the agent to do lighter work first (`when_busy = "deny"`, the default), or it runs without
  a lease and the agent is told so (`"run"`). Either message names the heaviest lease in the
  pool, by its command and session, and the command's place in line: `machine busy for 300s:
  cargo test (session cc-ab12) holds 8 slots, 2 other leases hold 6; pool work holds 14 of 14;
  this needs 4; queued #1`. `leasehold doctor` warns when `wait_s` is under 120 s.
- **The daemon says no.** A `refused` answer (a request that could never be granted, a
  per-user limit), an error answer under 500 (such as 403 `forbidden`), or a matching rule
  that `leasehold commands` marks as never granted: the command is denied with the daemon's
  text, whatever `when_busy` says. A command matching a never-granted rule is never run under
  a lighter rule's lease instead.
- **The daemon cannot answer.** No daemon is reached, or it answers with an error of 500 or
  more (such as 503 `ledger_unavailable` on a full disk): the command runs without a lease,
  and the agent is told why. The same rule holds for listing the rules and for asking for the
  lease.
- **Passes through** for a command run in the background and inside a lease already held
  (`LEASEHOLD_LEASE_ID` set).

The hooks go through a pre-filter, `hooks/prefilter.sh`, so that an ordinary Bash command
starts no Python (about 10 ms instead of a tenth of a second or more). It reads the command
from the event and starts `leasehold hook claude-code pre` only when one of its words is a
program a rule names, from a word list in `$XDG_CACHE_HOME/leasehold/programs`
(`~/.cache/leasehold/programs`). The hook writes the list itself and keeps it in step with the
daemon's rules whenever it runs; `leasehold commands --programs --write` writes it at once
(after changing `[commands]`, say). Without a list, every command goes to the hook. After the
command, `post` starts only when the hook noted a lease for that call (in
`$XDG_RUNTIME_DIR/leasehold-hooks-<uid>/`).

The plugin's skill tells the agent what a wait, a refusal and the exit codes mean.

### PATH shims

```sh
leasehold shims install            # ~/.local/share/leasehold/shims; --only pytest,cargo
export PATH="$HOME/.local/share/leasehold/shims:$PATH"
```

For each program `[commands]` names that is on PATH now (`--all` for every one), `install`
writes a small `sh` script. It runs the real program, the next one on PATH that is not a shim,
under a lease when its arguments match a rule, sized as `hold` sizes it, for the life of the
process. It passes straight through, as if no shim were there, inside a lease already held
(including the lease the Claude Code plugin took for this very command, so `leasehold run --
claude` with the plugin takes one lease per command, not two), with `LEASEHOLD_SHIMS=off`, when no rule matches, and when no daemon answers. While it
waits it says its place on stderr, again whenever that changes and at least every 20 s. Busy
past `wait_s`, it exits 75 and says why on stderr (`when_busy = "run"`: runs the program without a
lease and says so). When the daemon says no (a refusal, an error answer under 500, a
matching rule never granted) it exits 77 with the daemon's text, without running the program,
whatever `when_busy` says; when it answers with an error of 500 or more, it runs the program
without a lease and says why, as with no daemon.

Shims see only what is run through PATH: `.venv/bin/pytest`, `python -m pytest` and absolute
paths go round them. `shims list` shows what is installed, `shims remove` removes only shims.

### MCP server

```sh
uv tool install 'leasehold[mcp]'
claude mcp add leasehold -- leasehold mcp
```

`leasehold mcp` is an MCP server on stdio, for any MCP client;
[`integrations/mcp/`](../integrations/mcp/) has the configs for Claude Code, Codex, Cursor and
Gemini CLI. Its tools:

| Tool | What it does |
|---|---|
| `machine_status` | Free slots, each pool, who waits and why, pressure and lending, ending with a recommendation |
| `acquire_lease` | Ask for a lease of a weight; answers granted (with the job count) or queued within 20 s |
| `wait_for_capacity` | Wait up to 20 s more for a queued lease |
| `release_lease` | Give back a lease this server made |
| `list_my_leases` | What this server holds and has waiting |

No tool runs a command: the agent runs its work with its own shell tool. The server renews what
it holds, gives back a place in line nobody has asked about for two minutes, and releases
everything when the session ends.

### AGENTS.md

[`integrations/AGENTS.md.snippet`](../integrations/AGENTS.md.snippet) is a section to paste into
a repository's `AGENTS.md` or `CLAUDE.md`, for agents that use none of the above.

## Seeing the machine

- **`leasehold status`** prints the machine: pools, load, lending, leases and the queue.
  `--json` prints the [status object](#the-status-object).
- **`leasehold top`** is the machine live in the terminal: the slots as numbered bays, each
  lease spanning as many as it weighs and each waiting one in a second lane; PSI pressure with a
  sparkline; the pools; the leases; the queue with why each waits and, with memory counted,
  what each counts; the memory budget in the header (`mem 5.9/11.7 GiB`); lending; recent
  events.
  Keys: `q` quits, `p` pauses, `s` changes the order of the leases, `?` shows help. It follows
  `NO_COLOR` and draws in ASCII outside a UTF-8 locale. Off a terminal, on one smaller than
  80x20, or with `--once`, it prints one plain view and exits.
- **`leasehold ui`** serves the same view to a browser on `127.0.0.1`, at a link that carries a
  random token, and opens it (`--no-open` to only print it). The page is read-only; requests
  without the token, or for another `Host`, are refused.
- **`leasehold metrics`** prints the status in the Prometheus text format;
  `--textfile DIR` writes `DIR/leasehold.prom` for node_exporter's textfile collector.
  `leasehold ui` serves the same at `/metrics?token=...`.
- **`leasehold report --url URL`** POSTs the status object to a URL every `--interval` seconds
  (30), with `--token-file` as a bearer token and `--header`s of your own: https unless the URL
  is on loopback, the token file not world-readable, redirects not followed.
  `deploy/leasehold-report.service` runs it.
- **`leasehold doctor`** checks the config, the socket (its path within the 107 bytes a unix
  socket allows, a `socket_mode` that lets every user in) and its permissions, the daemon and
  its clock, PSI, cgroup v2 and the slices the config names, `/proc` visibility, the ledger,
  and whether commands are contained (`enforced (systemd)`, or `advisory:` and why). It warns
  when your own config lacks what today's has ([upgrading](#upgrading-a-02-config)), when no
  built-in build or test rule could start while the memory guard is on, and checks that the
  [memory budget](#memory) worked out for this machine lets work start, and fails a
  `[commands]` rule no lease could ever be granted for (its commands are refused). It exits 1
  on any failure and prints a fix for each problem. `--json` for scripts.
- **`leasehold demo`** runs a made-up busy machine, a daemon with a scripted crowd, in a
  temporary directory, and shows it in `top` (`--ui` for the browser). It touches nothing real.

## The HTTP API

HTTP/JSON on the unix socket. The peer's uid (`SO_PEERCRED`) names the user a lease is charged
to, and only that user, or root, may renew or release it. `leasehold.client.Client` is a small
client for it.

| Request | Body | Answer |
|---|---|---|
| `GET /v1/status` | | the machine: [below](#the-status-object) |
| `GET /v1/leases` | | `leases` and `queue` |
| `GET /v1/events?since=N&limit=M` | | `events` after number N, oldest first, and `next` |
| `GET /v1/commands` | | the `[commands]` rules, each weight clamped as the daemon would grant it; a rule no lease could ever be granted for carries `never`, the reason |
| `POST /v1/acquire` | `kind`, `pool`, `weight`, `labels`, `session`, `lease_id`, `wait_s`, `ttl_s`, `memory` (a size such as `"6g"`) | `status`: `granted` (with `lease`), `queued` (with `position` and `blocked`) or `refused` (with `reason`) |
| `POST /v1/renew` | `lease_id`, `ttl_s` | `ok`, `revoked` (with `reason`), `queued` or `unknown` |
| `POST /v1/release` | `lease_id` | `released` |
| `POST /v1/lend` | `open`: true or false | the lend state; administrators only |
| `POST /v1/pools/<name>` | `max`: a number, or null to reset | the pool; administrators only |

`acquire` waits up to `wait_s` (at most 300) for the lease, then answers `queued`; asking again
with the same `lease_id` keeps the place in line, and is not a new request. A place given back
(`/v1/release`) while its `acquire` waits ends that wait, `refused`. A new request over
its pool's per-user limits (`per_user_max_queued`, `create_rate_per_min`) is `refused` with
`code: "quota_exceeded"`; one that fits now is granted whatever the waiting limit. A 0.2
daemon does not take `memory`; `leasehold.client.Client` then asks again without it.

An acquire naming a pool that does not exist is `refused` with `reason` `there is no pool
'NAME'`.

An error is answered with an `error` and a `detail`: 400 `bad_request` (a malformed request,
including a `Content-Length` that is not a non-negative integer), 403 `forbidden` (another
user's lease, or an administrator's route), 404 `not_found`, 413 `too_large` (a body over
64 KiB, which is not read), 500 `internal` (the daemon failed on the request; its traceback goes
to the daemon's stderr), and 503 `ledger_unavailable` (the ledger cannot be used now, at start
or later, such as locked past the busy timeout, a full disk or a read-only file system;
admission fails closed). A client that stops sending part way through a request is cut off
after 30 seconds without an answer.

Events are kept in memory, the last 500, each with a `type`: `granted` (and whether it
`waited`), `queued` (with its `position`), `left` (the line, without a grant), `released`,
`ended` (with a `reason`: `expired`, `holder gone`, or `no longer matched` for an observed
program), `revoked`, `lend_opened` and `lend_closed`. A restarted daemon numbers them afresh.

### The status object

`GET /v1/status` and `status --json`, versioned by its `schema` field (2), which goes up only
when a field is removed or changes meaning:

- `version`, `mode` (`user` or `system`), `time`, `machine` (`name`, `labels`), `facts`;
- `ceiling` (`slots`, `cpus`, `memory`, `memory_bytes`), and the machine's `slots`, `held` and
  `overcommitted`;
- `pools`, per pool: `reserved`, `max`, `priority`, `preemptible`, `observed`, `closed`,
  `held_weight`, `overcommitted`, `running`, `revoked`, `queued`, `memory_counted_bytes`. A
  pool's `held_weight` includes its standing weight; `running` counts leases only;
  `memory_counted_bytes` is the memory admission counts there (0 without `[memory]`);
- `load`: `pressure`, `loadavg`, `mem_available_bytes`, `mem_total_bytes`, `cgroup_v2`,
  `slices`;
- `lend`: `eligible`, `reason`, `quiet_since`, `quiet_s`, `window`, `pools`, `switch`;
- `guard`: `enabled`, `on`, `pressing` (whether the last reading was itself over a threshold,
  not only settling), `reason`, `since`, `memory_pressure_on`, `memory_pressure_off`,
  `min_available_bytes`, `settle_s`;
- `memory`: `counted` (whether the config has `[memory]`), `machine_bytes`, and with it
  `budget_bytes`, `kept_bytes`, `assumed_per_slot_bytes`, `held_bytes` (counted now, standing
  memory included) and `free_now_bytes` (what may start now while the guard is on, else null);
  without it these are null;
- `contain`: the daemon's `[contain]` settings, `mode` and `hard_cpu` (whether a given caller
  is contained is that caller's own `leasehold doctor`);
- `standing`, `leases`, and `queue`, each queued item with its `position` and why it waits
  (`blocked`). Each lease and queued item has `memory_bytes` (what it declared, or null) and
  `memory_counted_bytes` (what admission counts for it).

## The command line

Every command takes `--config PATH` and `--socket PATH`, before or after the command's name.
`leasehold --help` lists the everyday commands; the rest (`acquire`, `keep`, `release`, `hook`,
`shims`, `facts`, `ui`, `metrics`, `mcp`, `report`, `observe`, `lend`, `pool`) work the same and
are named at its end. `leasehold COMMAND --help` says more; `mcp`, `ui`, `metrics`, `report`,
`observe` and `lend` are experimental and may change before 1.0.

<!-- cli:start -->

set up your own leasehold: config, daemon, shims; --remove undoes it:

```
leasehold setup [--remove] [--upgrade-config] [--dry-run]
```

run an agent as one session: its heavy commands leased and contained:

```
leasehold run [--session SESSION] [--leave-running] [--quiet] [--no-contain] ...
```

run a command under a lease:

```
leasehold hold [--kind {work,external,lend}] [--pool POOL] [--weight WEIGHT] [--label K=V] [--session SESSION] [--timeout TIMEOUT] [--ttl TTL] [--memory MEMORY] [--grace GRACE] [--jobserver] [--no-job-env] [--threads N|jobs] [--if-unavailable run|fail] ...
```

the machine live: slots, pressure, pools, leases, the queue and events:

```
leasehold top [--interval S] [--once]
```

the machine: pools, load, lending, leases and the queue:

```
leasehold status [--json]
```

the leases held and the queue:

```
leasehold leases [--json]
```

check this machine and this user's setup:

```
leasehold doctor [--json]
```

watch a made-up busy machine; touches nothing real:

```
leasehold demo [--ui] [--port PORT] [--no-open] [--seed SEED]
```

which commands agents and shims take a lease for:

```
leasehold commands [--json] [--test COMMAND] [--programs] [--write]
```

run a daemon: your own, or --system for the machine's:

```
leasehold serve [--system | --user] [--verbose]
```

write a commented config sized for this machine:

```
leasehold init [--system | --user] [--print] [--force]
```

what the config's [[observe]] rules match right now:

```
leasehold observe [--json]
```

wait for a lease another process will release:

```
leasehold acquire --lease-id LEASE_ID [--kind {work,external,lend}] [--pool POOL] [--weight WEIGHT] [--label K=V] [--session SESSION] [--timeout TIMEOUT] [--ttl TTL] [--memory MEMORY] [--keep] [--max-hold MAX_HOLD] [--watch-pid WATCH_PID]
```

renew a lease until it is released or a limit is hit:

```
leasehold keep --ttl TTL [--max-hold MAX_HOLD] [--watch-pid WATCH_PID] lease_id
```

give a lease back:

```
leasehold release lease_id
```

this machine's facts (no daemon needed):

```
leasehold facts [--json]
```

the machine in your browser, on a private loopback link:

```
leasehold ui [--port PORT] [--no-open]
```

the status as Prometheus metrics:

```
leasehold metrics [--textfile DIR]
```

switch lending on or off (administrators):

```
leasehold lend {on,off}
```

set a pool's max, or --reset it to the configured one:

```
leasehold pool set (--max MAX | --reset) name
```

POST this machine's status to a URL, repeatedly:

```
leasehold report --url URL [--token-file TOKEN_FILE] [--header 'NAME: VALUE'] [--interval INTERVAL] [--once]
```

a coding agent's hook (reads its event on stdin):

```
leasehold hook {claude-code} {pre,post}
```

write a shim for each program the rules name:

```
leasehold shims install [--dir DIR] [--only ONLY] [--force] [--all]
```

the shims in the directory:

```
leasehold shims list [--dir DIR]
```

delete the shims (only leasehold's):

```
leasehold shims remove [--dir DIR] [--only ONLY]
```

an MCP server (stdio) for coding agents: status and leases, no command running:

```
leasehold mcp [--session SESSION]
```

<!-- cli:end -->
