"""What a lease is for.

`work` is the machine owner's own work, `external` someone else's bounded job (a CI runner, a
test gate), `lend` spare capacity lent out until the machine needs it back, `maintenance` the
whole machine taken for a while (a benchmark window, an upgrade), and `observed` a known program
seen running that never asked for a lease (only the daemon makes these).
"""

from __future__ import annotations

from typing import Literal

LeaseKind = Literal["work", "external", "lend", "maintenance", "observed"]
KINDS: tuple[str, ...] = ("work", "external", "lend", "maintenance", "observed")
#: The only kind that is ever revoked.
PREEMPTIBLE: frozenset[str] = frozenset({"lend"})
#: Kinds a client may ask for. `observed` leases are made by the machine agent alone.
GRANTED: frozenset[str] = frozenset({"work", "external", "lend", "maintenance"})
#: Kinds that take the whole machine: admission drains it first (`leasehold.admission`).
WHOLE: frozenset[str] = frozenset({"maintenance"})
