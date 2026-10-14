# Lending idle capacity

A `lend` lease runs spare work (a batch job, a volunteer-computing client) only while the machine
is idle, and is revoked the moment the machine needs its slots back. It is the only kind of lease
leasehold ever preempts.

Start a daemon of your own on this directory's `machine.toml`, then run the script:

```sh
export LEASEHOLD_CONFIG=$PWD/machine.toml
leasehold serve &
./run.sh
```

`LEASEHOLD_CONFIG` names the config for `serve` and tells the clients to use its daemon's socket.
`serve` refuses to start if a daemon of yours already answers on that socket: stop it first.

`run.sh` does three things:

1. `leasehold lend on`, then the batch job asks for the 2 lend slots:

   ```sh
   leasehold hold --kind lend --weight 2 --ttl 3 -- ./batch-job.sh
   ```

   The lease is granted once nothing else waits, CPU and memory pressure are low, and both have
   been so for `quiet_s` (10 s here). `leasehold status` then says `lend open: idle for ...` and
   lists the lease. If the job keeps waiting, `status` says why (`lend closed: cpu pressure ...`).
   `batch-job.sh` runs `stress-ng --cpu 2` when stress-ng is installed, and `sleep 600` when not.

2. The owner's build arrives and needs all 4 slots:

   ```sh
   leasehold hold --weight 4 -- echo "the owner's build runs on all 4 slots"
   ```

   It cannot fit beside the lent 2, so it waits, and work waiting revokes every lend lease in the
   same call. The batch job's `hold` hears it at its next renewal (every TTL/3, so every second
   with `--ttl 3`), sends the job SIGTERM, and exits 76. Once the job has stopped, the build runs.
   High CPU or memory pressure revokes lending the same way, checked every `tick_s`.

3. `leasehold lend off` closes the lend pool and revokes what it holds, until `leasehold lend on`.
   Only administrators may switch it: root, the daemon's own user, and members of `admin_group`.

The end of the output:

```
leasehold hold: waiting for 4 slots (work): place 1; 2 of 4 slots are free; this needs 4
the owner's build runs on all 4 slots
the batch job's hold exited 76 (76: revoked)
batch job started
leasehold hold: lease revoked (non-lend work waiting); stopping the command
lending off: lending switched off
```

## On a real machine

Run lent work in `lend.slice` (`deploy/lend.slice`: `CPUWeight=idle`, a `MemoryMax`), so the
kernel gives it only CPU nobody else wants, and its memory is capped while it stops:

```sh
sudo systemd-run --slice=lend.slice --uid="$USER" --gid="$(id -gn)" --same-dir --pty --wait \
  --collect -E HOME="$HOME" -E PATH="$PATH" \
  leasehold hold --kind lend --weight 2 -- ./batch-job.sh
```

By default pressure is measured over the whole machine, so the lent job's own load counts
against it. With lent work in `lend.slice`, set `pressure_cgroups` (commented out in
`machine.toml`) to the other slices, and only their pressure revokes lending. Keep `quiet_s` at
minutes, not seconds, so a short pause in your own work does not hand the machine away.
