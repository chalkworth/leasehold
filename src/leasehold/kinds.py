"""What a lease is for.

`work` is the machine owner's own work, `external` someone else's bounded job (a CI runner, a
test gate), `lend` spare capacity lent out until the machine needs it back, and `observed` a
known program seen running that never asked for a lease (only the daemon makes these).
"""

from __future__ import annotations

from typing import Literal

LeaseKind = Literal["work", "external", "lend", "observed"]
KINDS: tuple[str, ...] = ("work", "external", "lend", "observed")
#: The only kind that is ever revoked.
PREEMPTIBLE: frozenset[str] = frozenset({"lend"})
#: Kinds a client may ask for. `observed` leases are made by the machine agent alone.
GRANTED: frozenset[str] = frozenset({"work", "external", "lend"})
