"""The status object as Prometheus metrics (the text exposition format, version 0.0.4), for
`leasehold metrics` and `GET /metrics` in `leasehold ui`. Pure: status in, text out.

Every metric is a gauge of the moment the status was taken, labelled with the machine's name so
several machines can share one Prometheus.
"""

from __future__ import annotations

from collections.abc import Iterator

PREFIX = "leasehold"


def _escape(value: object) -> str:
    return str(value).replace("\\", r"\\").replace("\n", r"\n").replace('"', r"\"")


def _labels(**labels: object) -> str:
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items() if v is not None)
    return "{" + inner + "}" if inner else ""


class _Writer:
    def __init__(self, machine: str) -> None:
        self.machine = machine
        self.lines: list[str] = []
        self._declared: set[str] = set()

    def gauge(self, name: str, help_text: str, value: float | None, **labels: object) -> None:
        full = f"{PREFIX}_{name}"
        if full not in self._declared:
            self._declared.add(full)
            self.lines += [f"# HELP {full} {help_text}", f"# TYPE {full} gauge"]
        if value is None:
            return
        number = int(value) if isinstance(value, bool) else value
        self.lines.append(f"{full}{_labels(machine=self.machine, **labels)} {number}")


def _pools(w: _Writer, pools: dict) -> None:
    for name, p in pools.items():
        w.gauge("pool_max_slots", "The most slots the pool may hold.", p["max"], pool=name)
    for name, p in pools.items():
        w.gauge("pool_reserved_slots", "Slots only the pool may use.", p["reserved"], pool=name)
    for name, p in pools.items():
        w.gauge(
            "pool_held_slots", "Slots held in the pool, standing work included.",
            p["held_weight"], pool=name,
        )  # fmt: skip
    for name, p in pools.items():
        w.gauge("pool_running", "Leases held in the pool.", p["running"], pool=name)
    for name, p in pools.items():
        w.gauge("pool_queued", "Items waiting in the pool's line.", p["queued"], pool=name)
    for name, p in pools.items():
        w.gauge("pool_closed", "1 while the pool admits nothing.", p["closed"], pool=name)


def lines(status: dict) -> Iterator[str]:
    w = _Writer(status["machine"]["name"])
    w.gauge("slots", "The slots the machine's pools share.", status["slots"])
    w.gauge("held_slots", "Slots held, by leases and standing work.", status["held"])
    w.gauge(
        "overcommitted_slots", "Slots held beyond the machine's (by observed programs).",
        status["overcommitted"],
    )  # fmt: skip
    _pools(w, status["pools"])
    lend = status["lend"]
    w.gauge("lend_open", "1 while lend leases may be granted.", lend["eligible"])
    pressure = (status.get("load") or {}).get("pressure") or {}
    for resource in ("cpu", "memory", "io"):
        some = (pressure.get(resource) or {}).get("some") or {}
        w.gauge(
            "pressure_some_avg10_percent", "PSI some avg10: share of time tasks stalled.",
            some.get("avg10"), resource=resource,
        )  # fmt: skip
    waits = [status["time"] - q["queued_at"] for q in status["queue"] if q.get("queued_at")]
    w.gauge(
        "queue_oldest_wait_seconds", "How long the longest-waiting item has waited.",
        max(waits, default=0.0),
    )  # fmt: skip
    for rule in status.get("observe") or []:
        w.gauge(
            "observed_running", "Processes an [[observe]] rule counts now.",
            rule["running"], rule=rule["name"],
        )  # fmt: skip
    yield from w.lines


def render(status: dict) -> str:
    return "\n".join(lines(status)) + "\n"
