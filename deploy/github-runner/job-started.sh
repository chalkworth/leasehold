#!/bin/sh
# ACTIONS_RUNNER_HOOK_JOB_STARTED: take an `external` leasehold lease for this job before any
# of its steps run, waiting in line while the machine is busy. The wait is the queue: a job that
# cannot be admitted blocks here until capacity frees (or its own timeout ends it), since a
# runner hook cannot hand a job back. The lease is kept by a detached renewer
# (`leasehold acquire --keep`) and given back by job-completed.sh; if that never runs, the
# renewer stops when this job's Runner.Worker ends, or after LEASEHOLD_RUNNER_MAX_HOLD
# seconds, and the lease ends one TTL later.
#
# It fails open: with leasehold missing, unreachable or refusing, the job runs unaccounted,
# with a loud line in the job's log and the journal. CI never breaks because leasehold is down.
#
# Settings (environment, e.g. the runner's .env):
#   LEASEHOLD_RUNNER_WEIGHT    the lease's weight in slots (default 4)
#   LEASEHOLD_RUNNER_MAX_HOLD  the longest the lease is renewed, seconds (default 21600)
#   LEASEHOLD                  the leasehold command (default: leasehold on PATH)
#   LEASEHOLD_SOCKET           the daemon's socket (default: the machine config's)
set -u

# shellcheck source=common.sh
. "$(dirname "$0")/common.sh"

weight=${LEASEHOLD_RUNNER_WEIGHT:-4}
max_hold=${LEASEHOLD_RUNNER_MAX_HOLD:-21600}
id=$(lease_id)

# worker_pid: the Runner.Worker process running this job (an ancestor), or nothing.
worker_pid() {
	pid=$PPID
	while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do
		comm=$(ps -o comm= -p "$pid" 2>/dev/null) || return 0
		if [ "$comm" = "Runner.Worker" ]; then
			printf '%s\n' "$pid"
			return 0
		fi
		pid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')
	done
}

if ! have_leasehold; then
	say "WARNING: '$LEASEHOLD' is not installed; job $id runs WITHOUT a lease (unaccounted)"
	exit 0
fi

worker=$(worker_pid)
set -- --lease-id "$id" --kind external --weight "$weight" --keep --max-hold "$max_hold" \
	--label consumer=github-runner --label "runner=${RUNNER_NAME:-}" \
	--label "repository=${GITHUB_REPOSITORY:-}" --label "run=${GITHUB_RUN_ID:-}" \
	--label "job=${GITHUB_JOB:-}"
if [ -n "$worker" ]; then
	set -- "$@" --watch-pid "$worker"
fi

# The runner runs a .sh hook as `bash -e`: catch the code, so a failure never ends the hook.
code=0
"$LEASEHOLD" acquire "$@" >/dev/null || code=$?
case $code in
0) say "lease $id held (external, weight $weight)" ;;
69) say "WARNING: the leasehold daemon is unreachable; job $id runs WITHOUT a lease (unaccounted)" ;;
77) say "WARNING: leasehold refused lease $id (weight $weight); job runs WITHOUT a lease (unaccounted)" ;;
*) say "WARNING: leasehold acquire exited $code; job $id runs WITHOUT a lease (unaccounted)" ;;
esac
exit 0
