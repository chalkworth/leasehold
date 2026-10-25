#!/bin/sh
# One ephemeral GitHub Actions runner for one job, brought online only once it holds a lease.
# Run it from the runner's directory, and again after each job (in a loop, or as a service with
# Restart=always). The commands of README.md, in order.
#
#   RUNNER_REPO               owner/repo the runner serves
#   LEASEHOLD_RUNNER_WEIGHT   the job's weight in slots, or a range MIN-MAX (default 1-4)
set -eu
weight=${LEASEHOLD_RUNNER_WEIGHT:-1-4}

# Register a runner that takes one job and then removes itself. A registration token lasts an
# hour, so each pass asks for a new one.
token=$(gh api -X POST "repos/$RUNNER_REPO/actions/runners/registration-token" --jq .token)
./config.sh --unattended --ephemeral --replace --name "$(uname -n)-ephemeral" \
	--url "https://github.com/$RUNNER_REPO" --token "$token"

# Wait in line for the slots, then go online and run the one job.
exec leasehold hold --kind external --weight "$weight" --label consumer=github-runner -- ./run.sh
