#!/bin/sh
# The lent work: stress-ng on 2 CPUs when it is installed, otherwise a stand-in that sleeps. It
# runs until it is stopped: `leasehold hold` sends it SIGTERM when the lease is revoked.
echo "batch job started"
if command -v stress-ng >/dev/null 2>&1; then
	exec stress-ng --cpu 2 --timeout 600s --quiet
fi
exec sleep 600
