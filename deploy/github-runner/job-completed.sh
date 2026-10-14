#!/bin/sh
# ACTIONS_RUNNER_HOOK_JOB_COMPLETED: give back the lease job-started.sh took for this job. It
# never fails the job: a lease it cannot release ends by itself (its renewer stops with the
# job's Runner.Worker, and the lease one TTL later).
set -u

# shellcheck source=common.sh
. "$(dirname "$0")/common.sh"

id=$(lease_id)
if ! have_leasehold; then
	exit 0
fi
if "$LEASEHOLD" release "$id"; then
	say "lease $id released"
else
	say "WARNING: could not release lease $id; it ends at its TTL once its renewer stops"
fi
exit 0
