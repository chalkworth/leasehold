#!/bin/bash
# scenario_batch.sh OUT < lines: run "NAME ARGS..." lines of bench.scenarios, each under the lab
# lock (flock /home/bench/lab.lock), released between runs.
out=$1
cd "$(dirname "$0")/.." || exit 1
while read -r name args; do
  [ -z "$name" ] && continue
  case "$name" in \#*) continue ;; esac
  # shellcheck disable=SC2086
  flock /home/bench/lab.lock uv run --no-sync python -m bench.scenarios run "$name" \
    --out "$out" $args 2>&1 </dev/null | tail -1
done
