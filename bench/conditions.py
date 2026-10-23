"""The benchmark's conditions by name, with parameters for leasehold's, and the config each
leasehold condition's daemon runs on.

A condition is a name from `CONDITIONS`, optionally followed by `@` and parameters:

    lh-enforced@w=3-3,h=1,kept=3
    lh-enforced@w=2-4,mem=declared
    lh-enforced@w=4,arm=b
    lh-elastic@w=2-8,grow=leave-one

* `w`: what every job asks `hold --weight` for, `N` or `MIN-MAX`; with `N`, also the config's
  `[commands] heavy_weight`. Without it, jobs ask for `--weights`.
* `h`: `[pools.work] headroom`, a count of slots.
* `grow`: `[elastic] headroom`, how much of the pools' headroom an elastic lease's growth
  leaves free: `keep` (all of it), `leave-one` or `use` (none). Only `lh-elastic` takes it.
  `lh-jobserver` asks `hold --no-elastic` where `hold` has it, so its ranges keep the size
  they were granted.
* `kept`: CPUs kept back from the slots. The config is rendered for `cpus - kept` slots, which
  sets the ceiling, the pools' `max` and the work pool's reservation as `init` would.
* `mem=declared`: each `mem` job declares its size plus 512 MiB (`hold --memory`).
* `budget`: `[memory] budget`, a size such as `7g` (otherwise `--memory-budget`, else auto).
* `park`: run the harness's parker (`bench/park.py`), an experiment that pauses a `mem` job and
  pushes its memory to swap when memory inside the limit runs out. `freeze` parks once a job
  spills past its own small swap allowance (`overflow`); `threshold` parks at 85% of the limit.
  Only `lh-enforced` takes it, as its jobs run in lease scopes the parker can freeze.
* `parkcap`: the most a `park` run may reclaim, a size such as `24g` (the default). Only with
  `park`.
* `hold=parked`: a `mem` job waits to start while the parker has a job parked. Only with
  `park=freeze`.
* `overflow`: each running `mem` job's `memory.swap.max`, a size such as `1g`, the default
  under `park=freeze`. Without `park` it sets the swap limit and parks nothing. Not with
  `park=threshold`.
* `arm`: a tag that changes nothing, so that one condition can run as two arms (an A/A run).

Only the leasehold conditions (`lh-...`, except `lh-enforced-v020`) take parameters. In a
comma-separated list, a part with `=` and no `@` belongs to the condition before it, so
`--conditions unmanaged,lh-enforced@w=2-4,h=1` is two conditions. `Condition.text` is the
canonical spelling (parameters in a fixed order, `w=3-3` as `w=3`), the one results and gate
files use; `Condition.slug` is the same as a directory name.

Standard library only, apart from `config`, which renders through `leasehold.template` when
`kept` is given and for `lh-enforced-v020`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field

CONDITIONS = {
    "unmanaged": "jobs started as they are, in the same cgroup as the person's shell",
    "nice": "jobs under nice -n 19, same cgroup",
    "slice": "jobs in one systemd user slice with a low CPUWeight",
    "lh-advisory": "leasehold hold, containment off: lease and job count only",
    "lh-enforced": "leasehold hold, containment on: weighted scope per lease",
    "lh-advisory-greedy": "as lh-advisory, jobs ignore the job count and use every CPU",
    "lh-enforced-greedy": "as lh-enforced, jobs ignore the job count and use every CPU",
    "lh-hardcpu-greedy": "as lh-enforced-greedy, with [contain] hard_cpu = true",
    "static-jobs": "unmanaged, each job told the job count its lease would give",
    "lh-enforced-declared": "as lh-enforced, mem jobs declare their memory (hold --memory)",
    "lh-enforced-v020": "as lh-enforced, on leasehold 0.2.0's generated config and weights",
    "lh-jobserver": "as lh-enforced, through hold --jobserver, jobs are jobserver clients",
    "lh-elastic": "as lh-jobserver, with elastic leases (hold --elastic); needs leasehold "
    "with elastic leases",
}
#: The conditions that run by default: all but those that need a jobserver.
DEFAULT_CONDITIONS = [c for c in CONDITIONS if c not in ("lh-jobserver", "lh-elastic")]
#: The parameters, in their canonical order.
KEYS = (
    "w",
    "h",
    "grow",
    "kept",
    "mem",
    "budget",
    "park",
    "parkcap",
    "overflow",
    "hold",
    "arm",
)
_WEIGHT = re.compile(r"^([0-9]+)(?:-([0-9]+))?$")
_SIZE = re.compile(r"^[0-9]+(\.[0-9]+)?[kmgt]?b?$", re.IGNORECASE)
#: The choices of `grow`, the same as `leasehold.elastic.HEADROOM_MODES`.
GROW_MODES = ("keep", "leave-one", "use")
_TAG = re.compile(r"^[A-Za-z0-9]+$")
#: What `mem=declared` declares for a mem job beyond the block it touches.
DECLARED_EXTRA = 512 * (1 << 20)
#: The choices of `park`.
PARK_MODES = ("freeze", "threshold")
_UNITS = {"": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}


def size_bytes(text: str) -> int:
    """A size such as `24g` (binary units, an optional `b`) in bytes."""
    text = text.strip().lower().removesuffix("b")
    unit = text[-1] if text and text[-1] in _UNITS else ""
    return int(float(text.removesuffix(unit) if unit else text) * _UNITS[unit])


def parse_weight(text: str) -> tuple[int, int]:
    """`N` as (N, N), `MIN-MAX` as (MIN, MAX); a ValueError unless 1 <= MIN <= MAX."""
    match = _WEIGHT.match(text.strip())
    if match is None:
        raise ValueError(f"w={text!r}: a weight is N or MIN-MAX, e.g. 4 or 2-4")
    least = int(match.group(1))
    most = least if match.group(2) is None else int(match.group(2))
    if not 1 <= least <= most:
        raise ValueError(f"w={text!r}: a weight needs 1 <= MIN <= MAX")
    return least, most


def weight_text(weight: tuple[int, int]) -> str:
    least, most = weight
    return str(least) if least == most else f"{least}-{most}"


@dataclass(frozen=True)
class Condition:
    """A condition: its base name and its parameters, each already checked."""

    base: str
    weight: tuple[int, int] | None = None
    headroom: int | None = None
    grow: str | None = None
    kept: int | None = None
    declared: bool = False
    budget: str | None = None
    park: str | None = None
    park_cap: str | None = None
    overflow: str | None = None
    hold: str | None = None
    arm: str | None = None
    params: dict[str, str] = field(default_factory=dict, compare=False, hash=False)

    @property
    def text(self) -> str:
        parts = self.canonical_params()
        return self.base + ("@" + ",".join(f"{k}={v}" for k, v in parts) if parts else "")

    @property
    def slug(self) -> str:
        parts = self.canonical_params()
        return "_".join([self.base, *(f"{k}{v}" for k, v in parts)])

    def canonical_params(self) -> list[tuple[str, str]]:
        values = {
            "w": weight_text(self.weight) if self.weight else None,
            "h": None if self.headroom is None else str(self.headroom),
            "grow": self.grow,
            "kept": None if self.kept is None else str(self.kept),
            "mem": "declared" if self.declared and self.base != "lh-enforced-declared" else None,
            "budget": self.budget,
            "park": self.park,
            "parkcap": self.park_cap,
            "overflow": self.overflow,
            "hold": self.hold,
            "arm": self.arm,
        }
        return [(k, values[k]) for k in KEYS if values[k] is not None]

    @property
    def parker(self) -> str | None:
        """The mode the harness's parker runs in (`bench/park.py`), or None for no parker."""
        return self.park or ("off" if self.overflow else None)

    @property
    def leasehold(self) -> bool:
        return self.base.startswith("lh-")

    @property
    def contained(self) -> bool:
        """Whether the jobs run in lease scopes under a session slice of the benchmark's."""
        return self.base.startswith(("lh-enforced", "lh-hardcpu")) or self.base in (
            "lh-jobserver",
            "lh-elastic",
        )

    @property
    def jobserver(self) -> bool:
        return self.base in ("lh-jobserver", "lh-elastic")

    @property
    def elastic(self) -> bool:
        return self.base == "lh-elastic"

    @property
    def greedy(self) -> bool:
        return self.base.endswith("-greedy")

    @property
    def describe(self) -> str:
        about = CONDITIONS[self.base]
        parts = self.canonical_params()
        return about + (f" ({', '.join(f'{k}={v}' for k, v in parts)})" if parts else "")


