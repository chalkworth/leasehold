#!/bin/sh
# A test suite held to a weight-2 lease, three ways. The commands of README.md, in order; needs
# a leasehold daemon (`leasehold serve`).
set -eu
cd "$(dirname "$0")"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/running"
export RUNNING="$work/running" LOG="$work/log"

# most: the most jobs job.sh saw running at once since the last call.
most() {
	sort -n "$LOG" | tail -n 1
	rm -f "$LOG"
}

# 1. GNU make 4.4 and later: a jobserver holding the lease's 2 jobs, sub-makes included.
leasehold hold --weight 2 --jobserver -- make -s all
echo "make under --jobserver: at most $(most) jobs at once"

# 2. Any make: pass the job count hold sets, $LEASEHOLD_JOBS, yourself.
leasehold hold --weight 2 -- sh -c 'make -s -j"$LEASEHOLD_JOBS" all'
echo "make -j\$LEASEHOLD_JOBS: at most $(most) jobs at once"

# 3. pytest-xdist: -n auto starts $PYTEST_XDIST_AUTO_NUM_WORKERS workers, which hold sets.
if python3 -c 'import xdist' 2>/dev/null; then
	leasehold hold --weight 2 -- python3 -m pytest -q -p no:cacheprovider -n auto test_sample.py
	echo "pytest -n auto: at most $(most) tests at once"
else
	echo "pytest-xdist is not installed: skipped pytest -n auto"
fi
