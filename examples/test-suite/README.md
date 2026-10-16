# A test suite held to a pool

`leasehold hold --weight N -- cmd` waits until N slots are free, then runs `cmd` and tells it how
many parallel jobs it has. A suite that runs as many jobs as the machine has CPUs then runs only
as many as its lease allows, and the next suite waits in line instead of piling on.

With a daemon running (`leasehold serve`, which by default has one slot per CPU):

```sh
./run.sh
```

The output, with 2 slots or more (the last line needs pytest-xdist):

```
make under --jobserver: at most 2 jobs at once
make -j$LEASEHOLD_JOBS: at most 2 jobs at once
pytest -n auto: at most 2 tests at once
```

The `Makefile` has six independent targets. Each runs `job.sh`, which notes how many jobs are
running at that moment, so `run.sh` can print the most that ran together.

## The job count

`hold` runs its command with the lease's job count in `LEASEHOLD_JOBS`, and in the variables
other tools read for how many jobs or workers to fan out to: `PYTEST_XDIST_AUTO_NUM_WORKERS`,
`CARGO_BUILD_JOBS` and `CMAKE_BUILD_PARALLEL_LEVEL` (`leasehold.jobs.WORKER_VARIABLES`). The
variables that limit the parallelism inside each process, such as `OMP_NUM_THREADS`,
`GOMAXPROCS` and `RUST_TEST_THREADS` (`leasehold.jobs.THREAD_VARIABLES`), get the thread
count: the job count, or 1 for a pytest run that starts xdist workers. A variable you set
before `hold` starts is left alone. The count is the weight times the daemon's `[jobs]
per_slot`, 1 by default. `--no-job-env` sets none of these variables, and removes the ones an
enclosing lease set, so each tool uses its own default.

## The three commands

1. GNU make 4.4 and later:

   ```sh
   leasehold hold --weight 2 --jobserver -- make -s all
   ```

   `--jobserver` hands make a jobserver (a named pipe announced in `MAKEFLAGS`) that holds the
   lease's job count. Every make under it, sub-makes included, takes a token per job, so the whole
   tree runs at most 2 at once. Do not pass your own `-j`: make would start its own jobserver.
   Ninja 1.13 and later, cargo and GCC's LTO take the same tokens. An older make warns and runs
   one job at a time.

2. Any make, or any tool that takes a job count:

   ```sh
   leasehold hold --weight 2 -- sh -c 'make -s -j"$LEASEHOLD_JOBS" all'
   ```

   The single quotes matter: the variable is set for the command, so the shell inside `hold`
   expands it, not yours.

3. pytest with pytest-xdist:

   ```sh
   leasehold hold --weight 2 -- python3 -m pytest -q -p no:cacheprovider -n auto test_sample.py
   ```

   `-n auto` reads `PYTEST_XDIST_AUTO_NUM_WORKERS`, so it starts 2 workers. `run.sh` skips this
   step when pytest-xdist is not installed.

In CI, give the suite's lease `--kind external` so it goes to the pool for other people's work,
and `--timeout S` to give up after S seconds in line (exit 75, busy) rather than wait forever.