def parse(text: str) -> Condition:
    """One condition, `NAME` or `NAME@k=v,...`; a ValueError naming what is wrong."""
    base, at, rest = text.strip().partition("@")
    if base not in CONDITIONS:
        raise ValueError(f"unknown condition {base!r}")
    if not at:
        return Condition(base, declared=base == "lh-enforced-declared")
    if not base.startswith("lh-") or base == "lh-enforced-v020":
        raise ValueError(f"{base} takes no parameters")
    params: dict[str, str] = {}
    for part in rest.split(","):
        key, eq, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not value:
            raise ValueError(f"{text!r}: {part!r} is not key=value")
        if key not in KEYS:
            raise ValueError(f"{text!r}: unknown parameter {key!r} (known: {', '.join(KEYS)})")
        if key in params:
            raise ValueError(f"{text!r}: {key} given twice")
        params[key] = value
    weight = parse_weight(params["w"]) if "w" in params else None
    headroom = _count(text, "h", params.get("h"))
    kept = _count(text, "kept", params.get("kept"))
    grow = params.get("grow")
    if grow is not None and base != "lh-elastic":
        raise ValueError(f"{text!r}: only lh-elastic takes grow")
    if grow is not None and grow not in GROW_MODES:
        raise ValueError(f"{text!r}: grow is one of {', '.join(GROW_MODES)}")
    mem = params.get("mem")
    if mem not in (None, "declared", "assumed"):
        raise ValueError(f"{text!r}: mem is 'declared' or 'assumed'")
    budget = params.get("budget")
    if budget is not None and not _SIZE.match(budget):
        raise ValueError(f"{text!r}: budget={budget!r} is not a size such as 7g")
    park = params.get("park")
    if park is not None and base != "lh-enforced":
        raise ValueError(f"{text!r}: only lh-enforced takes park")
    if park is not None and park not in PARK_MODES:
        raise ValueError(f"{text!r}: park is one of {', '.join(PARK_MODES)}")
    park_cap = params.get("parkcap")
    if park_cap is not None and park is None:
        raise ValueError(f"{text!r}: parkcap needs park")
    if park_cap is not None and (not _SIZE.match(park_cap) or size_bytes(park_cap) <= 0):
        raise ValueError(f"{text!r}: parkcap={park_cap!r} is not a size such as 24g")
    overflow = params.get("overflow")
    if overflow is not None and base != "lh-enforced":
        raise ValueError(f"{text!r}: only lh-enforced takes overflow")
    if overflow is not None and park == "threshold":
        raise ValueError(f"{text!r}: park=threshold takes no overflow")
    if overflow is not None and (not _SIZE.match(overflow) or size_bytes(overflow) <= 0):
        raise ValueError(f"{text!r}: overflow={overflow!r} is not a size such as 1g")
    hold = params.get("hold")
    if hold is not None and hold != "parked":
        raise ValueError(f"{text!r}: hold is 'parked'")
    if hold is not None and park != "freeze":
        raise ValueError(f"{text!r}: hold=parked needs park=freeze")
    arm = params.get("arm")
    if arm is not None and not _TAG.match(arm):
        raise ValueError(f"{text!r}: arm={arm!r} is letters and digits only")
    return Condition(
        base,
        weight=weight,
        headroom=headroom,
        grow=grow,
        kept=kept,
        declared=mem == "declared" or base == "lh-enforced-declared",
        budget=budget,
        park=park,
        park_cap=park_cap,
        overflow=overflow,
        hold=hold,
        arm=arm,
        params=params,
    )


