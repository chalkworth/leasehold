"""`status`, `leases`, `facts`, `ui` and `metrics`: looking at the machine. The first three
take `--json` for the same object the API returns."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from leasehold import weights
from leasehold.client import ApiError, Client, Unavailable
from leasehold.facts import probe


def register(sub) -> None:
    s = sub.add_parser("status", help="the machine: pools, load, lending, leases and the queue")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=_status)

    s = sub.add_parser("leases", help="the leases held and the queue")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=_leases)

    s = sub.add_parser("facts", help="this machine's facts (no daemon needed)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(run=_facts)

    s = sub.add_parser(
        "ui",
        help="the machine in your browser, on a private loopback link",
        description="The machine in your browser, on a private loopback link. "
        "Experimental: may change before 1.0.",
    )
    s.add_argument("--port", type=int, default=0, help="the port (default: any free one)")
    s.add_argument("--no-open", action="store_true", help="print the link, open no browser")
    s.set_defaults(run=_ui)

    s = sub.add_parser(
        "metrics",
        help="the status as Prometheus metrics",
        description="The status as Prometheus metrics. Experimental: may change before 1.0.",
    )
    s.add_argument(
        "--textfile",
        metavar="DIR",
        help="write DIR/leasehold.prom for node_exporter's textfile collector, and exit",
    )
    s.set_defaults(run=_metrics)


def _status(args, ctx) -> int:
    try:
        st = ctx.client().status()
    except (Unavailable, ApiError) as exc:
        return ctx.unreachable(exc)
    print(json.dumps(st, indent=2)) if args.json else _print_status(st)
    return 0


def _leases(args, ctx) -> int:
    try:
        listing = ctx.client().leases()
    except (Unavailable, ApiError) as exc:
        return ctx.unreachable(exc)
    print(json.dumps(listing, indent=2)) if args.json else _print_leases(listing, time.time())
    return 0


def _ui(args, ctx) -> int:
    from leasehold.webui import UiServer, open_browser

    socket = ctx.socket_path()
    try:
        server = UiServer(lambda: Client(socket), port=args.port)
    except OSError as exc:
        sys.stderr.write(f"leasehold: cannot listen on 127.0.0.1:{args.port}: {exc}\n")
        return 1
    print(f"leasehold ui: {server.url}")
    print("  the link carries this run's access token; keep it to yourself. Ctrl-C stops it.")
    sys.stdout.flush()
    if not args.no_open:
        open_browser(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _metrics(args, ctx) -> int:
    from leasehold.metrics import render

    try:
        text = render(ctx.client().status())
    except (Unavailable, ApiError) as exc:
        return ctx.unreachable(exc)
    if not args.textfile:
        sys.stdout.write(text)
        return 0
    directory = Path(args.textfile)
    target = directory / "leasehold.prom"
    try:
        # A new file of a name nobody can guess (O_EXCL), renamed over the target, so
        # node_exporter never reads half a file and no planted link is written through.
        fd, partial = tempfile.mkstemp(prefix=".leasehold.prom.", dir=directory)
        try:
            with os.fdopen(fd, "w") as out:
                os.fchmod(out.fileno(), 0o644)  # whatever the umask: node_exporter reads it
                out.write(text)
            os.replace(partial, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(partial)
            raise
    except OSError as exc:
        sys.stderr.write(f"leasehold: cannot write {target}: {exc}\n")
        return 1
    return 0


def _facts(args, ctx) -> int:
    facts = probe()
    if args.json:
        print(json.dumps(facts, indent=2))
    else:
        for key, value in facts.items():
            print(f"{key}: {value}")
    return 0


def _ago(ts: float | None, now: float) -> str:
    return "-" if ts is None else f"{ts - now:+.0f}s"


def _print_leases(listing: dict, now: float) -> None:
    rows = [("LEASE", "KIND", "POOL", "W", "USER", "SESSION", "EXPIRES", "STATE")]
    for x in listing["leases"]:
        state = f"revoked: {x['revoke_reason']}" if x["revoked"] else "held"
        rows.append(_row(x, now, state))
    for x in listing["queue"]:
        state = f"queued #{x.get('position')}"
        if x.get("blocked"):
            state += f": {x['blocked']}"
        rows.append(_row(x, now, state))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip())


def _row(x: dict, now: float, state: str) -> tuple[str, ...]:
    return (
        x["lease_id"],  # whole: it is what `release` and `keep` take
        x["kind"],
        x["pool"],
        _weight(x),
        x["user"] or "-",
        x.get("session") or "-",
        _ago(x["expires_at"], now),
        state,
    )


def _weight(x: dict) -> str:
    """A lease's weight (`3`, or `3/2-4`: granted 3 of the range 2-4), or a waiting item's
    (`2`, or `2-4`)."""
    least = x.get("min_weight") or x["weight"]
    most = x.get("max_weight") or least
    if most == least:
        return str(x["weight"])
    asked = weights.text(least, most)
    return asked if "queued_at" in x else f"{x['weight']}/{asked}"


def _print_standing(st: dict) -> None:
    if not st.get("standing"):
        return
    print("standing:")
    for item in st["standing"]:
        extra = []
        if item.get("cpus") is not None:
            extra.append(f"{item['cpus']:g} cpus")
        if item.get("memory_bytes"):
            extra.append(f"{item['memory_bytes'] / 2**30:.1f} GiB")
        if item.get("note"):
            extra.append(str(item["note"]))
        tail = ("  " + "  ".join(extra)) if extra else ""
        name, kind, pool = item["name"], item["kind"], item["pool"]
        print(f"  {name:<12} {kind:<10} {pool:<10} weight {item['weight']}{tail}")


def _print_status(st: dict) -> None:
    m, f, c = st["machine"], st["facts"], st["ceiling"]
    cpus = f.get("cpus_logical")
    print(f"machine {m['name']}  {f.get('arch')}  {cpus} cpus  leasehold {st['version']}")
    over = f", {st['overcommitted']} over" if st.get("overcommitted") else ""
    print(
        f"slots {st['held']}/{c['slots']} held{over}; at most {c['cpus']:g} cpus and "
        f"{c['memory_bytes'] / 2**30:.1f} GiB per lease"
    )
    memory = st.get("memory") or {}
    if memory.get("counted"):
        gib = 2**30
        free = memory.get("free_now_bytes")
        short = f"; memory short, {free / gib:.1f} GiB may start now" if free is not None else ""
        print(
            f"memory {memory['held_bytes'] / gib:.1f}/{memory['budget_bytes'] / gib:.1f} GiB "
            f"counted ({memory['kept_bytes'] / gib:.1f} GiB kept for you; "
            f"{memory['assumed_per_slot_bytes'] / gib:.1f} GiB a slot when none is declared)"
            f"{short}"
        )
    load = st["load"] or {}
    pressure = load.get("pressure") or {}
    psi = "  ".join(
        f"{r} {pressure[r]['some']['avg10']:g}%" for r in ("cpu", "memory", "io") if pressure.get(r)
    )
    print(f"load {load.get('loadavg')}  pressure {psi or 'unknown (no PSI)'}")
    lend = st["lend"]
    print(f"lend {'open' if lend['eligible'] else 'closed'}: {lend['reason']}")
    print("pools:")
    for name, p in st["pools"].items():
        flags = " preemptible" if p["preemptible"] else ""
        flags += " observed" if p.get("observed") else ""
        flags += " closed" if p["closed"] else ""
        flags += f" {p['overcommitted']} over" if p.get("overcommitted") else ""
        shape = f"reserved {p['reserved']}"
        shape += f", headroom {p['headroom']}" if p.get("headroom") else ""
        print(
            f"  {name:<10} {p['held_weight']}/{p['max']} held ({shape}), "
            f"{p['running']} running, {p['queued']} queued{flags}"
        )
    _print_standing(st)
    if st["leases"] or st["queue"]:
        _print_leases(st, st["time"])
