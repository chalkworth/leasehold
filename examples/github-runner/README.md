# A self-hosted GitHub Actions runner

Two ways to make a runner's jobs take `external` leases, so CI waits in line on a busy machine
instead of piling on:

- **Hooks**, for a runner that stays registered: the runner's job-started hook takes a lease
  before the job's first step, and its job-completed hook gives it back.
- **Ephemeral**, for a runner that registers for one job: the runner only comes online once it
  holds a lease.

Use one or the other on a runner, not both: a runner started under `hold` that also has the hooks
holds two leases per job.

## Hooks

The hooks are `deploy/github-runner/job-started.sh` and `job-completed.sh`. Install them as
"GitHub Actions runner hooks" in `docs/reference.md` describes (the `leasehold` group, the copy to
`/opt/leasehold/github-runner/`, the `runner.slice` drop-in), then add to the runner's `.env`
(`<runner-dir>/.env`, read when the runner starts, so restart it after a change):

```
ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/leasehold/github-runner/job-started.sh
ACTIONS_RUNNER_HOOK_JOB_COMPLETED=/opt/leasehold/github-runner/job-completed.sh
LEASEHOLD_RUNNER_WEIGHT=1-4
```

`LEASEHOLD_RUNNER_WEIGHT` is the weight in slots of every job this runner takes: a number, or a
range `MIN-MAX` (`1-4` by default). A range waits for its least and is granted up to its most,
so the default fits a small `external` pool, such as the one `leasehold setup` writes on 8 CPUs,
which holds 3 slots. For jobs of different sizes, register one runner per size with its own `.env` weight and a label
of its own (`heavy`, say), and let each workflow choose with `runs-on: [self-hosted, heavy]`. A
weight above the `external` pool's `max` is refused, and the job then runs without a lease;
`job-completed.sh` then says it had no lease to release.

To see what the hooks do for one job, run them the way the runner does, against a daemon of your
own (`leasehold serve`):

```sh
./try-hooks.sh
```

It sets the variables the runner gives a job (`RUNNER_NAME`, `GITHUB_REPOSITORY`, `GITHUB_RUN_ID`,
`GITHUB_RUN_ATTEMPT`, `GITHUB_JOB`) and `LEASEHOLD_RUNNER_WEIGHT=2`, then runs
`bash -e job-started.sh`, `leasehold leases`, `bash -e job-completed.sh` and `leasehold leases`:

```
leasehold runner hook: lease gh.buildbox-1.1001.1.test held (external, weight 2)
LEASE                      KIND      POOL      W  USER  SESSION  EXPIRES  STATE
gh.buildbox-1.1001.1.test  external  external  2  ann   -        +15s     held
leasehold runner hook: lease gh.buildbox-1.1001.1.test released
LEASE  KIND  POOL  W  USER  SESSION  EXPIRES  STATE
```

The lease outlives `job-started.sh`: a renewer it starts in the background keeps it until
`job-completed.sh` releases it, until the job's `Runner.Worker` ends, or after
`LEASEHOLD_RUNNER_MAX_HOLD` seconds (6 h by default), whichever comes first.

### A waiting job uses up its timeout

GitHub has already assigned the job when the hook runs, and a hook cannot hand it back. So while
the machine is busy the job waits inside `job-started.sh`, and that wait counts against the job's
`timeout-minutes` (360 by default). Set `timeout-minutes` to the longest wait you accept plus the
job's own run time.

The hooks fail open: with leasehold not installed, its daemon down, or the weight refused, the
job runs without a lease and the hook writes a `WARNING ... WITHOUT a lease` line to the job's log.

### Gitea act_runner

Gitea's act_runner reads the same two variables from its own environment (`runner.envs` or
`runner.env_file` in its config) when `runner.hooks.job_started` and `job_completed` are unset,
and runs a `.sh` hook as `bash -e`, as GitHub's runner does. Two differences:

- It runs the hooks where the job's steps run: inside the job container, unless the runner
  label uses host mode. A containerized job sees neither `/opt/leasehold` nor the daemon's
  socket, so use host mode for jobs that should take leases.
- There is no `Runner.Worker` for the renewer to watch. If `job-completed.sh` never runs, the
  lease is renewed until `LEASEHOLD_RUNNER_MAX_HOLD`: set it to the runner's `runner.timeout`
  (3 h by default) in seconds, `10800`.

Forgejo Runner is a separate fork of act_runner: check that your version has `runner.hooks`
before relying on this.

## Ephemeral runners

An ephemeral runner (`config.sh --ephemeral`) takes one job and then removes its own
registration. `ephemeral.sh` registers one and runs it under a lease. Run it from the runner's
directory, with `gh` signed in to an account that may add runners to the repository:

```sh
export RUNNER_REPO=acme/api LEASEHOLD_RUNNER_WEIGHT=1-4
/path/to/ephemeral.sh
```

Each pass runs three commands:

```sh
token=$(gh api -X POST "repos/$RUNNER_REPO/actions/runners/registration-token" --jq .token)
./config.sh --unattended --ephemeral --replace --name "$(uname -n)-ephemeral" \
	--url "https://github.com/$RUNNER_REPO" --token "$token"
exec leasehold hold --kind external --weight "$weight" --label consumer=github-runner -- ./run.sh
```

A registration token lasts an hour, so each pass asks for its own. The registered runner stays
offline, so GitHub gives it no job, until `hold` has the lease and starts `run.sh`. `hold` renews
the lease while `run.sh` runs, and gives it back when the job is done and `run.sh` exits. Run the
script again for the next job: in a loop (`while :; do /path/to/ephemeral.sh || sleep 60; done`,
the sleep so a failing pass does not spin), or as a systemd service with `Restart=always` and
`Slice=runner.slice`.

The lease is held from the moment the runner comes online, including any time it sits idle
waiting for GitHub to send a job. That suits a machine where jobs are already queued; for a
runner that is often idle, the hooks hold slots only while a job runs.
