#!/bin/sh
# Lend idle capacity to a batch job, then see the lease revoked when the owner's work arrives.
# The commands of README.md, in order; needs a daemon on this directory's machine.toml.
set -u
cd "$(dirname "$0")" || exit 1
log=$(mktemp)
trap 'rm -f "$log"' EXIT

leasehold lend on

# 1. The batch job asks for the 2 lend slots. They are granted once the machine has been idle
#    for quiet_s. A short TTL makes it renew every second, so it hears of a revocation quickly.
leasehold hold --kind lend --weight 2 --ttl 3 -- ./batch-job.sh >"$log" 2>&1 &
lent=$!
until grep -q 'batch job started' "$log"; do sleep 0.2; done
leasehold status

# 2. The owner's build needs all 4 slots. It has to wait, and work waiting revokes every lend
#    lease at once: the batch job gets SIGTERM at its next renewal, and the build runs.
leasehold hold --weight 4 -- echo "the owner's build runs on all 4 slots"
wait "$lent"
echo "the batch job's hold exited $? (76: revoked)"
cat "$log"

# 3. Switch lending off: no lend lease is granted until `leasehold lend on`.
leasehold lend off
