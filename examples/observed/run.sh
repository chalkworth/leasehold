#!/bin/sh
# Start a known heavy program outside leasehold and see the daemon charge it. The commands of
# README.md, in order; needs a daemon on this directory's machine.toml.
set -u
cd "$(dirname "$0")" || exit 1

python3 prep_dataset.py &
prep=$!

# What the config's [[observe]] rules match right now. It reads the config and /proc only.
leasehold observe

# Within one tick the daemon charges it: an observed lease of weight 3.
until leasehold leases | grep -q '^observed\.'; do sleep 0.5; done
leasehold status

# New work waits while it runs: 2 more slots do not fit beside its 3.
leasehold hold --weight 2 --timeout 1 -- echo "not reached"
echo "hold exited $? (75: still waiting at --timeout)"

# When the program ends, so does its lease, and the work gets in.
kill "$prep"
wait "$prep"
leasehold hold --weight 2 -- echo "the work runs once the data prep has ended"
