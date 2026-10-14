#!/bin/sh
# Run the runner hooks (deploy/github-runner/) the way the runner runs them for one job: with the
# job's environment, as `bash -e`. The commands of README.md, in order; needs a leasehold daemon.
set -eu
hooks=$(cd "$(dirname "$0")/../../deploy/github-runner" && pwd)

# What the runner sets for a job. The hooks build the lease id from these.
export RUNNER_NAME=buildbox-1 GITHUB_REPOSITORY=acme/api GITHUB_RUN_ID=1001 \
	GITHUB_RUN_ATTEMPT=1 GITHUB_JOB=test
# From the runner's .env: this runner's jobs weigh 2 slots each.
export LEASEHOLD_RUNNER_WEIGHT=2

bash -e "$hooks/job-started.sh"
leasehold leases
# ... the job's steps run here ...
bash -e "$hooks/job-completed.sh"
leasehold leases
