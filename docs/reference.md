# leasehold reference

The detail behind the [README](../README.md): how leases are admitted, every config key, the
command line, the HTTP API, and the integrations.

- [Leases](#leases): kinds, pools, turns, memory, lending, observed programs, maintenance
- [Installing](#installing): your own daemon, the machine's daemon
- [The config file](#the-config-file)
- [Running work under a lease](#running-work-under-a-lease): `hold`, sizing, exit codes, `acquire`, [from Python](#from-python)
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
| `maintenance` | the whole machine, for a while: a benchmark window, an upgrade ([below](#maintenance)) | never |
| `observed` | a known program seen running that never asked for a lease (`[[observe]]`) | never; only the daemon makes them |

Each kind goes to a pool (`[kinds]`, by default the pool of the same name; `maintenance`, when
`[kinds]` leaves it out, goes to the `work` kind's pool). A pool listed in `[lend] pools` takes
only `lend` leases, and reserves nothing.

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
needs 1" or "2 of 12 slots are free; this needs 4" ("this needs at least 2 (asks up to 4)" for
a [range](#ranges-min-max-slots)), and, with memory counted, "the memory budget is used: 9.0 of
11.7 GiB counted; this counts 5.9 GiB" or "memory is short (...): 0.4 GiB may start now; this
counts 5.9 GiB". `hold` and `acquire` print it once while they wait.

### Ranges: MIN-MAX slots

A request may ask for a range of slots instead of one weight: `weight = "2-4"` in a
[`[commands]` rule](#which-commands-take-a-lease-commands), `hold --weight 2-4`, or `weight`
and `max_weight` in [`POST /v1/acquire`](#the-http-api). MIN is what the work needs, MAX what
it can use; leasehold does not care how it uses them. The config's limits (each pool's `max`,
what one lease could ever hold, the ceiling) stay the outer bounds. A waiting request keeps
the MAX it asked for; when it is admitted, the daemon cuts it to the most one lease could be
granted in its pool then, so a pool's `max` raised while it waits counts. Wherever a waiting
request is shown (its queued answer, its `blocked` text, the status queue, `top` and events),
its MAX is cut the same way. Whether a request can ever be granted, and whether a rule is
marked as never granted, is judged by MIN alone. `weight = N` is the range N-N, and everything
about it is as before ranges.

A range waits in line, and is admitted, exactly as a request of MIN would be: every rule in
[Pools and turns](#pools-and-turns) and [Memory](#memory) is applied at MIN, so ranges change
no one's turn. A waiting range holds its place at the head of its pool's queue by its MIN, as
any request does, so a large MIN waits for room and the requests in its pool wait behind it.
Then, in the same decision, each request admitted in a pool that asks for more is offered a share of F, how much more the pool could hold when the decision began: its `max`
less what it holds, and no more than the machine's free slots less those owed to other pools'
reservations. F is shared evenly among the requests the pool admitted in this decision and
those still waiting in it, each counted from its MIN up to its MAX. A request whose MAX is
below its even share takes its MAX, and the rest of its share goes to the others.

The extra slots are dealt one at a time, round the admitted requests in admission order, until
each has its offer or its next slot would break a limit. A slot is dealt only if:

- the memory guard has no allowance in force, does not hold the pools to their reservations,
  and memory is not under pressure. While it does either, or memory is short, every request
  is granted its MIN, and what the guard lets start is kept for the MIN of the requests that
  come next;
- the pool and the machine have room for it beside every request whose turn came in this
  decision but which was not admitted, in any pool: that request's MIN stays free in its pool,
  and on the machine as far as its pool's `max` would let it use it, and its memory stays free
  in the budget. Other pools' reservations are never taken;
- the pool still has its own `headroom` free after it, and the machine the largest `headroom`
  of the pools an extra slot could land in;
- the memory it counts fits the budget, beside the memory the machine's headroom would count
  at `assumed_per_slot` or the extra slot's own per-slot size, whichever is larger, so the
  request the headroom is kept for can start.

A request kept for this way never waits on an extra slot. A closed pool admits nothing, so
nothing is kept for its requests.

Headroom (`[pools.<name>] headroom`) is how many of a pool's slots extra slots leave free, so
a request that arrives a moment later starts at once instead of waiting for a range's whole
life. It is a count, or `"auto"` (the default): `max // 4`, so 2 for a pool of 8, 1 for a pool
of 4 to 7, and 0 below 4. It never holds back a MIN or a fixed weight, which may use the last
slots as before. The free slots it keeps stack with other pools' unused reservations, which
extra slots never take either: a pool with a reservation of 4 standing idle beside it leaves
those 4 free too. The machine's headroom is the largest of the pools an extra slot could land
in: a closed or capped pool, the observed pool (whose headroom is 0 unless the config sets
one), and a pool whose headroom is at least the most it could hold (its `max`, clamped as
[Pools and turns](#pools-and-turns) says) add nothing to it. `leasehold doctor`
warns about a pool where a range alone is never granted more than its MIN. `leasehold status`
shows each pool's headroom beside its reservation when it is not 0, and the status object
gives it as a count.

The size granted is the lease's `weight` for its life: its job count, thread counts, jobserver,
`LEASEHOLD_WEIGHT`, `CPUWeight` and, with a memory per slot, `MemoryHigh`. It does not grow or
shrink while the lease is held, unless the lease is [elastic](#elastic-leases). To ask for a
different range, release the lease (or give up the place in line) and ask again under a new
lease id: asking again under the same id keeps the first request's range.

For example, on an idle `work` pool of 8 slots with the default headroom of 2:

- one request of 1-8 alone gets 6 (its share is all 8, less the 2 kept as headroom), and a
  request of 2 that arrives next fits in the 2 left;
- two requests of 2-4 arriving together get 3 each (a share of 4 each, but only 6 can be
  dealt before the headroom, one slot to each in turn);
- a request of 1-2 and one of 1-8 arriving together get 2 and 4: the 1-2 is full at 2, so the
  1-8's share is the other 6, cut to 4 by the headroom (with a headroom of 0, 2 and 6);
- a request of 2-4 that arrives when only 2 slots are free gets 2;
- a request of 2-4 admitted ahead of a request of 7 that does not fit gets 2: the slots left
  are the 7's to wait for;
- a request of 8, or of 7-8, gets 8 or 7: headroom never holds back a MIN.

Headroom helps the requests whose MIN fits in it. A fixed request larger than the headroom
(a fixed 4 beside a lone 1-8 that took 6) waits for the range to end.

Memory is counted at the size granted. A request declares a total (`memory`), which does not
grow with its size, or a size per slot (`memory_per_slot`: a rule key, `hold --memory-per-slot`,
an acquire field), counted as size × it and held to that when contained; not both. One that
declares neither counts size × `assumed_per_slot`. A per-slot size times MIN over `[ceiling]
memory` is refused, and MAX is cut to the slots whose memory fits it, whether or not the machine
has a memory budget. On a machine with a budget, a range that declares no memory has its MAX cut
the same way, at `assumed_per_slot` a slot. A fixed weight is never cut, so a fixed `--weight 6`
can be granted 6 where a range of 2-6 is granted less. A per-slot lease also records the total
it was granted as its `memory`, which is what leasehold 0.3 reads.

### Elastic leases

A range may also be elastic. `hold --jobserver` with a range makes its lease elastic once the
command's jobs take tokens from the jobserver, and an API client asks for one with
`elastic: true` in [`POST /v1/acquire`](#the-http-api) or makes a held range elastic with
`POST /v1/elastic`. An elastic lease is admitted and first sized exactly as any range is. After
that its size can move between MIN and MAX while it is held. The holder makes two promises: it
takes slots when they are offered, and it gives slots back when asked. Nothing is ever taken
from it. A holder that keeps more than its target keeps those slots, no command is stopped,
and only a `lend` lease is ever revoked, as before.

The daemon works out each elastic lease's `target`, the size it should have now, in every
decision, and every renewal answers with it. The holder moves toward it with
[`POST /v1/resize`](#the-http-api). The target goes:

- up by one slot per decision, up to MAX, while nothing waits in the lease's pool, the pool is
  open and not held to its reservation, memory is neither short nor under pressure, and the
  lease's size has not changed for `[elastic] settle_s` (15 s by default, and at least 1 s;
  giving slots back is never held back). The slot must be free by the same rules an extra slot
  of a range follows, with room kept for any request whose turn came in another pool. By
  default it also leaves the pool's and the machine's `headroom` free, as a range does.
  `[elastic] headroom` changes that: `"keep"` (the default) leaves all of it, `"leave-one"`
  lets a lease grow into the headroom but leaves one slot of it free, and `"use"` lets it grow
  into all of it, since an elastic lease gives those slots back when a request arrives for
  them. Its memory, at its own size per slot or the assumed one, must fit the budget and
  `[ceiling] memory`. When several leases could grow and slots are short, users take turns,
  then each user's sessions, the one with the fewest extra slots first;
- down, when a request whose turn has come does not fit, and lowering the elastic leases on the
  machine to their MIN would let it in. The largest extras are lowered first, one slot at a
  time, until the request fits, and a lease whose slots would not help it keeps them. A
  request that only waits for the memory guard's allowance does not lower anyone;
- down to MIN for every elastic lease while the memory guard sees memory under pressure, and
  down to what one lease could be granted now when that is less than its size (after
  `leasehold pool set` lowers a pool's max, say).

A resize that gives slots back always succeeds, and the daemon admits whatever then fits in the
same transaction. One that takes slots succeeds only up to the target, and only while the slots
are still free: a request that arrived in between, or another elastic lease that took them
first, gets them, and the resize is answered 409. The holder asks again after its next renewal.
A refused resize changes only that lease's target. Every resize is checked in the ledger's
transaction, so processes sharing the ledger never hold more than the pools allow. Status shows
how many slots each lease is asked to give back (`giving_back`), and `top` says so beside it.

`hold` is the reference holder, and it follows a lease only through a jobserver: `--elastic`
without `--jobserver` exits 2, because nothing would make the command's jobs follow the lease.
It starts with the job count of the size granted, so the jobserver holds that many tokens less
one, and it renews at least every `settle_s`. The lease is a plain range until a job takes a
token, which shows the command's jobs follow the jobserver; `hold` then makes it elastic. A
command that never takes one keeps the size it was granted, neither growing nor giving any back.
When the target rises `hold` asks for it, and once granted it puts the new jobs' tokens in the
jobserver and sets the scope's `CPUWeight` (and, with a memory per slot, `MemoryHigh`) for the
new size with `systemctl --user set-property --runtime`. When the target falls, a thread blocked
reading the jobserver takes each token a job gives back, so no running job is stopped. `hold`
gives a slot back only once the tokens of its jobs have come back, however long the jobs take,
so the jobs that can run never outnumber the slots the lease holds. A job killed while it holds
a token keeps that slot held until the command ends. A request waiting first in line says what
it waits for (`waiting for an elastic lease to give back 2 slots as its jobs finish`). A scope
that cannot be resized is reported, and the lease moves anyway. The calls `hold` makes for this
wait no longer than a renewal interval, so they never hold a renewal up.

Only jobserver clients see the size change: make, ninja 1.13 and later, cargo and rustc, and
GCC's LTO take a token before each extra job, so the number of jobs they run follows the
tokens. The job and thread variables (`LEASEHOLD_JOBS`, `OMP_NUM_THREADS` and the others), the
`-jN` in `MAKEFLAGS` and `LEASEHOLD_WEIGHT` keep the values the command started with, because a
running process's environment cannot be changed from outside. Two variables differ for an
elastic lease. `CMAKE_BUILD_PARALLEL_LEVEL` is not set, since `cmake --build` passes it to make
as `-jN`, and `CARGO_BUILD_JOBS` is the job count of MAX, since cargo also takes a token for
each job. A `make -jN` given its own count, on the command line or in a recipe, leaves the
jobserver and runs its own: it runs N jobs whatever the lease holds, and the lease keeps its
size. A bare `make -j` sets no limit at all and also leaves it. `hold` warns when the command
line gives a `-j`, with a number or without one. The Claude Code hooks, the MCP server and the
PATH shims never ask for elastic leases.

For example, a `work` pool of 4 slots with no headroom, where a test run holds 2:

1. `leasehold hold --weight 2-4 --jobserver -- make` is granted 2, the 2 left. make runs 2
   jobs: its own, and one more on the jobserver's one token. Taking that token makes the lease
   elastic.
2. The test run ends. After `settle_s`, the next decision sets the build's target to 3; `hold`
   sees it on its next renewal, resizes to 3, and adds a token, so make runs 3 jobs. After
   another `settle_s` it grows to 4 the same way.
3. Someone asks for 2 slots and waits in line. In that decision the build's target drops to 2.
   `hold` takes the free tokens out of the jobserver, and as two of make's jobs finish and give
   their tokens back, it takes those too. Once make can run only 2 jobs, `hold` gives back 2
   slots, and the request waiting for them is granted in the same transaction.

A daemon from before elastic leases refuses the `elastic` field; `leasehold.client.Client` then
asks again without it, and the lease is a range of the size granted, fixed as before. It has
no `/v1/elastic` or `/v1/resize` either, and `hold` keeps such a lease at its size. A client
from before elastic leases never asks for one, and sees elastic leases' `weight` as their size
now.

### Memory

A config with a `[memory]` table (the one `init` and `setup` write for your own daemon) counts
memory as well as slots. A request starts only when its slots fit and the memory it counts fits
the **budget**: the machine's memory less a share kept back for you.

```toml
[memory]
budget = "auto"            # the machine's memory less `kept`; or a size such as "12g"
kept = "25%"               # with "auto": a share of the memory, or a size such as "4g"
assumed_per_slot = "auto"  # what a request that declares no memory counts, per slot of weight
learn = "shadow"           # "on": count what a rule's past runs used; "shadow": only show it; "off"
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

Your own daemon also looks for a cgroup memory limit, the lowest `memory.max` or `memory.high`
set on its own cgroup or any cgroup above it (cgroup v2 only). A container, a systemd slice or
scope with `MemoryMax` or `MemoryHigh`, or a user slice with a limit sets one. The daemon runs
beside your work, so the same limit holds that work too, and the machine's free memory says
nothing about it. When that limit is below the machine's memory, an `"auto"` budget is the
limit less the same share of it that `kept` is of the machine: with `kept = "25%"` and a 7 GiB
limit on a 16 GiB machine, the budget is 5.25 GiB (12 GiB without the limit), and `assumed_per_slot = "auto"`
divides that among the slots. The limit is read when the daemon starts. A budget given as a
size is left as it is. The machine's daemon (`serve --system`) ignores its own cgroup, since
the work it admits runs in other parts of the tree. `status` reports the limit and the cgroup
that sets it (`memory.limit_bytes`, `memory.limit_cgroup`).

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

A declared size (or, for `memory_per_slot`, the size granted times it, see
[Ranges](#ranges-min-max-slots)) is also what a contained command is held to (`MemoryHigh`, see
[Containment](#containment)), which throttles it past that size but never kills it: the budget
decides what starts, it does not bound what runs. A request that counts more than the whole
budget, or declares more than `[ceiling] memory`, is refused. The budget counts only what
leasehold admits (and `[[standing]]` memory); memory other programs use is the
[guard](#memory-guard)'s to notice.

#### Learned memory

A request that declares no memory can instead count what its rule's past runs used. Since 0.5
a contained command's [outcome](#containment) is kept with the `[commands]` rule that sized it
and the repository it ran in. A request from `hold` (with no `--weight`), a shim or the agent
hook sends its rule and repository too. The daemon works out a **learned figure** a slot of
weight for that user, rule and repository:

- A run's size is its most anonymous memory (`anon_bytes`). Page cache can be reclaimed, so it
  says little about running out of memory, and `memory.peak` includes it. `anon_bytes` is read
  about once a second, so a run shorter than 10 seconds, which a few reads can miss a spike
  in, gives its `peak_bytes` instead. Neither counts memory in swap, so the run's most swap
  (`swap_bytes`) is added: a run that spilled to swap, or that [the watchdog](#the-watchdog)
  paused, is sized by all it held. An outcome from a client that does not report swap adds
  none.
- A run's size a slot is its size over the weight its lease held when it ended. A command
  given more slots runs more jobs, so the figure is counted a slot, as a declared
  `memory_per_slot` is: a lease counts it times the weight it is granted, and an elastic lease
  counts more as it grows.
- Only clean runs count: ones that exited 0 and that the OOM killer did not reach. A failed or
  interrupted run may have stopped before it used what a whole run uses.
- The figure is 115% of the larger of the weighted median of the clean runs, with each run
  weighing half as much as the one 10 runs newer, and the smaller of the newest two. Two big
  runs in a row raise it at once; it comes down only as smaller runs take over the median.
- A run the OOM killer reached holds the figure at 120% of its size a slot, and at least
  256 MiB a slot above it, while its outcome is kept. No figure is under 64 MiB a slot.
- There is no figure until 3 clean runs are known. Until then the request counts its weight
  times `assumed_per_slot`, as before.

`learn` says what the figure does:

- `"on"`: a request with a figure counts it in place of `assumed_per_slot`. A figure whose
  least weight's worth would not fit the budget left beside `[[standing]]` memory is cut so
  that it does, and the command runs alone instead of being refused. `[ceiling] memory` does
  not cut it: that is the most a lease may declare, and a learned figure is no declaration.
- `"shadow"` (the default): the figure is worked out and shown, but the request counts what it
  would with `"off"`. Run in shadow for a while and compare the figures with what is assumed
  (`leasehold leases`, `leasehold outcomes`) before turning it on.
- `"off"`: no figure is worked out for requests.

Declared memory always wins: a request with `--memory`, `--memory-per-slot` or a rule's
`memory` counts what it declares, whatever was learned. A figure is fixed when the request
first asks. A learned figure is never a limit: the command's scope gets no `MemoryHigh` from
it. Since the figure starts from a median, about half of runs use more than the median. The
15% margin covers part of that, and the [memory guard](#memory-guard) stops new work while
memory is short. `leasehold doctor` warns when `learn` is not `"off"` but
`[contain] mode = "off"`, since then nothing is measured.

Without a `[memory]` table no memory is counted, as in leasehold 0.2, and a declared size only
sets `MemoryHigh`. A 0.2 config gains the table with `leasehold setup --upgrade-config` (see
[Upgrading a 0.2 config](#upgrading-a-02-config)).

### Memory guard

Running out of memory is what freezes a machine that runs too much at once. Your own daemon
watches memory pressure (PSI `some avg10`) and available memory. Pressure is the highest of the
machine's (`/proc/pressure/memory`) and that inside `leasehold.slice`, where contained commands
run (unless `[contain] mode = "off"`), and inside the cgroup that sets a memory limit, when one
does: pressure inside a limit can be high while the machine's is low. `status` lists those
cgroups as `guard.pressure_cgroups`. Available memory is
`MemAvailable`, or less under a cgroup memory limit (see [Memory](#memory)): that limit less
what the cgroup that sets it uses now, its inactive file cache left out, read on every tick. The guard goes on when
pressure passes `[guard] memory_pressure_on` (10%) or available memory drops under
`min_available`, and off once pressure has stayed at most `memory_pressure_off` (2.5%), with
memory above the floor, for `settle_s` (30 s). While it is on, what runs keeps running, and
what new work may start depends on whether memory is counted:

- With `[memory]`, nothing new starts while a reading is over a threshold, whatever it
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
- Without it (a 0.2 config), every pool admits only its reserved slots, and the rest waits,
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

Observing only counts a program. It has already started, so its weight is held even past its pool's max or the machine's slots, and `status` marks the machine and the pool
`overcommitted`. New work waits until enough of it ends. A program is seen within one tick (2 s
by default). `leasehold observe` shows what the rules match right now.

Where `/proc` is mounted with `hidepid`, the daemon sees only the processes its user may see,
and process fencing falls back to the pid alone; `leasehold doctor` says so.

### Maintenance

A `maintenance` lease takes the whole machine for a while, so that it can go out of service: a
test host shut down for a benchmark window, a VM stopped for an upgrade. It drains the machine
first, and it tells everyone waiting when the machine will be back.

```sh
leasehold hold --kind maintenance --until 07:00 --reason "benchmark window" \
    --label name=benchmark-window --timeout 30m -- ./run-benchmarks
```

While it waits, nothing new starts on the machine. Requests that arrive after it wait behind
it, in every pool and whoever makes them, the owner's own `work` included. Requests that were
already waiting keep their turns. The leases already running finish in their own time: only
`lend` leases are revoked, as they are for any other waiting work. Observed programs count as
running work. Once no lease is held (standing work aside), the maintenance lease is granted
every slot that standing work does not hold, whatever its pool's `max` or `--weight` say, and
nothing else starts while it is held. Of several maintenance leases, the oldest goes first.

`hold --timeout` (seconds, or `30m`, `2h`) bounds the wait for the machine to empty.
Under an agent, `LEASEHOLD_TOOL_TIMEOUT_S` bounds it too, as for any `hold` (see
[`leasehold run`](#leasehold-run)). Still
waiting at the timeout, `hold` exits 75 and gives back its place, so the queue is as it was.
Otherwise it runs the command with the machine to itself.

`--until` takes `HH:MM` (the next such time in UTC) or an ISO 8601 time, and `--reason` free
text. They become the lease's `until` (RFC 3339, UTC) and `reason` labels, and every request
the lease holds up says so in `blocked`: `machine draining for maintenance until 07:00 UTC
(benchmark window)` while it drains, `machine in maintenance until 07:00 UTC (benchmark
window)` once it holds the machine. The date is added when the end is more than a day off, and
the `name` label stands in for a missing reason. `status` has the same under `maintenance`,
and a queued `acquire` answer carries it as `maintenance`. When `hold` or `acquire` gives up
while a maintenance lease holds or drains the machine, it also prints a line for scripts,
`held by benchmark-window (maintenance) until 2026-10-21T07:00:00Z` (`draining for ...` while
the lease still waits), naming the holder by its `name` label, or else by its user.

The end is advisory, and nothing ends the lease at `until`. Like any lease it lasts as long as
its holder renews it, and `hold` releases it when the command ends. The user who took it, or
root, can end it early with `leasehold release ID`. It shows in the ledger, `leases`, `status`
and `top` with its labels. On a machine whose `[[standing]]` work holds every slot, it is
refused (exit 77): there is nothing for it to take.

From Python, `leasehold.lease(kind="maintenance", labels={"until": "2026-10-21T07:00:00Z",
"reason": "benchmark window"}, timeout=1800)` takes the same lease.

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
test rules at weight 4, with weights that run two at once on 8 CPUs (6 slots) offered as
comments.

The two kept CPUs are kept only from commands that run as many jobs as their lease gives them
(`MAKEFLAGS`, `CARGO_BUILD_JOBS` and the others under [Containment](#containment)); a tool that
ignores the job count can still fill every CPU. `[contain] hard_cpu = true` caps each contained
command at its weight in CPUs; it is off by default, and `leasehold doctor` warns while it is.

On 4 to 7 slots, where the built-in weight 4 runs one build or test run at a time, the config
`init` writes offers two at once, each with fewer jobs, as two commented lines:
`[commands] heavy_weight = 3` and `light_weight = 2` on 6 slots (half the slots, and half that
again, rounded up). Uncomment them when the heavy commands declare memory (a rule's `memory`, or
`hold --memory`), or when two of them fit in memory together. A command that declares no memory
is counted at its weight times the budget over the slots, so two of them always seem to fit,
whatever they use. When two that each needed more than half the memory ran together, the OOM
killer ended 25 jobs in 6 rounds ([memtight](../bench/results/8cpu-16g/memtight/README.md));
`leasehold doctor` warns when a config sets these weights and the heavy rules declare no
memory. 0.4.0's `init` set them uncommented, and a config it wrote keeps them until you remove
them. [Measured](bench.md#two-heavy-jobs-at-once).

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

Clients find a daemon from `--socket`, then the `socket` of the config `--config` names, then
`LEASEHOLD_SOCKET`, then the `socket` of the config `LEASEHOLD_CONFIG` names, so a flag always
outranks the environment. Told none, they try the system socket, then their own, and use the
first that answers. `leasehold doctor` lists each candidate and why it was taken or passed
over. `hold` sets `LEASEHOLD_SOCKET` for the command it runs to the socket it found this way,
made absolute, so a nested `hold` and the PATH shims reach the same daemon, even when `hold`
found it through `--config` or the search. `serve` listens on `--socket`, then
`LEASEHOLD_SOCKET`, then the config's `socket`, then its mode's own.

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
| `[pools.<name>]` | the default split | `reserved`, `max`, `priority`, `queue_timeout_s`, `headroom` (`"auto"`, see [Ranges](#ranges-min-max-slots)), and per-user limits: `per_user_max_running`, `per_user_max_queued`, `create_rate_per_min` |
| `[kinds]` | each kind to its namesake; `maintenance` to the `work` kind's pool | the pool each kind goes to |
| `[lend]` | see [Lending](#lending) | `pools`, `quiet_s` (600), `cpu_pressure_max` (10), `memory_pressure_max` (5), `load_per_cpu_max` (0.25), `window` (as `"22:00-07:00"`), `revoke_grace_s` (30), `pressure_cgroups` |
| `[load] slices` | none | the cgroups (systemd slices) whose CPU and memory `status` reports |
| `[jobs] per_slot` | `1` | jobs per slot, for [sizing](#sizing-the-command-to-its-lease) |
| `[[standing]]` | none | long-lived work outside leasehold that permanently holds capacity: `name`, `pool`, `weight` (required), `kind` (`"workload"`), `cpus`, `memory`, `note` |
| `[[observe]]` | none | see [Observed programs](#observed-programs): `name`, `match` (`comm`, `exe`, `cmdline`, `user`), `weight`, `pool` (`observed`), `max_count` |
| `[commands]` | the built-in rules | see [`[commands]`](#which-commands-take-a-lease-commands): `defaults`, `[[commands.rule]]`, `heavy_weight` (4), `light_weight` (2), `wait_s`, `when_busy` |
| `[memory]` | none: no memory counted | see [Memory](#memory): `budget` (`"auto"`), `kept` (`"25%"`), `assumed_per_slot` (`"auto"`), `learn` (`"shadow"`) |
| `[guard]` | on for your own daemon | see [Memory guard](#memory-guard): `enabled`, `memory_pressure_on` (10), `memory_pressure_off` (2.5), `min_available` (5% of memory, at least 512 MiB), `settle_s` (30) |
| `[elastic] settle_s` | `15` | how long an [elastic lease](#elastic-leases)'s size stays put after it was granted or last changed before it may grow again; at least 1 |
| `[elastic] headroom` | `"keep"` | how much of the pools' and the machine's headroom an [elastic lease](#elastic-leases) leaves free as it grows: `"keep"` all of it, `"leave-one"` one slot, `"use"` none |
| `[watchdog] agents` | `["claude", "codex", "gemini", "cursor-agent", "aider"]` | the program names your own daemon [watches as sessions](#watched-sessions) without being asked; `[]` for none |
| `[watchdog]` | `mode = "watch"` (`init` writes `"on"`) | see [The watchdog](#the-watchdog): `mode` (`"off"`, `"watch"` or `"on"`), `act_at` (`"critical"`), `critical_below` (`"2g"`), `hold_below` (`"3g"`), `min_size` (`"auto"`), `max_pause_s` (900), `mapped_window_s` (300), `io_stall_critical` (30), `choose_discovered` (false), `protect` (`[]`). A `[memory_net]` table is a config error |
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

Without `--weight`, `hold` weighs the command as the [`[commands]`
rules](#which-commands-take-a-lease-commands) weigh it (cargo test: `heavy_weight` slots, cut to
what its pool may hold; a rule's range as a range), and anything else (no rule, or a case of
no lease) as 1 slot. A training script or other single process that runs many threads weighs 1
slot unless a rule names it, so give it `--weight N` for the N slots it uses. `--weight 2-4` asks for a
[range](#ranges-min-max-slots) and runs the command sized to the weight granted. With
`--jobserver` the range is [elastic](#elastic-leases) unless `--no-elastic` is given: once the
command's jobs take jobserver tokens, its jobserver's tokens and its scope follow the lease as
it grows and shrinks. `--elastic` needs `--jobserver`, and exits 2 without it. `--memory 6g`
declares the memory it will use (default: its rule's `memory`), counted against the [memory
budget](#memory) and held to when contained; `--memory-per-slot 2g` declares it per slot granted
instead (default: its rule's `memory_per_slot`). `--threads N` sets the parallelism inside each
of its processes (default: its rule's `threads`, else the job count; see
[sizing](#sizing-the-command-to-its-lease)). A PATH shim inside the command may still lower it.
`hold` reads the rules when no `--weight` is given, or for the threads when the job variables
are on; with `--weight` and `--no-job-env` it does not ask the daemon for them. `--timeout`
takes seconds, or minutes and hours as `30m` and `2h`. `--kind maintenance` takes the
[whole machine](#maintenance), with `--until` and `--reason` saying when it is back and why.

`hold` waits for a lease, runs the command in its own process group, renews while it runs
(every TTL/3, or every `[elastic] settle_s` for an elastic lease when that is sooner, each
renewal waiting at most that long for an answer), forwards SIGTERM, SIGINT and
SIGHUP to the group, releases when the command ends, and exits with its code. The lease covers
the group: anything left in it when the command ends is stopped too. The command sees
`LEASEHOLD_LEASE_ID`, `LEASEHOLD_GRANT`, `LEASEHOLD_POOL` and `LEASEHOLD_WEIGHT` (the weight
granted), and does not see `LEASEHOLD_WANT`, so the commands it runs ask for their own size.

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

When memory ended the command, the last line `hold` writes on stderr is the memory verdict.
It starts with `leasehold:`, says the command was killed for memory and that this is not a test
failure, and says what to do next. `hold` still exits with the command's own code (137 for
SIGKILL), so a script reads the reason from that line and the code from the exit status. The
PATH shims print the same line.

```
leasehold: the kernel's OOM killer ended pytest (peak 13.9 GiB) when the machine ran out of memory. This is not a test failure. Run it again only after the other heavy work has finished, with fewer workers.
```

When the command's own [hard cap](#a-hard-cap-memory_max) ended it, the verdict names the cap,
and the largest of its processes when leasehold saw it near the cap:

```
leasehold: pytest was stopped by leasehold's cap of 8.0 GiB for this command: its largest process, python (pid 41233), had 7.6 GiB. This is not a test failure. Do not run it again unchanged: run fewer workers or a narrower selection, find what allocates, or raise its memory_max.
```

The verdict is certain when the command's scope counted the OOM kill, or systemd says the OOM
killer ended its capped scope. It names the command's own cap when that was the tightest limit
on it, else the `memory.max` above the command when one was set (a session slice, say), in
place of the machine. A command that ended by SIGKILL while the machine counted an OOM kill gets a verdict
that says "most likely", since the kill counted may have been another process's. This works
contained or not, on Linux only for now: other systems get no verdict.

For a contained command the OOM killer reached, other than at its own cap, the line before
the verdict gives the command's peak, the memory leasehold counted for it, and a `--memory`
about 15% above the peak.
That peak is at most what the command was let use before the kill, so declare at least that
much.

### Containment

Where it can, `hold` (and so the PATH shims) starts the command in a systemd user scope sized to
the lease, so the lease holds the command back instead of only counting it. The Claude Code
plugin moves a leased command's shell into the same kind of scope as the command starts
([Containment on the hook path](#containment-on-the-hook-path)):

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
  command outright, so the machine can still run out of memory.
  The same size is what [memory admission](#memory) counts before the command starts, and the
  [memory guard](#memory-guard) is what stops new work starting while memory is short.
- `hold --memory-max 8g`, or a rule's `memory_max`, sets a [hard cap](#a-hard-cap-memory_max).
  It is off unless asked for.

The scope is `lease-<id>.scope` in `leasehold.slice`, or in the slice of the `leasehold run`
session it was started from. Every process the command starts stays in it. If the user manager
refuses the scope or does not answer within 30 s, the command has not run yet; it then runs
uncontained, and `hold` says so.

A command can leave a process running in its scope after it ends: a build server such as
Bazel's or Gradle's, started in a session of its own. Once the command ends, `hold` takes the
lease's limits off the scope (its weight goes back to one slot's, and its `MemoryHigh`,
`MemoryMax` and `CPUQuota` go), so the server does not keep a cap meant for one command, and
says so:

```
leasehold hold: bazel left java running (pid 81234, 532 MiB) in its scope; it no longer has the lease's limits
```

It is used when the daemon's `[contain] mode` is `auto` (the default) and the caller runs in your
own session (anything under `/user.slice/user-<uid>.slice/`) with a systemd user manager that
answers and is delegated the cpu and memory controllers, the default on current Debian, Ubuntu
and Fedora.
A command started from a system service, a CI runner's job or an operator's slice is left where
it is, under that service's limits and clean-up. So is a command whose caller runs under a
`memory.max` that its scope would not be under: Claude Code's `CLAUDE_CODE_TOOL_MEMORY_LIMIT`
group, or the scope of a capped command that runs `hold` itself. Moving it would take it out
from under that limit. It keeps its lease, and its memory verdict still names that limit
(`leasehold doctor` says "Claude Code's tool memory limit (4G) is on: leasehold leaves
commands in its cgroup and reports its kills"). Elsewhere, or with `LEASEHOLD_CONTAIN=off`, the
command runs as before and the lease is advisory. `leasehold doctor` says which applies.

While a contained command runs, its client reads the scope's cgroup files about once a second
and keeps the last values, since the scope is removed when the command ends. When the lease is
released, the client sends what it read with the release, and the daemon keeps it in the ledger
with the lease's size, range and declared memory. `leasehold outcomes` lists it. A record has:

- `peak_bytes`: `memory.peak`, the most memory the scope used, page cache included. It is null
  on kernels before 5.19, which have no such file, and the last read can miss what the final
  second added.
- `anon_bytes`: the most `anon` seen in `memory.stat`. Page cache is not counted here, so it is
  a lower bound on what the command needed.
- `swap_bytes`: `memory.swap.peak`, the most swap the scope used, or on kernels before 6.5 the
  most `memory.swap.current` seen. Neither of the two above counts memory in swap.
- `oom_kills` and `high_events`: `oom_kill` and `high` from `memory.events`.
- `cpu_usec`: `usage_usec` from `cpu.stat`. `wall_s` is how long the command ran and
  `exit_status` how it ended (negative: the signal that killed it).
- `rule`: the [`[commands]` rule](#which-commands-take-a-lease-commands) that sized the command,
  when `hold` was given no `--weight`, or for a command run through a [shim](#path-shims). It is the rule's programs, sorted and joined by `, `, then
  ` ~ ` and its `args` when it has them (`make ~ -j`). The built-in pytest-xdist rule is
  `py.test, pytest ~ xdist`.
- `repo`: the top of the git work tree the command ran in, or null.
- `pauses` and `paused_s`: how often [the watchdog](#the-watchdog) paused the command and
  for how long in all, recorded by the daemon; null where it did not say.

An uncontained command records nothing. A lease from `leasehold.lease()` records its last
contained command's outcome, with no rule. The ledger keeps the newest 50 outcomes for each
user, rule and repository, none older than 30 days, and keeps them when it is rebuilt for a new
schema. With `[memory] learn = "on"`, a request that declares no memory counts what its rule's
past runs in its repository used (see [Learned memory](#learned-memory)).

#### A hard cap: `memory_max`

A hard cap is opt-in. With one, a runaway command is stopped at its cap instead of taking the
machine's memory:

- `hold --memory-max 8g` (a size, a share of the machine's memory such as `50%`, or `off`);
- a `[[commands.rule]]`'s `memory_max`, for the commands it matches, through `hold` and the
  [PATH shims](#path-shims);
- `[commands] memory_max`, for every leased command whose rule sets none (default `off`);
- `lease.run(argv, memory_max="8g")` from [Python](#running-a-command-under-the-lease).

The first of these that is given wins, and `off` at any of them sets none. On Linux the
command's scope gets `MemoryMax` at the cap and `MemorySwapMax=0`, so a runaway cannot push
its memory out to swap on the way, and its `memory.oom.group` is set, so the kernel kills
every process of the command at once, not one worker at a time. The command keeps its own
exit code (137), and the [memory verdict](#hold) names the cap. A cap that is set too low
kills work that would have finished, which is why none is set by default.

A cap needs the command's scope. Where `hold` cannot make one (no systemd user manager,
`[contain] mode = "off"`, no daemon, or a caller under someone else's memory limit, as above),
it says so before the command runs, and runs it uncapped:

```
leasehold hold: no memory cap of 8.0 GiB on pytest: the daemon has [contain] mode = "off"
```

macOS has no per-command memory limit that a user can set without root. There a cap will be
polled: leasehold will read the command's memory and kill it past the cap. Until that is
built, a cap asked for on macOS is not enforced, and `hold` says so. `leasehold doctor`'s
`memory_max` line says how a cap works on this machine (`exact`, `polled` or none, and why),
and warns when the config sets a cap that cannot hold. The status object says the same under
`contain.memory_cap`.

### Sizing the command to its lease

A lease of weight W (for a [range](#ranges-min-max-slots), the weight granted)
allows W × `[jobs] per_slot` jobs. `hold` sets two groups of variables.
`--no-job-env` sets none of them. It also removes an enclosing lease's sizing: every
variable that lease set and that still has its value, along with `LEASEHOLD_SET`,
`LEASEHOLD_JOBS` and `LEASEHOLD_THREADS`, so each tool falls back to its own default.

The job count goes to the variables that say how many jobs or worker processes a tool fans
out to:

| Variable | Read by |
|---|---|
| `LEASEHOLD_JOBS` | anything: the job count itself |
| `CARGO_BUILD_JOBS` | cargo build, test and check |
| `CMAKE_BUILD_PARALLEL_LEVEL` | `cmake --build` |
| `PYTEST_XDIST_AUTO_NUM_WORKERS` | `pytest -n auto` (pytest-xdist) |

The thread count goes to the variables that limit the parallelism inside each process:

| Variable | Read by |
|---|---|
| `LEASEHOLD_THREADS` | anything: the thread count leasehold chose |
| `OMP_NUM_THREADS` | OpenMP: numpy and other BLAS users, PyTorch, compilers with `-fopenmp` |
| `OPENBLAS_NUM_THREADS` | OpenBLAS (numpy's and scipy's wheels), which reads it before `OMP_NUM_THREADS` |
| `MKL_NUM_THREADS` | Intel MKL; PyTorch reads `OMP_NUM_THREADS`, then `MKL_NUM_THREADS` overrides it |
| `RUST_TEST_THREADS` | test binaries built by cargo (`cargo test` runs them one at a time) |
| `GOMAXPROCS` | Go programs, `go build` and `go test` included |
| `POLARS_MAX_THREADS` | Polars |
| `RAYON_NUM_THREADS` | Rust programs using rayon |
| `NUMBA_NUM_THREADS` | Numba, as a ceiling: `numba.set_num_threads()` above it is an error, which keeps the lease's limit |
| `LOKY_MAX_CPU_COUNT` | joblib and loky process pools: scikit-learn's `n_jobs=-1` |
| `DASK_NUM_WORKERS` | Dask's threaded and multiprocessing schedulers (the `num_workers` setting) |
| `PYTHON_CPU_COUNT` | Python 3.13 and later: `os.cpu_count()` and `os.process_cpu_count()`, and so `multiprocessing`, `ProcessPoolExecutor` and Dask's default worker count |

With `PYTHON_CPU_COUNT` at the same count, `os.cpu_count()` agrees with Numba's ceiling.

The thread count is the job count unless the command's `threads` says otherwise: `hold
--threads N` or `--threads jobs`, else what the [`[commands]` rules](#which-commands-take-a-lease-commands)
give the command line. Each program on the line takes its own rule's `threads`, and the line
gets the smallest of them, so no program on it runs more threads per process than its rule
allows. Programs on the line that no rule names get the line's count too, and a pytest run
that starts workers allows its share even with no rule of its own. A pytest run with 4
workers gives the line a quarter of the job count whatever else runs on it:
`hold -- sh -c "cargo test && pytest -n 4"` and `pytest -n 4; pytest -n 2` both get the job
count divided by 4, and `cargo test && pytest -n auto` gets 1. A rule's count lowers a share
too: a program whose rule gives 3 beside `pytest -n 2` makes the line's count the smaller of 3
and half the job count. A count above the job count is cut to it. When every rule on the line
has `threads = "jobs"` and no pytest run on it starts workers, every variable gets the job
count, as in 0.3.0. `leasehold commands --test LINE` shows the count a line gets.

A variable already set when `hold` starts is yours, and `hold` leaves it alone unless the
lease the command runs in set it to the value it still has. `hold` records each name it set,
with its value, in `LEASEHOLD_SET` (`OMP_NUM_THREADS=4,GOMAXPROCS=4`). The record counts only
when the environment `hold` starts in has a `LEASEHOLD_LEASE_ID`, and only for the variables
in the tables above. A name you changed since is yours again, whether you exported it inside
the lease (`export OMP_NUM_THREADS=3`) or set it for one command
(`OMP_NUM_THREADS=3 leasehold hold ...`). A name you unset is set again. A value you set equal
to the lease's cannot be told from it. A lease started by leasehold 0.3.0 records nothing, so
a `hold` inside it takes its values for yours.

A `hold` inside a lease takes a lease of its own, and sets the recorded names to that lease's
counts, larger or smaller than the enclosing lease's. A PATH shim inside a lease takes no lease
of its own. It only lowers the thread variables the lease set, each to the smaller of its
current value and the count the shim's rule gives, so a program run through a shim never gets
more threads than the lease around it. That holds for a count given with `hold --threads N`
too. The exception is `OPENBLAS_NUM_THREADS` and `MKL_NUM_THREADS`, which follow your own
`OMP_NUM_THREADS` even when it is higher (below).

When you set `OMP_NUM_THREADS` to a plain count, `hold` gives `OPENBLAS_NUM_THREADS` and
`MKL_NUM_THREADS` the same value unless you set them too. OpenBLAS reads its own variable
before `OMP_NUM_THREADS`, and PyTorch lets `MKL_NUM_THREADS` override it, so a different value
there would undo yours. Your `OMP_NUM_THREADS` wins for the OpenMP and BLAS variables: a
nested `hold` and a shim lowering the lease's count leave it, and the two copied from it, at
your value. Your own
`OPENBLAS_NUM_THREADS` or `MKL_NUM_THREADS` is left alone and copied nowhere. When your
`OMP_NUM_THREADS` is not a plain count (`4,2`), it is not copied, and an enclosing lease's count
in the other two stays. `LEASEHOLD_JOBS` and `LEASEHOLD_THREADS` are always the counts leasehold
chose for this lease. A count of more than 6 digits is never read as a count.

A command whose jobs are worker processes would, with the job count in both groups, let each
of N workers start an N-thread BLAS or OpenMP pool: N × N threads on N jobs' worth of CPU. A
pytest run that starts xdist workers shares the job count among them instead, whichever
rule it matched. With `-n N`, each worker gets the job count divided by N, and at least 1, so
`-n 1` on 8 jobs gets 8 threads and `-n 4` gets 2. With `-n auto` or `-n logical`, xdist starts
as many workers as `PYTEST_XDIST_AUTO_NUM_WORKERS`, the job count, so each gets 1. A rule's
own `threads` can only lower that share.

leasehold reads the config file's `addopts`, then `PYTEST_ADDOPTS`, then the command line, as
pytest does, and decides as pytest-xdist 3 does:

- Workers start when the last `-n` or `--numprocesses` is above 0, `auto` or `logical`.
  `--maxprocesses M` limits a count to M, and `auto` to the smaller of the job count and M,
  so each worker gets the job count divided by M, and at least 1. `-nN`, `-n=N`,
  `--numprocesses=N`, `-vn 4`, `-vn=4`, `'-n 4'` and `-n ' +4'` all count, as xdist reads
  counts with `int`.
- `--tx` starts workers only with `--dist` (other than `no`) or `-d`. `4*popen` counts as 4.
  `-n 0` clears `--tx`.
- `--collect-only` (`--co`) starts none. `--pdb` with `-n auto` runs serially, and `--pdb`
  with any other workers is an error, so neither counts. `--setup-plan` and `--setup-only`
  still start the workers.
- The last of `-p no:xdist`, `-p xdist` and `-p xdist.plugin` decides whether xdist is
  loaded at all.

Arguments are read one word at a time, the way pytest's argument parser reads them. An option
that takes a value takes the next word unless that word looks like an option, and `--cov`
takes it only then, so `--cov -n 4` starts workers. An option that needs a value and gets
none is an error pytest stops on, so `-k '-n 4' -n 2` and `--maxfail -n 2` start none. Nothing
after `--` counts. A word `@FILE` stands for the lines of FILE, one argument each, when FILE is
a regular file of at most 1 MiB. Files may name further files. leasehold reads them up to
10,000 words in all and 8 files deep, and the arguments after them always; when a limit leaves
some unread, the run counts as `-n auto`. A file that names itself, directly or through
another, is an error pytest stops on, so it starts none. A count of more than 6 digits is not
read.

The config file is located the way pytest 9 does it. The last `-c` or `--config-file`, from
`PYTEST_ADDOPTS` or the command line, names it. Otherwise the search starts at the common
directory of the path arguments that exist, or at the directory the command runs in, and goes
upwards without resolving symbolic links. In each directory it takes the first of
`pytest.toml`, `.pytest.toml`, `pytest.ini`, `.pytest.ini`, `pyproject.toml` with
`[tool.pytest]` or `[tool.pytest.ini_options]`, `tox.ini` with `[pytest]`, and `setup.cfg`
with `[tool:pytest]`. A `pytest.toml` or `pytest.ini` stops the search even when empty.
`-o addopts=...` or `--override-ini addopts=...` replaces the file's `addopts`. A file pytest
would refuse, or that cannot be read, adds nothing.

The directory the command runs in is `hold`'s own, or the one `uv --directory DIR` names.
leasehold does not follow a `cd` inside the command, so `hold -- sh -c 'cd sub && pytest'`
reads the config found from `hold`'s directory.

leasehold's search differs from pytest's in a few other cases:

- ini files are parsed with Python's `configparser` and pytest uses `iniconfig`, so unusual
  syntax can read differently.
- A file over 1 MiB, or one that is not a regular file, counts as no config. pytest reads it.
- A value-taking option leasehold does not know, from a plugin, is read as taking no value. If
  its value names an existing path, that path joins the search as a path argument would.
- Configuration that plugins add while pytest starts is not seen.

A serial `pytest`, tox, nox and every other built-in rule have the job count.

This covers a pytest run started by `hold` itself, by a PATH shim, and by a PATH shim inside
another lease. Under `hold -- make test`, the pytest shim takes no second lease and shares the
lease's job count among the workers of `pytest -n 4`. It waits at most 0.3 s for the daemon's
rules. If working out the thread count fails, the program runs with its environment as it
was. Some runs are not covered:

- `python -m pytest`, or a virtualenv's own `pytest`, run by a script or makefile inside a
  lease passes through no shim. Only `hold` itself (`hold -- python -m pytest -n 4`) and a shim
  see it.
- A command leased by the [Claude Code plugin](#the-claude-code-plugin), which never changes
  a command's environment.
- tox 4 passes none of these variables into its environments by default, and a tox or nox
  environment's `pytest` comes before the shim on PATH. Give the environment the counts with
  `pass_env` (`pass_env = OMP_NUM_THREADS, OPENBLAS_NUM_THREADS, MKL_NUM_THREADS,
  PYTEST_XDIST_AUTO_NUM_WORKERS`), or set them in one that runs `pytest -n` with `set_env =
  OMP_NUM_THREADS=1`.

`hold --jobserver` also hands the command a GNU make jobserver: a named pipe holding one token
fewer than the job count, announced in `MAKEFLAGS` as `--jobserver-auth=fifo:PATH` (the protocol
of GNU make 4.4). make, ninja 1.13 and later, cargo and rustc, and GCC's LTO take a token before
each extra job, so a whole tree of builds, sub-makes included, runs at most that many jobs at
once. The command must not pass its own `-jN`, on its command line or in a recipe: make then
runs its own jobserver instead, and `hold` warns when the command line gives one. A tool
that does not speak the protocol, or make before 4.4, still has the variables. steve and
guildmaster run one jobserver for a whole machine; here it is one per lease. Under an
[elastic lease](#elastic-leases) the jobserver's tokens follow the lease's size, while the
variables keep the values the command started with.

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
| 75 | still waiting at `--timeout`: busy, not failed. The place in line is given back. While a [maintenance lease](#maintenance) holds or drains the machine, a line `held by NAME (maintenance) until TIME` (or `draining for ...`) follows |
| 76 | the lease was revoked (or, for `lend`, lost): the group got SIGTERM, then SIGKILL after `--grace` (30 s) |
| 77 | refused: the request could never be granted (heavier than its pool or the ceiling, or a pool that does not exist), or the daemon answered it with an error (such as 500 `internal` or 503 `ledger_unavailable`). The command does not run |
| 69 | no daemon answered (`hold` only with `--if-unavailable fail`) |
| 127 | the command could not be started |

### A lease across processes

`hold` ties a lease to one command. When the process that asks for a lease ends before the work
does, and a later process gives it back (a CI runner's job-started and job-completed hooks),
use `acquire` and `release` instead:

- `acquire --lease-id ID` waits in line as `hold` does, with the same exit codes (75, 77, 69),
  prints the granted lease as one JSON line (its `weight` the size granted, with `min_weight`
  and `max_weight`; `--weight 2-4` asks a range) and exits 0. The caller picks the id, so the
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

### From Python

A Python program can take a lease itself instead of running `leasehold hold`. The API is
synchronous, installs no signal handlers, and is provisional until 1.0: its names are those in
`leasehold.__all__`, and the [CHANGELOG](../CHANGELOG.md) lists every change to them.

```python
import leasehold

with leasehold.lease(4, kind="external", labels={"job": "nightly"}, timeout=600) as lease:
    print(lease.weight, lease.lease_id)
    run_the_work(jobs=lease.jobs)
```

`leasehold.lease()` waits in line, then returns a `Lease` that a thread of its own renews
(every TTL/3, as `hold` does) until it is released. Leaving the `with` block releases it,
whether the block ends normally or by an exception; `lease.release()` does the same outside a
`with`. `weight` is a number of slots, or `(least, most)` for a [range](#ranges-min-max-slots),
in which case `lease.weight` is what was granted and may change as the lease renews.
`kind`, `pool`, `labels`, `session`, `memory`, `memory_per_slot` and `ttl` are `hold`'s
options of the same names. The daemon is found as the command line finds it (`find_daemon()`
says which socket that is); `socket=` names one.

How a wait ends:

| Outcome | Raised | The place in line |
|---|---|---|
| granted | (returns the `Lease`) | |
| still waiting at `timeout` | `Busy`, with `position`, `blocked` and `waited_s` | given back |
| `cancel` (a `threading.Event`) is set | `Cancelled` | given back |
| refused, or a 4xx answer | `Refused`, with `reason`, `code` and `status` | none was held |
| the daemon answered 5xx | `DaemonError` | given back |
| no daemon, with `if_unavailable="fail"` | `Unavailable` | none was held |

All of these subclass `LeaseError`. With no daemon and the default `if_unavailable="run"`, the
`Lease` is unleased: `lease.held` is false and the work runs as it would without leasehold,
which is what `hold` does. To fail open on every error instead, catch `LeaseError` and run
without a lease. `on_wait` is called with a `Waiting` (`position`, `blocked`, `waited_s`) as
the wait starts, whenever its place or what blocks it changes, and every 20 s.

```python
import threading

stopping = threading.Event()  # set from elsewhere to give up the wait
try:
    lease = leasehold.lease(
        (2, 4), timeout=300, cancel=stopping, on_wait=lambda w: print("waiting, place", w.position)
    )
except leasehold.Busy:
    ...  # try again later
except leasehold.Cancelled:
    ...  # stopping was set
else:
    with lease:
        ...
```

A revoked `lend` lease, or a `lend` lease whose renewals go unanswered past its TTL, sets
`lease.lost` to the reason and calls `on_lost(reason)` on the renewal thread. `lease.check()`
raises `LeaseLost` once that has happened, for code that would rather check between steps. A
`work` or `external` lease is never revoked; one the daemon forgot (say, after its ledger was
reset) is asked for again with the same id, and `lost` is set only if that is refused.

#### Agent sessions

`leasehold.session()` makes the calling process one agent session's agent for a `with` block,
so that the daemon [watches](#watched-sessions) what it starts. It sets `LEASEHOLD_SESSION`
in `os.environ` (or in the `env` mapping you pass) for the processes it starts, and puts it
back as it was when the block ends.

```python
with leasehold.session("nightly-agents") as s:
    run_the_agents()  # their processes are counted as the session's
print(s.id, s.registered, s.reason)
```

`name` is the session's id when it is a valid one (letters, digits and `_.:-`, at most 64),
else the start of one with a tag added. `agent` says what to call the agent. `socket=` or
`client=` names the daemon. Watching never gets in the way: with no daemon, or one from before
sessions, the block runs all the same, `s.registered` is false and `s.reason` says why.

#### Running a command under the lease

`lease.run(argv)` runs a command as `hold` would and returns its exit status; `lease.popen(argv)`
starts it and returns a `Command`, whose `wait()` gives the status, and which is a context
manager that stops the command if it still runs on leaving. The command:

- leads a process group of its own and gets the lease's environment: the `LEASEHOLD_*`
  variables and, unless `job_env=False`, the [job and thread counts](#sizing-the-command-to-its-lease)
  for the weight granted (`threads` sets the thread count as `--threads` does);
- runs in the lease's systemd scope where the daemon and this process allow one, as under
  `hold` ([containment](#containment)); `contain=False` runs it as is;
- with `memory_max` (a size such as `"8g"`, a share such as `"50%"`, or `"off"`; default the
  daemon's `[commands] memory_max`), is killed whole past that [cap](#a-hard-cap-memory_max).
  `Command.uncapped` says why a cap asked for does not hold here, and is None when it holds;
- with `jobserver=True`, gets a make jobserver, and a range is [elastic](#elastic-leases)
  unless `elastic=False`;
- is stopped with its group, SIGTERM then SIGKILL after `grace` seconds (30), when a `lend`
  lease is lost, whether or not anything waits for it; `Command.stopped_for` says why.

Once a contained command ends, `Command.oom_killed` says whether the kernel's OOM killer ended
any of its processes, and `Command.peak_bytes` is the most memory its scope used (None when
unknown). After `lease.run(argv)`, `lease.oom_killed` says the same of the command it ran. An
uncontained command is never `oom_killed`.

Once any command ends, `Command.memory` is the memory verdict on it, and `lease.memory` the
one on the last command the lease ran: None when memory did not end it, as far as leasehold can
tell, else an object with `kind` (`cap` for the command's own cap, `kernel`, or `agent_cap`
for Claude Code's own memory limit), `certain`, `program`, `peak_bytes`, `limit_bytes` (the
cap, for `cap`), `largest` (the largest of its processes as `(pid, name, bytes)`, when known),
`person` (what happened) and `agent` (that, and what to do next). Both texts are the line `hold` prints, and say "not a test
failure".

Whatever the command leaves in its group is stopped when it ends. Other keyword arguments go to
`subprocess.Popen`. A command that cannot be started raises `NotStarted`.

For work that is not a separate command, or a command started some other way, `lease.env()`
gives the variables alone: `subprocess.run(argv, env=lease.env())` sizes the command to the
lease but leaves its process group, containment and revocation to the caller.

#### Testing code that takes leases

`client=` replaces the daemon with any object that has `acquire`, `renew` and `release`, so tests
need no daemon:

```python
class FakeDaemon:
    def __init__(self, status="granted"):
        self.status, self.released = status, []

    def acquire(self, *, lease_id, kind, weight, wait_s=0.0, **_):
        if self.status != "granted":
            return {"status": self.status, "position": 1, "blocked": None, "reason": "test"}
        lease = {
            "lease_id": lease_id,
            "kind": kind,
            "pool": "default",
            "weight": weight,
            "grant": 1,
            "ttl_s": 60,
        }
        return {"status": "granted", "lease": lease}

    def renew(self, lease_id):
        return {"status": "ok"}

    def release(self, lease_id):
        self.released.append(lease_id)


daemon = FakeDaemon()
with leasehold.lease(2, client=daemon) as lease:
    assert lease.weight == 2
assert daemon.released == [lease.lease_id]

try:
    leasehold.lease(2, timeout=0, client=FakeDaemon("queued"))
except leasehold.Busy:
    pass
```

## GitHub Actions runner hooks

`deploy/github-runner/` makes a self-hosted runner take an `external` lease per job:

- `job-started.sh` (the runner's `ACTIONS_RUNNER_HOOK_JOB_STARTED`) runs `acquire --keep` with
  the id `gh.<runner>.<run id>.<attempt>.<job>`, weight `LEASEHOLD_RUNNER_WEIGHT` (a number or a range `MIN-MAX`; `1-4`) and the
  labels `consumer=github-runner`, `runner`, `repository`, `run` and `job`. A runner hook cannot
  hand a job back (it runs after the job is assigned, with no timeout), so the hook *is* the
  queue: while the machine is busy the job waits in line here, before its first step, until
  capacity frees or the job's own `timeout-minutes` ends it. The renewer watches the job's
  `Runner.Worker` (an ancestor of the hook) and stops after `LEASEHOLD_RUNNER_MAX_HOLD` (21600 s)
  in any case.
- `job-completed.sh` (`ACTIONS_RUNNER_HOOK_JOB_COMPLETED`) releases it, or says the job held no
  lease when the daemon lists none for it.
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
the agent uncontained. When a daemon answers, `run` registers the session with it, so that the
daemon [watches](#watched-sessions) what the agent runs.

An agent runs each shell command with a timeout of its own, and a shim inside the command waits
in line on that clock. `run` puts the agent's timeout in `LEASEHOLD_TOOL_TIMEOUT_S`, and a shim
or `hold` under it waits in line at most half of that, then says why the command did not run
and exits 75, before the agent's timeout ends the call. `--tool-timeout S` gives the timeout
(`0`: none). Without it, `run` keeps a `LEASEHOLD_TOOL_TIMEOUT_S` already set, and for Claude
Code (`claude`) uses its Bash tool's default: `BASH_DEFAULT_TIMEOUT_MS` when set, else 120 s.
For any other agent, give `--tool-timeout` (or set the variable) to what its shell tool allows.
A call that sets its own, longer timeout still gets only half the default.

### Which commands take a lease: `[commands]`

The Claude Code plugin and the PATH shims decide from the same rules whether a command is heavy
and what it weighs. A rule names programs by basename and, optionally, a regular expression
their arguments must contain:

```toml
[commands]
wait_s = 300           # how long an agent's hook or a shim waits in line (at most 540)
when_busy = "deny"     # then: "deny" the command, or "run" it without a lease and say so
# heavy_weight = 3     # the built-in build and test rules' weight (default 4)
# light_weight = 2     # the built-in install and bundler rules' weight (default 2)
# memory_max = "50%"   # a hard cap on every leased command whose rule sets none (default "off")

[[commands.rule]]
programs = ["pytest"]
args = "--benchmark"   # only benchmark runs
weight = 6
memory = "8g"          # counted against [memory]; held to this when contained (MemoryHigh)
memory_max = "12g"     # killed whole past this when contained (MemoryMax); "off" for none
threads = 1            # parallelism inside each process (OMP_NUM_THREADS ...); default "jobs"

[[commands.rule]]
programs = ["make"]
weight = 2
pool = "external"

[[commands.rule]]
programs = ["ninja"]
weight = "2-6"           # a range: needs 2, can use 6, granted a size between when admitted
memory_per_slot = "1g"   # instead of memory: counted, and held to, per slot granted
```

The config's rules are tried first, then the built-in ones (`defaults = false` drops those):
pytest and py.test (with xdist workers, and without), tox and nox; cargo build, test, check,
clippy, bench, run and doc; make, ninja and bazel; `cmake --build`; go build and test; mvn and
gradle; docker and podman build; npm, pnpm, yarn and bun test, build, install, and the test
tools run through them (`pnpm vitest`); jest, vitest, playwright, tsc, webpack and vite; and
`just test`, `build` and `check`. Builds and test runs, JavaScript test runners and `tsc`
included, weigh `heavy_weight` (4). Package installs, webpack and vite weigh `light_weight`
(2). Lower them to run more at once (a new config on 4 to 7 slots offers this as comments:
[two at once](#your-own-daemon)).

A rule's cases weigh a command by its arguments. Each `[[commands.rule.case]]` has `args`, a regular
expression searched in the arguments joined by spaces; `weight`, a count, or 0 for no lease;
and an optional `note`, which `leasehold commands` prints. They are tried in order on a
command the rule matches, and the first that matches gives its weight. A command no case
matches weighs the rule's own weight, so arguments leasehold does not recognise never weigh
less than the rule:

```toml
[[commands.rule]]
programs = ["pytest"]
weight = 4

[[commands.rule.case]]
args = "(^| )--collect-only( |$)"
weight = 0             # no lease
note = "collection only"

[[commands.rule.case]]
args = "::"
weight = 1
note = "named tests"
```

The built-in rules have these cases:

| Program | No lease | Weight 1 |
|---|---|---|
| pytest, py.test | `-h`, `--help`, `--version`, `-V`, `--collect-only`, `--co`, `--fixtures`, `--fixtures-per-test`, `--markers` | named tests, without xdist workers: a node id (`tests/test_x.py::test_y`, but not one given to `--deselect`), or a `-k` selection that does not start with `not` |
| tox, nox | help, version, `-l`, `--list`, `--listenvs` | |
| cargo, cmake, docker, podman, webpack, vite | help or version before a `--` (`cargo run -- --help` still builds) | |
| make, gmake | help, version, `-n` and the other dry runs, `-q`; or only `clean`, `distclean`, `mrproper` and `help` targets (with `-C`, `-f`, `-j` or `-s`) | |
| ninja | help, version, `-n`, `-t` | |
| bazel, bazelisk | help, version; `info`, `version`, `query`, `shutdown`, `help`, `clean` and `license`, after startup options | |
| go | help | `go test -run NAME` in named packages: no `./...`, no `-bench` |
| mvn, gradle | help, version, `-v` | |
| npm, pnpm, yarn, bun | help or version before a `--`; the test script's or test tool's help or version after one (`npm test -- --help`) | |
| jest, vitest, playwright, tsc | help, version, `-v`, `--listTests`, `--list`, `--showConfig`, `--init`, `--all`, `vitest list` | |

Everything else a rule matches weighs its weight: a serial `pytest -q` and `pytest
tests/test_x.py` weigh `heavy_weight` like `pytest -n auto`, and so do `cargo run`, `make
clean all` and `go test ./...`. A command with no lease runs at once and is not counted. A
command that a config rule matches is weighed by the config's rules alone, so a config rule
for pytest needs cases of its own to weigh `pytest --version` as none.

A weight is both what a command costs in slots and the job count it is given. It is a
count (`weight = 4`) or a [range](#ranges-min-max-slots) (`weight = "2-4"`), whose command is
granted a size between the two when admitted and run sized to it. A case's weight is a count.
A rule's
`threads` sets the parallelism inside each process ([sizing](#sizing-the-command-to-its-lease)):
`"jobs"`, the job count (the default), or a count of at least 1. The built-in xdist rule has
1, and a pytest run that starts xdist workers, under any rule, shares the job count among
them; a rule's own `threads` can only lower that
([sizing](#sizing-the-command-to-its-lease)). Among one source's rules the heaviest match
wins, each weighed by its case, by the least of a range; of equals, the first. The daemon
clamps each weight, each end of a range and each case's weight to the most its pool could
ever grant one lease, beside the other pools' reservations and standing work.

A caller can ask for its own size with `LEASEHOLD_WANT=2-4` (or `=3`). A PATH shim reads it
from its command's environment, so `LEASEHOLD_WANT=2-4 pytest -n auto` works through a shim;
the Claude Code hook reads it from Claude Code's own environment, so it applies to the whole
session, and a `LEASEHOLD_WANT=` written in front of the agent's command is not seen. Either
way it replaces the weight of whichever rule the command matches, any rule. Both ends are cut
to the most one lease under that rule could ever be granted (the rule's `ceiling` in
`GET /v1/commands`), and leasehold says what it cut. `hold` and a shim size the command to
the size granted, within the cut range; the hook does so only when the command then runs
through a shim. A value that is not a weight is ignored, and said. `hold` removes it from
the environment of the command it runs, and a shim ignores it while the agent it runs under
holds a hook's lease for any command. `leasehold commands` lists the rules;
`leasehold commands --test "CMD"` says which rule and case a command matches and the threads it
gets, and exits 0 when it takes a lease and 1 when it runs without one.

A command line is read as the shell would split it, without running anything: `&&`, `||`,
`;`, pipes, groups, redirections, `VAR=1` assignments, wrappers (`env`, `time`, `nice`,
`timeout`, `sudo`, `xargs`, `npx`, `uv run`, `poetry run` and others, with their options) and
`sh -c '...'` are seen through. What a literal `false &&` or `true ||` skips is left out. A
program reached through a variable, an alias, a function or a script is not seen.

### Watched sessions

Your own daemon watches agent sessions and reports how much memory everything each agent
started holds. It only watches, and never pauses or stops a process.

A session joins in one of these ways:

- `leasehold run` registers the session it starts, with the agent's process and its slice.
- The [Claude Code plugin](#the-claude-code-plugin) registers each session at `SessionStart` and
  unregisters it at `SessionEnd`. It also adds `export LEASEHOLD_SESSION=cc-<id>` to the
  session's `CLAUDE_ENV_FILE`, so every Bash command carries the session's id.
- A Python program: `with leasehold.session("nightly") as s:` registers the calling process for
  the block, and sets `LEASEHOLD_SESSION` for the processes it starts ([From
  Python](#from-python)).
- Any other harness: `leasehold session register --pid PID` prints the session's id, which you
  export to the agent's commands as `LEASEHOLD_SESSION`, and `leasehold session unregister ID`
  ends the session (exit 77 for an id no session has). `--session` names it and `--agent` says
  what to call the agent. `--pid` defaults to the process that runs the command. Or use
  `POST /v1/sessions` ([the HTTP API](#the-http-api)).
- Found by name: your own daemon also watches each of your processes whose name (the kernel's
  `comm`; for a script, the script's name) is in `[watchdog] agents`, as a session `auto-<pid>`,
  unless it is already part of a session. This reaches agents started from an editor or a
  desktop app. Such a session never asked to join, so it is only ever reported. The default is
  `["claude", "codex", "gemini", "cursor-agent", "aider"]`, and `agents = []` turns it off.

Under `leasehold run`, a session's processes are those in its slice. Otherwise they are the
agent's descendants, plus your processes whose environment holds the session's
`LEASEHOLD_SESSION`. That second part finds a server the session left running after its shell
exited. The agent process itself is left out of the count. The rest is counted in trees, one
for each process whose parent is the agent (a shell command, an MCP server) or is outside the
session. A session's footprint is the anonymous memory of its trees, in RAM or in swap. File
cache is left out, because a build reads a lot of it and the kernel takes it back first.

A registered session ends when it unregisters or its agent process exits. A session found by
name ends when its agent exits. The daemon reads every session again every 2 seconds.
`leasehold status --json` lists them under `watchdog.sessions`, `leasehold top` shows each
one's footprint, and the events say when one starts and ends.

Where leasehold cannot list a session's processes yet (macOS among such systems), each session
has `how` set to `none` and a `reason`, and its footprint is null, so it never looks idle.

### The watchdog

Your own daemon judges how short of memory the machine is and acts on it, to help keep the
machine from freezing. With `[watchdog] mode = "on"` it pauses the newest heavy work an agent started,
stops it if pausing does not free enough, and tells the agent. `mode = "watch"` decides and
reports what it would do, and holds, pauses or stops nothing. `mode = "off"` turns the judging
off; sessions are still watched. `leasehold init` writes `mode = "on"`; a config with no
`[watchdog]` table gets `"watch"`.

Available memory counts only once swap is nearly full: when there is no swap, or less swap free
than the larger of 1 GiB and 10% of the swap. While the kernel swaps, it keeps available memory
near a level that says nothing about a freeze (idle browser tabs going to swap), so with swap
room the stall shares are the signal.

The floor is where a freeze starts. A desktop slows to a crawl once available memory falls
under the file cache it keeps hot (program text, the files a browser or editor has open). That
size is absolute, and does not grow with RAM. The kernel's `Mapped` (in `/proc/meminfo`) is a
cheap stand-in for it. So the floor is `critical_below` (2 GiB), or the highest `Mapped` over
the last `mapped_window_s` (5 minutes) plus 0.5 GiB, whichever is more. It uses the highest
value, because `Mapped` falls as the cache is evicted, which is just when the floor matters.
`critical_below` is only the lowest the floor goes, and `hold_below` only sets how far the `warn`
line sits above the floor (1 GiB by default); the lines in use are `floor_bytes` and
`hold_line_bytes`. When `Mapped` raised the floor, the `critical` reason in events and
holds says so; the `warn` reason names only its line. A `critical` reason reads like `2.8 GiB available, under 3.0 GiB (mapped 2.5 GiB + 0.5)`. Status shows
the floor in use and why (`floor_reason`, such as `2.6 GiB: mapped 2.1 GiB + 0.5`). A file
read with `read()` and not mapped is not in `Mapped`; the disk stall below covers that case.

The level is one of three:

- `critical`, the last resort before the machine freezes:
  - available memory under the floor while it counts;
  - every task stalled on memory more than 20% of the last 2 seconds. This share comes from the
    kernel's running PSI `full` total. The ten-second average lags by about 10 seconds and
    stays high after the cause has gone;
  - some task waited on the disk more than `io_stall_critical` (30%) of the last 2 seconds (PSI
    io `some`, from its running total) while available memory is under the floor plus 1 GiB,
    whatever the swap. When the kernel evicts file cache that is still in use and has to read
    it back, the wait counts as io, not memory. In real runs in a 16 GiB VM this was the only
    signal that tracked the freeze: under 6% on every level where the desktop was fine, and
    40% or more on every level where it was not. It needs low memory as well, since io stalls for
    any disk-bound work.
- `warn`: available memory under the floor plus the distance from `critical_below` to
  `hold_below` (1 GiB by default; `hold_line_bytes`) while it counts.
- `ok`: anything else.

A `critical` episode starts at the first `critical` reading and ends only once 5 seconds have
passed with no `critical` reading. A brief flap out of `critical` keeps the episode, and the
time it has lasted. After the watchdog kills a tree, and until the level is next `ok`, a disk
stall on its own neither keeps an episode going nor starts one: the cache read back after a
kill keeps the disk busy for a while, and that is no reason to kill another tree.

At `act_at` and above (`critical` by default) the watchdog acts: it holds new heavy work and
pauses a tree. `would` says what it would do now: `action` `pause` and the tree, or
`{"action": "hold"}` when it has no tree to choose. With `act_at = "warn"` it also holds new
heavy work at `warn`, and pauses there. In watch mode the events it would send carry
`would: true`: `held` once when a hold would begin, `paused` once for each new choice of tree,
and `runaway` when a tree is first found running away.

A tree is one of a session's trees (see [Watched sessions](#watched-sessions)). The choice, in
order:

1. A runaway: the tree growing fastest, by its own footprint, when it has grown by at least
   20 MiB a second over each of the last two 5 second windows, and either holds more than twice
   what its leases declared or learned, or grew by at least half of what the machine's
   available memory fell. Real builds and test runs repeat their peaks to within about 10%, so
   twice is far beyond. A test run that grows slowly while something else (a browser, say)
   takes the memory is not a runaway.
2. A tree whose stop would free a quarter of the machine's memory or more, the largest first.
3. The newest tree, by its first process's start, across all sessions. A tree that holds no
   lease and runs no tool shell (for a session its hooks registered) comes after those that
   do, so the commands the agent ran go first.

Each is chosen only when stopping it would help: when it is a runaway, or would return at least
a quarter of the deficit (what available memory lacks of the `warn` line) to available memory.
While memory is short, the daemon measures what a stop would free as each process's
proportional share of the anonymous memory it holds in RAM (`Pss_Anon`). Pages that forked
workers share count once across them, and memory in swap, which a stop returns to swap and not
to RAM, does not count (`frees_bytes`; until it is measured, the footprint stands in). While
available memory does not count (swap has room), every tree helps.

Some trees are never chosen:

- a tree under `min_size`;
- one the agent process runs under;
- one made only of leasehold's own processes: `leasehold hold`, a PATH shim, a hook, the daemon.
  A process is leasehold's if its program is leasehold, whatever its arguments name. In any
  other tree those processes are `spared`: left out of its footprint, and
  left running if it were paused, with the shells they run under, up to the first other
  program. A program that runs a shim (a test run calling `make`) is paused and stopped with
  its tree, and so is a program that holds its own lease through the Python API;
- one with a coding agent among its processes (`claude`, `codex`, `gemini`, `cursor-agent`,
  `aider`, `opencode`, `goose`), so a nested agent is left alone;
- one whose heads (its first process, and what its tool shells run) match a built-in pattern or
  one of `[watchdog] protect`. The built-in patterns match the program, package or module name,
  never a path elsewhere on the line: MCP servers such as `npx -y @modelcontextprotocol/...`,
  `uvx mcp-...`, `github-mcp-server` or `python -m my_mcp`; and browsers and editors
  (`xdg-open`, `code`, `cursor`, `zed`, Chrome, Chromium, Firefox) unless headless
  (`--headless`, `-headless`, `-marionette`). A test runner's browser further down a tree
  protects nothing;
- one whose first process started within 10 seconds of its agent, such as the MCP and language
  servers a session starts with;
- any tree of a session found by name, which never asked to join, unless `choose_discovered =
  true`.

```toml
[watchdog]
mode = "on"               # "off", "watch" (decide and report, never act) or "on"
act_at = "critical"       # "warn" or "critical": the least level at which it holds and pauses
critical_below = "2g"     # the lowest floor (floor_bytes, floor_reason say the one in use)
hold_below = "3g"         # sets the warn line's gap above the floor (hold_line_bytes)
min_size = "auto"         # the larger of 256 MiB and 2% of memory: smaller trees are never chosen
choose_discovered = false
protect = []              # regular expressions searched for in a tree's command line
max_pause_s = 900         # a tree paused this long is stopped (at least 10)
mapped_window_s = 300     # the floor follows the highest Mapped over this many seconds
io_stall_critical = 30    # % of the last 2 s some task waited on the disk: critical near the floor
```

`critical_below` must be under `hold_below`. The sizes are absolute, not shares of memory: what
decides a freeze is the file cache the machine needs to keep, plus the fall in available memory
while the watchdog reacts, and neither grows with RAM. The defaults come from a simulation of
the measured profiles of real builds and test runs, and real runs in a 16 GiB VM confirmed
that pausing at `critical` and stopping after 5 seconds keeps such a machine responsive. In
those runs a 2.5 GiB hot file set slowed the desktop in 4 of 4 runs with a 2 GiB floor, and in
none with the floor from `Mapped`. Without the episode's 5 seconds of calm, a level that
flapped across the floor restarted the clock, and 2 of 4 runs never killed anything.

The daemon reads the machine's memory every 2 seconds while memory is plentiful (level `ok`,
swap room or at least twice the `warn` line available, and under 1% of the time stalled), every
half second when it is nearer, and four times a second while the level is not `ok`. Sessions
are read every 2 seconds whatever the level; the watchdog adds no scan of its own.

The machine's daemon has no watchdog. Where leasehold cannot read the machine's memory (macOS
for now), the level is null, the reason says the watchdog cannot see memory there, and it never
acts. A host that reads memory but not `Mapped` or io stalls leaves them null: the floor is then
`critical_below`, and the disk stall never counts.

On macOS the swap gate can't be ported as it is. macOS creates swap files as it needs them, so
free swap has no fixed size to fall below. The kernel's memory pressure level (warn or
critical) would take its place. With no PSI, the rates of page-ins and of decompressions from
the memory compressor would stand in for the disk stall, against the same floor. None of this
is built or measured yet.

#### What it does when it acts

With `mode = "on"`:

- It pauses the tree first in line: a lease's scope with the kernel group's freezer, and
  the rest with SIGSTOP to each process, leaving out `hold`, the PATH shims and the shells the
  tool runs them under. It pauses the next tree only 2 seconds later, and only while available
  memory is still falling. A paused tree's leases are kept alive for it, and a renewal of one
  answers `paused: true` and `paused_for` (`newest`, `runaway` or `large`).
- Once the episode has lasted 5 seconds, counted from its start or from the last stop, it
  kills, at a `critical` reading, one paused tree that has been paused for at least 5 seconds,
  a runaway first, then the newest, with SIGKILL (a scope with `cgroup.kill`). A paused
  runaway is killed 5 seconds after its pause while the level is `warn` or worse, and a large
  tree waits for `critical` like any other. It kills a paused tree only while the tree still
  helps (down to an eighth of the deficit, so that a deficit near the line does not pause and
  resume it in turn). A tree paused longer than `max_pause_s` (900) is killed whatever else
  holds, so nothing stays paused past it.
- When no tree paused or in line would help, the shortage is outside leasehold's sessions. The
  trees it paused run again, since stopping them would not help, it holds new heavy work while
  the level is at `act_at`, sets `watchdog.outside` in the status, and tells the agents (at
  most every 5 minutes) that memory is short outside leasehold's sessions. A tree that grows
  enough to help ends this at the next reading.
- Once the level has been `ok` for 10 seconds, with room for the tree to grow again, it lets
  the oldest paused tree run. New heavy work then waits until that tree settles: the level is
  `ok`, available memory is at least the `warn` line and a margin, and the tree's footprint has
  been flat for 30 seconds. After `max_pause_s` from the resume it waits no longer.
- While a tree is paused or settling, or the level is at `act_at`, a new request that counts
  memory waits, and its `blocked` says `the watchdog holds new work: <why>`.
- It records each process of a tree it pauses, with its start time. When a paused tree's
  first process ends, the others it recorded are let run.

It never acts on the agent process, on a tree of only leasehold's own processes, or on anything
outside a session. A session found by name is only reported unless `choose_discovered = true`.

A command it stopped keeps its own exit code (137, or -9 from the Python API). Its memory
verdict (`Command.memory`, the last line `hold` and the PATH shims print, and the Claude Code
hook's `post`) has `kind` `leasehold` and a `watchdog` field: `why` (`critical`, `runaway` or
`max_pause`), `runaway`, `rerun`, `alone`, `test` and `paused_s`. The line says
that leasehold's watchdog stopped the command to keep the machine responsive. For a test run
it says this is not a test failure, and for anything else that it is not a failure of the
command. A renewal of the command's lease answers the same object as `stopped`.
`verdict.test_like`, `verdict.stopped_note` and `verdict.stopped_todo` give that wording.

When the command ran under a `[commands]` rule and was not a runaway, the next request from the
same session under the same rule runs alone: it waits until no other lease is held on the
machine, and its `blocked` says so. A command that was stopped while it already ran alone
gets `rerun: false`, and the line says to run it again only with fewer workers. `hold` and the
PATH shims run such a command again once, on their own, under a built-in rule. They never do
for a runaway, for a rule in your config, or for `cargo`, `bazel`, `docker`, `podman`, `npm`,
`pnpm` and `yarn`, which may leave work half done. The Python API does not run a command again.
The daemon forgets a rule's run alone after 30 minutes.

The agent hears of it once, in one line, at its next Bash tool call: the daemon leaves a notice
for a session its hooks registered, and the Claude Code hook passes it on as
`additionalContext`, and as a `systemMessage` for the person. A pause says the command continues
on its own and is not to be started again; a stop says why and whether to run it again. A stop
of a command the hook leased reaches the agent through the hook's `post` verdict instead, and a
stop of a command run under `hold` through `hold`'s own last line, so it is told once. `hold`'s
line on a pause says why the command was chosen.
`leasehold status` gives the reason new work waits (`watchdog.held`), and `leasehold top` shows
`paused`, `resumed` and `stopped` events.

The daemon keeps the trees it paused in `watchdog.json` beside the ledger. A daemon that stops
lets them run first, and one that starts lets any it finds there run, so no pause outlives the
daemon that made it. The file records each paused process with its start time, so a tree
whose first process has gone is let run all the same, and the boot it was written in: a file
from an earlier boot is only removed. As it starts, the user's daemon also thaws any lease
scope it finds frozen, and says so on standard error. While it acts, `leasehold serve` locks
the daemon's memory in RAM (`mlockall`, with `MCL_ONFAULT` on Linux, so that memory it reserved
and never touched takes no RAM), gives its threads small stacks, and raises its priority, where
the user may, so that it still runs when memory is short. `leasehold doctor` warns in a
`watchdog` check when the user's locked-memory limit (`ulimit -l`) is too low for that.

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

- A leased command runs in its lease's scope, as under `hold` (see "Containment on the hook
  path" below). It is measured, its outcome is recorded, a rule's `memory` sets its
  `MemoryHigh`, and a `memory_max` caps it.
- When memory ended a leased command, `post` tells the agent, as context it reads with the
  tool's result, that the command was killed for memory and is not a test failure, and what
  to do next. The person sees the same in the transcript. When the command's scope counted
  the kill, the verdict is certain, and it covers a command that went on after one of its
  processes was killed, such as pytest exiting 1 after an xdist worker died:
  `leasehold: the kernel's OOM killer ended a process of pytest (peak 7.8 GiB) when the machine ran out of memory, and pytest exited 1. This is not a test failure. Run it again only after the other heavy work has finished, with fewer workers.`
  For a command that was not contained, `post` compares the machine's count of OOM kills
  (`/proc/vmstat`) before and after a command that ended with exit code 137 (SIGKILL), and
  says "most likely". With `CLAUDE_CODE_TOOL_MEMORY_LIMIT` set, the message names that limit:
  `leasehold: pytest was most likely killed by Claude Code's tool memory limit (CLAUDE_CODE_TOOL_MEMORY_LIMIT=4G, shared by every command of this session). This is not a test failure. Run it alone, or raise the limit.`
  On systems other than Linux it says nothing yet.
- Sub-agents run in their parent's session, so a session's agents share its turns.
- `SessionStart` registers the session with the daemon, which then
  [watches](#watched-sessions) everything the Claude Code process starts, and `SessionEnd`
  unregisters it. `SessionStart` also adds `export LEASEHOLD_SESSION=cc-<id>` to the session's
  `CLAUDE_ENV_FILE`, so `hold`, the shims and the processes a command leaves running all carry
  the session's id.
- A renewer keeps the lease while the command runs, watches the Claude Code process, and
  stops at the Bash call's own timeout plus a minute, so an interrupted call cannot hold slots.
- On a busy machine the command waits in line, keeping its place, for up to `wait_s` (300 s
  by default). The hook waits before the command starts, so the Bash tool's own timeout does
  not count the wait. One that waited at least 5 s says so as it starts, to the person and to
  the agent (`additionalContext`), with the lease it waited behind: `leasehold: pytest waited
  42s in line for its lease, behind cargo test (session cc-ab12); the command itself has not
  been slow.` Still waiting after `wait_s`, it is refused with a message that says the
  command did not run and when to retry (`when_busy = "deny"`, the default), or it runs
  without a lease and the agent is told so (`"run"`). Either message names the heaviest lease
  in the pool, by its command and session, how long it has run and, once 3 clean runs of its
  rule in its repository are known, how long it usually takes, and the place in line the
  command gave up: `machine busy for 300s: cargo test (session cc-ab12) holds 8 slots,
  running 2m10s (it usually takes ~3m), 2 other leases hold 6; pool work holds 14 of 14; this
  needs 4; gave up its place in line (#1). The command did not run. Retry in about 1 minute,
  or do lighter work first.` Without a usual time it says to do lighter work first, then run
  it again. `leasehold doctor` warns when `wait_s` is under 120 s.
- When the daemon says no, with a `refused` answer (a request that could never be granted, a
  per-user limit) or an error answer under 500 (such as 403 `forbidden`), or the command
  matches a rule that `leasehold commands` marks as never granted, the command is denied with
  the daemon's text, whatever `when_busy` says. A command matching a never-granted rule is
  never run under a lighter rule's lease instead.
- When no daemon is reached, or it answers with an error of 500 or more (such as 503
  `ledger_unavailable` on a full disk), the command runs without a lease, and the agent is told
  why. The same rule holds for listing the rules and for asking for the lease.
- A command run in the background, and one inside a lease already held (`LEASEHOLD_LEASE_ID`
  set), pass straight through.
- A rule's [range](#ranges-min-max-slots), or a range in `LEASEHOLD_WANT`, is asked for as a
  range, with both ends cut to the rule's `hook_ceiling` (what the range alone would be
  granted on an idle machine). It waits for its MIN only, and admission sizes it like any
  other range. The hook notes the size granted, with its job count and threads. When the hook
  cuts either end to `hook_ceiling`, its log says so.
- Before every leased command runs, the hook tells the agent the size granted and the job
  count to run it with (`additionalContext`): "leasehold: pytest was granted 4 slots; run it
  with at most 4 jobs (or run the agent under `leasehold run`)." A range says which part of
  it was granted: "pytest was granted 3 of 2-6 slots".
- The hook does not change the command, so it cannot size it. Under `leasehold run`, the
  command goes through a PATH shim, which finds the hook's lease, takes no second one, and
  gives the command the job count and threads of the size granted. Outside `leasehold run`
  the command is not sized and uses whatever it finds, as a command under a fixed heavy rule
  did in 0.3.

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

#### Containment on the hook path

The hook cannot start the command, and it does not change it. Instead, the command's own shell
moves itself into the lease's scope before the command runs:

1. At the start of a session, the plugin's `SessionStart` hook (`leasehold hook claude-code
   start`) adds one line to the session's `CLAUDE_ENV_FILE`. Claude Code runs that file in the
   shell of every Bash command, before the command:

   ```sh
   [ ! -d /run/user/1000/leasehold-hooks-1000/pending ] || /path/to/python -I -m leasehold.attach "$$" </dev/null >/dev/null 2>&1 || :
   ```

2. When `pre` grants a lease, it leaves a note in `pending/` with the command's text.
3. The shell sees the directory and runs `python -m leasehold.attach` with its own pid. That
   finds the note for the command this shell runs (Claude Code runs it as `eval '...'`) under
   the same Claude Code process, and asks the systemd user manager to move the shell into a
   new scope, `lease-cc.<tool call>.scope`, sized to the lease. The command then starts, and
   everything it starts is in that scope.
4. A small `sleep` process keeps the scope until `post`, so its counters can still be read
   after the command ends. The lease's renewer measures the scope while the command runs, and
   `post` sends what it used with the release.

A command with no lease pays for one directory test, about 0.05 ms. A leased command pays for
the Python start and the move, about 0.2 s, before it runs.

The shell is not moved when it runs under a `memory.max` someone else set, such as Claude
Code's own `CLAUDE_CODE_TOOL_MEMORY_LIMIT` cgroup: it keeps its lease and stays under that
limit, and a kill there is reported as "most likely". It is not moved either without a systemd
user manager, with `[contain] mode = "off"`, or with `LEASEHOLD_CONTAIN=off`. In each of these
cases the hook's log says why. If a rule asked for a `memory_max`, the person is told that the
cap did not hold, and why. On macOS there are no scopes, so `start` adds no line and a leased
command is only counted; `pre` says so when a `memory_max` was asked for.

If no shell claims a note (an older Claude Code, or a session started before the plugin was
installed), the renewer falls back to a scan. It looks among the Claude Code process's
descendants for the shell that runs the command, and moves that shell and what it has started
into the scope. This is racy: memory the command used before the move stays charged outside the
scope. A command found by scan still gets the verdict and the cap, but its outcome is not
recorded, so a run that was only partly measured does not teach leasehold too little memory.

Another agent harness can use the same mechanism. Its pre-command hook offers the lease
(`leasehold.attach.offer`), its shell sources the lines `leasehold.attach.env_lines` gives, and
its post-command hook collects the result (`leasehold.attach.collect`). The lines are plain
POSIX shell.

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
past `wait_s`, it exits 75 and says why on stderr, with when to retry as the hook does
(`when_busy = "run"`: runs the program without a lease and says so). Under an agent whose tool
timeout `LEASEHOLD_TOOL_TIMEOUT_S` gives (`leasehold run` sets it), it waits at most half of
that, so the agent reads the reason before its own timeout ends the call. A program granted
after a wait of 5 s or more says how long it waited, and behind what. When the daemon says no (a refusal, an error answer under 500, a
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
| `acquire_lease` | Ask for a lease of a weight (or, with `max_weight`, a [range](#ranges-min-max-slots)); answers granted (with the weight granted and the job count) or queued within 20 s |
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

- `leasehold status` prints the machine: pools, load, lending, leases and the queue.
  `--json` prints the [status object](#the-status-object).
- `leasehold top` is the machine live in the terminal: the slots as numbered bays, each
  lease spanning as many as it weighs and each waiting one in a second lane; PSI pressure with a
  sparkline; the pools; the leases; the queue with why each waits and, with memory counted,
  what each counts; the memory budget in the header (`mem 5.9/11.7 GiB`); the
  [watched sessions](#watched-sessions), each with what its work holds in memory; lending;
  recent events.
  Keys: `q` quits, `p` pauses, `s` changes the order of the leases, `?` shows help. It follows
  `NO_COLOR` and draws in ASCII outside a UTF-8 locale. Off a terminal, on one smaller than
  80x20, or with `--once`, it prints one plain view and exits.
- `leasehold ui` serves the same view to a browser on `127.0.0.1`, at a link that carries a
  random token, and opens it (`--no-open` to only print it). The page is read-only; requests
  without the token, or for another `Host`, are refused.
- `leasehold outcomes` lists what [contained commands used](#containment), one line for each
  rule and repository: the runs kept, the 95th percentile and the most of `memory.peak`, the
  most anonymous memory, how many runs the OOM killer or `MemoryHigh` reached, and the mean CPUs
  a run kept busy (CPU time over wall time), and the [learned figure](#learned-memory) your
  own runs give (`2/3 runs` while too few are known). Your own runs only, unless you are an
  administrator. `--json` prints the API's answer, every outcome included.
- `leasehold leases` lists the leases held and the queue. Its `MEMORY` column is what each
  counts and where that comes from: `6.0 GiB declared`, `1.2 GiB learned (3 runs)`, or
  `4.0 GiB assumed`, followed by `, learned 1.2 GiB/slot` when learning is in shadow.
- `leasehold metrics` prints the status in the Prometheus text format;
  `--textfile DIR` writes `DIR/leasehold.prom` for node_exporter's textfile collector.
  `leasehold ui` serves the same at `/metrics?token=...`.
- `leasehold report --url URL` POSTs the status object to a URL every `--interval` seconds
  (30), with `--token-file` as a bearer token and `--header`s of your own: https unless the URL
  is on loopback, the token file not world-readable, redirects not followed.
  `deploy/leasehold-report.service` runs it.
- `leasehold doctor` checks the config, the socket (its path within the 107 bytes a unix
  socket allows, a `socket_mode` that lets every user in) and its permissions, the daemon and
  its clock, PSI, cgroup v2 and the slices the config names, `/proc` visibility, the ledger,
  and whether commands are contained (`enforced (systemd)`, or `advisory:` and why). It warns
  when your own config lacks what today's has ([upgrading](#upgrading-a-02-config)), when no
  built-in build or test rule could start while the memory guard is on, when `[commands]
  heavy_weight` runs two heavy commands at once and some of them declare no memory
  (`two_at_once`), and checks that the
  [memory budget](#memory) worked out for this machine lets work start, and fails a
  `[commands]` rule no lease could ever be granted for (its commands are refused). It exits 1
  on any failure and prints a fix for each problem. `--json` for scripts.
- `leasehold demo` runs a made-up busy machine, a daemon with a scripted crowd, in a
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
| `GET /v1/commands` | | the `[commands]` rules, each `weight` and `max_weight` clamped as the daemon would grant it, with `ceiling` (the most one lease under the rule could ever be granted), `hook_ceiling` (what the rule's range alone would be granted on an idle machine: an agent hook caps both ends of its range at this), all computed when served, `memory_per_slot` (a rule declaring only that is also served `memory`, its least weight's worth, for clients from before it), `memory_max`, the rule's [hard cap](#a-hard-cap-memory_max) in bytes (`"8589934592b"`), `"off"`, or null when it sets none, and `case`, its cases, each with `args`, `weight` (clamped the same way; 0 is no lease) and `note`; a rule no lease could ever be granted for carries `never`, the reason |
| `POST /v1/acquire` | `kind`, `pool`, `weight`, `max_weight`, `labels`, `session`, `lease_id`, `wait_s`, `ttl_s`, `memory` or `memory_per_slot` (a size such as `"6g"`), `elastic`, `rule` and `repo` (what the work's outcome will be filed under; with a rule, a request that declares no memory may count a [learned figure](#learned-memory)) | `status`: `granted` (with `lease`; `elastic`, the daemon's `[elastic]` settings; `contain`, its `[contain]` settings with `memory_max`, the `[commands] memory_max` [cap](#a-hard-cap-memory_max) in bytes or null; the lease has `max_jobs`, the job count of its `max_weight`, and `memory_counted_bytes`, the memory admission counts for it at the weight granted), `queued` (with `position`, `blocked`, the `min_weight` and `max_weight` it waits with, as admission would cut them now, and, while a maintenance lease holds or drains the machine, `maintenance` as in [status](#the-status-object)) or `refused` (with `reason`) |
| `POST /v1/renew` | `lease_id`, `ttl_s` | `ok`, `revoked` (with `reason`), `queued` or `unknown`; on `ok`, `paused` (whether the watchdog has the command paused), `paused_for` (why it chose it: `newest`, `runaway` or `large`) and `stopped` (null, or what the watchdog said of a stop: `why`, `runaway`, `rerun`, `alone`, `test`, `paused_s` and `available_bytes`); for a lease, its `weight`, `min_weight`, `max_weight`, `pool`, `jobs`, `elastic`, `target` and `target_jobs` |
| `POST /v1/resize` | `lease_id`, `weight` | `resized`, with the lease's `weight`, `jobs`, `target` and `target_jobs`; 409 `conflict` when it cannot grow now or is not held, 400 for a lease that is not elastic or a `weight` outside its range |
| `POST /v1/elastic` | `lease_id` | `elastic`, with the lease's `weight`, `jobs`, `target` and `target_jobs`: a held range is elastic from now on; 409 `conflict` when it is not held, 400 for a lease with no range |
| `POST /v1/release` | `lease_id`, `outcome` | `released` |
| `GET /v1/outcomes` | | `outcomes`, newest first, and `groups`, one for each rule and repository: the asking user's, or every user's for an administrator. A group has `learned_per_slot_bytes`, the [learned figure](#learned-memory) a slot the asking user's own runs there give (null while fewer than 3 clean runs are known, and for a group with no rule), and `learned_runs`, the clean runs behind it |
| `POST /v1/lend` | `open`: true or false | the lend state; administrators only |
| `POST /v1/pools/<name>` | `max`: a number, or null to reset | the pool; administrators only |
| `POST /v1/sessions` | `session` (its id, what its processes have in `LEASEHOLD_SESSION`), `pid` (the agent's process; without one, the session lasts until it is unregistered), `agent` (what to call the agent; default its program's name), `slice` (a `leasehold run` session's slice), `source` (`api`, the default, `run`, `hook` or `sdk`) | `registered`, with the `session` as the [status object](#the-status-object) shows it; registering an id again replaces it. 400 for a `pid` that is not running, 403 for another user's session |
| `DELETE /v1/sessions/<id>` | | `unregistered`, with the `session`; 404 `unknown_session` when no session has that id, 403 for another user's |

`acquire` waits up to `wait_s` (at most 300) for the lease, then answers `queued`; asking again
with the same `lease_id` keeps the place in line, and is not a new request. A place given back
(`/v1/release`) while its `acquire` waits ends that wait, `refused`. A new request over
its pool's per-user limits (`per_user_max_queued`, `create_rate_per_min`) is `refused` with
`code: "quota_exceeded"`; one that fits now is granted whatever the waiting limit. A request
that could never be granted (a pool that does not exist, a weight over its pool's `max`, memory
over the ceiling) is `refused` with `code: "never_grantable"`. A 0.2
daemon does not take `memory`; `leasehold.client.Client` then asks again without it.

`weight` is the least the lease needs and `max_weight` (at least `weight`; absent: `weight`)
the most it can use: a [range](#ranges-min-max-slots). A granted `lease` has `weight`, the size
granted, `min_weight` and `max_weight` (the range, `max_weight` as cut by the daemon), and
`jobs` for the size granted. `memory` and `memory_per_slot` together are a 400 `bad_request`,
as is a `max_weight` below `weight`. A daemon from before ranges does not take `max_weight` or
`memory_per_slot`; `leasehold.client.Client` then asks again without them (a per-slot size sent
as the `memory` of `weight` slots), and is granted `weight`.

`elastic: true` asks for an [elastic lease](#elastic-leases), and only a range (a `max_weight`
above `weight`) may ask for one: otherwise it is a 400 `bad_request`. A granted `lease` has
`elastic`, and `target`, the size it is asked to move to (its `weight` for a lease that is not
elastic), with `target_jobs`, the job count of that size. `POST /v1/resize` moves an elastic
lease to `weight`: below its size to give slots back, which always succeeds, or up to its
`target` to take the slots offered, which succeeds only while they are still free.
`POST /v1/elastic` makes a range already held elastic, which is how `hold` asks once its
command's jobs take jobserver tokens. A daemon from before elastic leases does not take
`elastic`; `leasehold.client.Client` then asks again without it, and the lease is fixed.

An acquire naming a pool that does not exist is `refused` with `reason` `there is no pool
'NAME'`.

A release may carry an `outcome`, what the lease's command used ([containment](#containment)):
`rule`, `repo`, `peak_bytes`, `anon_bytes`, `oom_kills`, `high_events`, `cpu_usec`, `wall_s`
and `exit_status`. Fields the daemon does not know are ignored. It is kept only while the lease
is still held. A 0.4 daemon refuses the field, so when a release with an outcome is answered 400,
or with an error of 500 or more, `leasehold.client.Client` sends it again without the outcome.

An error is answered with an `error` and a `detail`: 400 `bad_request` (a malformed request,
including a `Content-Length` that is not a non-negative integer), 403 `forbidden` (another
user's lease, or an administrator's route), 404 `not_found` (and `unknown_session`, for a
session that is not watched), 409 `conflict` (a request that
cannot be done now but may be later: an elastic lease that cannot grow now, or a lease no longer
held), 413 `too_large` (a body over 64 KiB, which is not read), 500 `internal` (the daemon
failed on the request; its traceback goes to the daemon's stderr), and 503 `ledger_unavailable`
(the ledger cannot be used now, at start or later, such as locked past the busy timeout, a full
disk or a read-only file system; admission fails closed). A client that stops sending part way
through a request is cut off after 30 seconds without an answer.

Events are kept in memory, the last 500, each with a `type`: `granted` (and whether it
`waited`), `queued` (with its `position`), `left` (the line, without a grant), `released`,
`ended` (with a `reason`: `expired`, `holder gone`, or `no longer matched` for an observed
program), `revoked`, `resized` (an elastic lease's size changed, from `previous_weight`),
`lend_opened` and `lend_closed`, and `session_started` and `session_ended` (a
[watched session](#watched-sessions), with its `session`, `source`, `user`, `agent` and
`agent_pid`; an end has a `reason`: `unregistered`, `agent ended`, or `joined` when a registered
session took in an agent that was found by name), and from [the watchdog](#the-watchdog),
`held` (new heavy work begins to wait, with `level` and `reason`), `paused` (the tree it
pauses, with `level`, `reason` and the tree's fields as in `would`), `resumed` (a paused tree
runs again, with `paused_s`), `stopped` (it killed a tree, with `stopped_for`, `runaway` and
`paused_s`) and `runaway` (a tree newly found running away). Each has `would`, which is true
while the watchdog only decides and does nothing. A restarted daemon numbers them afresh. An event about a lease
or a place in line carries its `weight` (a lease's size, a waiting item's least),
`min_weight` and `max_weight`.

### The status object

`GET /v1/status` and `status --json`, versioned by its `schema` field (2), which goes up only
when a field is removed or changes meaning:

- `version`, `mode` (`user` or `system`), `time`, `machine` (`name`, `labels`), `facts`;
- `ceiling` (`slots`, `cpus`, `memory`, `memory_bytes`), and the machine's `slots`, `held` and
  `overcommitted`;
- `pools`, per pool: `reserved`, `max`, `priority`, `headroom` (as a count),
  `preemptible`, `observed`, `closed`, `held_weight`, `overcommitted`, `running`, `revoked`,
  `queued`, `memory_counted_bytes`. A
  pool's `held_weight` includes its standing weight; `running` counts leases only;
  `memory_counted_bytes` is the memory admission counts there (0 without `[memory]`);
- `load`: `pressure`, `loadavg`, `mem_available_bytes`, `mem_total_bytes`, `cgroup_v2`,
  `slices`, `memory_limit` (the cgroup memory limit as last read: `cgroup`, `limit_bytes` and
  `used_bytes`, or null without one);
- `lend`: `eligible`, `reason`, `quiet_since`, `quiet_s`, `window`, `pools`, `switch`;
- `guard`: `enabled`, `on`, `pressing` (whether the last reading was itself over a threshold,
  not only settling), `reason`, `since`, `memory_pressure_on`, `memory_pressure_off`,
  `min_available_bytes`, `settle_s`, `pressure_cgroups` (the cgroups whose memory pressure it
  reads besides the machine's, relative to the cgroup root);
- `memory`: `counted` (whether the config has `[memory]`), `machine_bytes`, `limit_bytes` and
  `limit_cgroup` (the cgroup memory limit when the daemon started, and the cgroup that sets it,
  or null without one), and with it
  `budget_bytes`, `kept_bytes`, `assumed_per_slot_bytes`, `held_bytes` (counted now, standing
  memory included) and `free_now_bytes` (what may start now while the guard is on, else null);
  without it these are null; and `learn`, the `[memory] learn` setting (`"off"` without the
  table);
- `contain`: the daemon's `[contain]` settings, `mode` and `hard_cpu` (whether a given caller
  is contained is that caller's own `leasehold doctor`); `memory_max`, the `[commands]
  memory_max` [cap](#a-hard-cap-memory_max) in bytes (`"8589934592b"`) or null; and
  `memory_cap`, how a cap works on this machine: `how` (`exact`, `polled` or `none`) and,
  for `none`, `reason`;
- `host`: what the daemon's host can do on this machine, one entry per operation (`start`,
  `adopt`, `members`, `footprint`, `pressure`, `pause`, `resume`, `kill`, `ended`,
  `memory_hard`, `session_processes`), each with `how` (`exact`, `polled` or `none`), `reason`
  (why not, for `none`) and `via` (what does it in words, such as `systemd`, or null);
- `maintenance`: the [maintenance lease](#maintenance) that holds the machine, or else the
  oldest one draining it, or null: `lease_id`, `state` (`held` or `draining`), `user`, `name`
  (its `name` label, or else its user), `reason`, `until` (its `until` label, or null) and
  `message`, the words a request it holds up gives as `blocked`;
- `watchdog`: the [watched sessions](#watched-sessions). `processes` says whether a session's
  processes can be found here (`how`: `polled` or `none`, and `reason`), `discover` lists the
  `[watchdog] agents` names this daemon looks for (empty for the machine's daemon), and
  `sessions` lists each session, oldest first: `session`, `source` (`run`, `hook`, `sdk`, `api`
  or `auto`), `user`, `agent`, `agent_pid`, `slice`, `since`, `how` and `reason` (as for
  `processes`, null until first read), `footprint_bytes` (the anonymous memory of everything
  the agent started, in RAM or swap; null when it cannot be read), `processes` (not counting
  the agent), `trees`, and `largest` (`pid`, `name` and `bytes` of the process with the most).
  It also has [the watchdog](#the-watchdog)'s decision: `mode` (`on`, `watch` or `off`), `act_at`,
  `level` (`ok`, `warn`, `critical`, or null when memory cannot be read or the watchdog is off),
  `reason`, `available_bytes`, `swap_free_bytes` (null without swap), `available_counts`
  (whether available memory counts, with swap nearly full or none), `stall_recent` (the
  percentage of the last 2 seconds every task stalled on memory), `stall_io_recent` (the
  percentage of the last 2 seconds some task waited on the disk, or null where it cannot be
  read), `floor_bytes` and `floor_reason` (the floor in use and why, such as
  `2.6 GiB: mapped 2.1 GiB + 0.5` or `2.0 GiB: critical_below`), `hold_line_bytes` (the `warn`
  line), `limits` (`critical_below`, `hold_below`, `min_size` and `large`, a quarter of memory,
  in bytes, and `max_pause_s`, `mapped_window_s` and `io_stall_critical`), `how` (`pressure` and
  `footprint`: how each is read, or `none`), `candidates` (the trees it could choose),
  `passed_over` (how many it may not choose, by why: `agent`, `leasehold`, `discovered`,
  `protected`, `started with its agent`, `small`), `victim` (the tree first in line whenever
  the level is not `ok`), `would` (what it would do now: null below `act_at`,
  `{"action": "hold"}`, or `action` `pause` with the tree), `held` (why new heavy work waits
  now, or null; always null in watch mode), `outside` (true while no tree in a session would
  help), `paused` (the trees it paused, or let run and that
  have not ended, oldest first, each with `paused_s` and `resumed`) and `stopped` (the last 20
  trees it killed, each with `stopped_for` and `paused_s`). A tree has `session`, `pid`, `cmd`, `footprint_bytes`,
  `frees_bytes` (what stopping it would return to available memory, or null until measured),
  `expected_bytes` (what its leases declared or learned, or null), `growth_bytes_per_s`,
  `started`, `why` (`runaway`, `large` or `newest`), `runaway` and `spared` (the pids of
  leasehold's own processes in it);
- `standing`, `leases`, and `queue`, each queued item with its `position` and why it waits
  (`blocked`). Each lease and queued item has `memory_bytes` (what it declared, or null; a
  lease declaring memory per slot gives the total it was granted, its weight times the size),
  `memory_per_slot_bytes` (what it declared per slot, or null) and `memory_counted_bytes`
  (what admission counts for it). `memory_from` says where that comes from: `declared`,
  `learned` or `assumed` (its weight times `assumed_per_slot`). `learned_per_slot_bytes` is
  the [learned figure](#learned-memory) a slot it was given when it asked, whether counted or
  only shown (null: none), and `learned_runs` the clean runs behind it, also while too few are known
  (null: none was worked out). Each also has `weight`, `min_weight` and `max_weight`: a lease's
  `weight` is the size it holds (granted, or since moved to by an elastic lease), a queued
  item's the least it needs, and `min_weight` and `max_weight` are the
  [range](#ranges-min-max-slots) asked (both `weight` for a lease from before ranges). Each
  also has `elastic`, and each lease its `target`, the size it is asked to move to (its
  `weight` unless it is [elastic](#elastic-leases)), and `giving_back`, how far the target is
  below the weight: the slots it is asked to give back, and will once its jobs finish. `top`
  shows a target that differs from the weight as `3>4`, and a lease giving slots back says so
  beside it. A pool's `held_weight` counts the sizes held.

## The command line

Every command takes `--config PATH` and `--socket PATH`, before or after the command's name.
`leasehold --help` lists the everyday commands; the rest (`acquire`, `keep`, `release`,
`outcomes`, `hook`, `shims`, `facts`, `ui`, `metrics`, `mcp`, `report`, `observe`, `lend`,
`pool`, `session`) work the same and
are named at its end. `leasehold COMMAND --help` says more; `mcp`, `ui`, `metrics`, `report`,
`observe` and `lend` are experimental and may change before 1.0.

<!-- cli:start -->

set up your own leasehold: config, daemon, shims; --remove undoes it:

```
leasehold setup [--remove] [--upgrade-config] [--dry-run]
```

run an agent as one session: its heavy commands leased and contained:

```
leasehold run [--session SESSION] [--leave-running] [--quiet] [--no-contain] [--tool-timeout S] ...
```

run a command under a lease:

```
leasehold hold [--kind {work,external,lend,maintenance}] [--pool POOL] [--weight N|MIN-MAX] [--label K=V] [--session SESSION] [--timeout S|Nm|Nh] [--ttl TTL] [--memory MEMORY] [--memory-per-slot MEMORY_PER_SLOT] [--until HH:MM|ISO] [--reason REASON] [--grace GRACE] [--jobserver] [--elastic | --no-elastic] [--no-job-env] [--threads N|jobs] [--memory-max SIZE|N%|off] [--if-unavailable run|fail] ...
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

watch a session; prints its id, for LEASEHOLD_SESSION:

```
leasehold session register [--session SESSION] [--pid PID] [--agent AGENT]
```

stop watching a session:

```
leasehold session unregister SESSION
```

what the config's [[observe]] rules match right now:

```
leasehold observe [--json]
```

wait for a lease another process will release:

```
leasehold acquire --lease-id LEASE_ID [--kind {work,external,lend,maintenance}] [--pool POOL] [--weight N|MIN-MAX] [--label K=V] [--session SESSION] [--timeout S|Nm|Nh] [--ttl TTL] [--memory MEMORY] [--memory-per-slot MEMORY_PER_SLOT] [--until HH:MM|ISO] [--reason REASON] [--keep] [--max-hold MAX_HOLD] [--watch-pid WATCH_PID]
```

renew a lease until it is released or a limit is hit:

```
leasehold keep --ttl TTL [--max-hold MAX_HOLD] [--watch-pid WATCH_PID] [--attach] lease_id
```

give a lease back:

```
leasehold release lease_id
```

what contained commands used, by rule and repository:

```
leasehold outcomes [--json]
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
leasehold hook {claude-code} {start,pre,post,end}
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