def _count(text: str, key: str, value: str | None) -> int | None:
    if value is None:
        return None
    if not value.isdigit():
        raise ValueError(f"{text!r}: {key}={value!r} is not a count of 0 or more")
    return int(value)


def split(text: str) -> list[str]:
    """The conditions in a comma-separated list. A part with `=` and no `@` is another
    parameter of the condition before it."""
    out: list[str] = []
    for part in filter(None, (p.strip() for p in text.split(","))):
        if "=" in part and "@" not in part:
            if not out or "@" not in out[-1]:
                raise ValueError(f"{part!r} follows no condition with parameters")
            out[-1] += "," + part
        else:
            out.append(part)
    return out


def parse_list(text: str) -> list[Condition]:
    """The conditions in a comma-separated list, each once (by canonical spelling)."""
    conditions = [parse(c) for c in split(text)]
    seen: set[str] = set()
    for c in conditions:
        if c.text in seen:
            raise ValueError(f"{c.text} is listed twice; tell two arms apart with arm=")
        seen.add(c.text)
    return conditions


# ---------------------------------------------------------------------------------------------
# The config a leasehold condition's daemon runs on


def neutral(text: str) -> str:
    """The config `init` wrote, named `bench` instead of after the machine, so that nothing the
    benchmark writes names its host, without the weight lines `init` writes to run two heavy
    commands at once: the arm alone sets the weights."""
    name = tomllib.loads(text).get("name")
    lines = text.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.startswith("name = "):
            line = 'name = "bench"\n'
        elif name and line.startswith("# leasehold config for your own daemon on "):
            line = line.replace(str(name), "bench")
        elif line.startswith(("heavy_weight = ", "light_weight = ")):
            continue
        out.append(line)
    return "".join(out)


