#!/bin/sh
# Three agent sessions of one user compete for one slot: agent-a asks three times, then agent-b
# and agent-c once each, and the sessions take turns. The commands of README.md, in order; needs
# a daemon on this directory's machine.toml.
set -u
cd "$(dirname "$0")" || exit 1
dir=$(mktemp -d)
trap 'rm -rf "$dir"' EXIT

# Something holds the slot while the agents line up.
leasehold hold --session setup -- \
	sh -c "touch '$dir/held'; until [ -e '$dir/go' ]; do sleep 0.1; done" &
until [ -e "$dir/held" ]; do sleep 0.1; done

# ask SESSION NAME: in the background, run a command that writes NAME, as SESSION; return once it
# is in line.
ask() {
	leasehold hold --session "$1" -- sh -c "echo $2 >>'$dir/order'" 2>"$dir/$2.log" &
	until grep -q waiting "$dir/$2.log"; do sleep 0.05; done
}
ask agent-a a1
ask agent-a a2
ask agent-a a3
ask agent-b b1
ask agent-c c1

leasehold leases
touch "$dir/go"
wait
echo "ran in this order: $(paste -sd " " "$dir/order")"
