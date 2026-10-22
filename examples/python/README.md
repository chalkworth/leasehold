# Taking leases from Python

`take.py` takes leases through the Python API instead of running `leasehold hold`. It asks for a
range, runs a command under what it was granted, then shows a wait that gives up and a wait
cancelled from another thread.

Start a daemon of your own on this directory's `machine.toml`, then run the script:

```sh
export LEASEHOLD_CONFIG=$PWD/machine.toml
uv tool install leasehold             # for the `leasehold` command
leasehold serve &
uv run --with leasehold python take.py
```

`uv tool install` gives you the `leasehold` command only; `take.py` imports the package, which
`uv run --with leasehold` installs for it (Python 3.12 or later).

`LEASEHOLD_CONFIG` names the config for `serve` and tells the clients, `take.py` included, to use
its daemon's socket. `serve` refuses to start if a daemon of yours already answers on that
socket: stop it first.

The output:

```
daemon: /run/user/1000/leasehold/leasehold.sock
granted 3 of 2-4 slots in pool work, 3 jobs
  the command sees 3 slots
the command exited 0
  waiting, place 1
busy after 2s: gave up after 2s in line
cancelled: the place in line was given back
released; the daemon now holds 0 leases
```

The range of 2 to 4 slots is granted 3, not 4: the slots a range takes beyond its minimum never
include a pool's last one (its [headroom](../../docs/reference.md#ranges-min-max-slots)). The
command started by `lease.run` sees the lease in its environment, sized to the 3 slots. With 1
slot left, a lease of 2 waits in line: `on_wait` hears its place, and after `timeout` it raises
`Busy` and gives the place back. The last wait is cancelled by setting the `threading.Event`
passed as `cancel`, from a timer thread. Leaving the `with` block releases the range.

The [reference](../../docs/reference.md#from-python) covers the rest: the other errors, what
happens when no daemon answers, revocation, and a fake daemon for tests.