def _insert_after(text: str, header: str, line: str) -> str:
    marker = f"{header}\n"
    if marker not in text:
        raise ValueError(f"the generated config has no {header} table")
    return text.replace(marker, f"{marker}{line}\n", 1)


def config(
    init_text: str,
    condition: Condition,
    *,
    cpus: int,
    ledger: str,
    memory_budget: str | None = None,
) -> str:
    """The config the daemon of `condition` runs on: `init_text` (what `leasehold init --user
    --print` writes for this machine) with the condition's parameters applied, its ledger at
    `ledger`. A ValueError if the generated config cannot take them."""
    text = neutral(init_text)
    raw = tomllib.loads(text)
    if condition.base == "lh-enforced-v020":
        from leasehold.template import render_user_0_2

        about = text.splitlines()[1].removeprefix("# ")
        c = raw["ceiling"]
        text = render_user_0_2(
            name=raw["name"], slots=c["slots"], cpus=c["cpus"], memory=c["memory"], about=about,
            release="0.2.0",
        )  # fmt: skip
    elif condition.kept is not None:
        from leasehold.template import render

        if not 0 <= condition.kept < cpus:
            raise ValueError(f"kept={condition.kept} leaves no slot of {cpus} CPUs")
        slots = cpus - condition.kept
        text = render(
            name="bench",
            mode="user",
            slots=slots,
            cpus=slots,
            memory=raw["ceiling"]["memory"],
            two_at_once=False,
            about=f"Sized for {cpus} CPUs: one slot per CPU; {condition.kept} CPUs are kept "
            "back by the benchmark.",
        )
    budget = condition.budget or (memory_budget if condition.base != "lh-enforced-v020" else None)
    if budget:
        line = 'budget = "auto"'
        if line not in text:
            raise ValueError("the generated config has no [memory] budget to replace")
        text = text.replace(line, f'budget = "{budget}"', 1)
    if condition.headroom is not None:
        text = _insert_after(text, "[pools.work]", f"headroom = {condition.headroom}")
    if condition.grow is not None:
        table = text.find("\n[elastic]\n")
        line = 'headroom = "keep"'
        at = text.find(line, table) if table >= 0 else -1
        if at < 0:
            raise ValueError('the generated config has no [elastic] headroom = "keep" to replace')
        text = text[:at] + f'headroom = "{condition.grow}"' + text[at + len(line) :]
    if condition.weight is not None:
        least, most = condition.weight
        line = (
            f"heavy_weight = {least}"
            if least == most
            else f"# the benchmark's jobs ask hold --weight {least}-{most}"
        )
        text = _insert_after(text, "[commands]", line)
    if condition.base == "lh-hardcpu-greedy":
        if "hard_cpu = false" not in text:
            raise ValueError("the generated config has no hard_cpu line to turn on")
        text = text.replace("hard_cpu = false", "hard_cpu = true", 1)
    marker = 'mode = "user"'
    if marker not in text:
        raise ValueError("the generated config has no mode line to place the ledger after")
    return text.replace(marker, f'{marker}\nledger_dir = "{ledger}"', 1)


def summary(text: str) -> dict:
    """What a condition's config decides, from its text: slots, the work pool, memory, rules."""
    raw = tomllib.loads(text)
    work = raw.get("pools", {}).get("work", {})
    return {
        "slots": raw["ceiling"]["slots"],
        "cpus": raw["ceiling"]["cpus"],
        "work_reserved": work.get("reserved"),
        "work_max": work.get("max"),
        "work_headroom": work.get("headroom", "auto"),
        "memory_budget": raw.get("memory", {}).get("budget"),
        "heavy_weight": raw.get("commands", {}).get("heavy_weight", 4),
        "hard_cpu": raw.get("contain", {}).get("hard_cpu", False),
        "elastic_headroom": raw.get("elastic", {}).get("headroom", "keep"),
    }
