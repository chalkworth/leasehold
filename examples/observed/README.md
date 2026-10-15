# Charging a program that never asks

Some heavy programs are started by hand and will never run under `leasehold hold`: a
data-preparation script, a training run. An `[[observe]]` rule names such a program by what its
process looks like and says what it costs. While one runs, the daemon holds an `observed` lease of
that weight for it, so new work waits instead of piling on. The lease ends when the process does.

Start a daemon of your own on this directory's `machine.toml`, then run the script:

```sh
export LEASEHOLD_CONFIG=$PWD/machine.toml
leasehold serve &
./run.sh
```

`serve` refuses to start if a daemon of yours already answers on its socket: stop it first.

## The rule

```toml
[[observe]]
name = "data-prep"
weight = 3
[observe.match]
comm = "python3"
cmdline = 'prep_dataset\.py'
```

Every field under `match` must match. `cmdline` is a regular expression searched anywhere in the
command line, so on its own it would also match `vim prep_dataset.py`, or a `leasehold hold`
whose arguments mention the file; `comm`, the kernel's name for the process, rules those out. The
rule then matches `python3 prep_dataset.py` but not `python prep_dataset.py`, whose `comm` is
`python`. `exe` (a regular expression on the executable's path) and `user` are the other fields.

## What run.sh does

1. Starts `python3 prep_dataset.py` in the background. It only sleeps.
2. `leasehold observe` prints what the rules match right now. It reads the config and `/proc`,
   asks no daemon and changes nothing, so it is the way to try a rule before the daemon uses it:

   ```
   data-prep: weight 3 each, 1 running now
     pid 24245    uid 1000   python3 prep_dataset.py
   ```

3. Waits for the daemon to see it (within one `tick_s`), then `leasehold status`:

   ```
   slots 3/4 held; ...
   pools:
     work       0/4 held (reserved 2), 0 running, 0 queued
     ...
     observed   3/4 held (reserved 0), 1 running, 0 queued observed
   LEASE                     KIND      POOL      W  USER  SESSION  EXPIRES  STATE
   observed.data-prep.24245  observed  observed  3  ann   -        -        held
   ```

   An observed lease has no TTL (`EXPIRES -`): it lasts as long as the process, which the daemon
   checks by pid and start time on every tick. `leasehold leases --json` shows its `command`,
   `pid` and `rule` labels.

4. `leasehold hold --weight 2 --timeout 1 -- echo "not reached"` waits, since 2 slots do not fit
   beside the 3 held, gives up after a second, and exits 75:

   ```
   leasehold hold: waiting for 2 slots (work): place 1; 1 of 4 slots are free; this needs 2
   leasehold hold: gave up after 1s in line
   ```

5. Stops `prep_dataset.py`. Its lease ends at the next tick, and the next `hold` runs.

A program started under `leasehold hold` (or through a PATH shim) is never observed: it runs
under a leasehold command, which holds its lease already. That is also why a rule should name
`comm` or `exe` and not only `cmdline`: a `cmdline` pattern matches any process that merely
mentions the script, such as an editor with it open.

Observing only counts a program: it is already running, so its weight counts even past its
pool's `max` or the machine's slots, and only new work waits. `max_count` caps how many
of a rule's processes count at once.
