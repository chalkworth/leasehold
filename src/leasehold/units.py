"""Memory sizes as Docker writes them (`3g`, `512m`, `1048576b`)."""

from __future__ import annotations

import re

#: The largest memory size taken, 1 EiB: more than any machine has, and within what SQLite
#: stores as an integer.
MEMORY_MAX = 1 << 60

_MEMORY_SUFFIXES: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "kb": 1024,
    "m": 1024**2,
    "mb": 1024**2,
    "g": 1024**3,
    "gb": 1024**3,
    "t": 1024**4,
    "tb": 1024**4,
}


def memory_to_bytes(memory: str) -> int:
    """Convert a Docker memory string to bytes, with binary suffixes.

    ``1k`` = 1024, ``512m`` = 536870912, ``3g`` = 3221225472; the doubled forms
    ``kb``/``mb``/``gb``/``tb`` mean the same as the single letters (Docker's
    ``RAMInBytes`` behaviour). A bare number, an unknown suffix or a size over
    `MEMORY_MAX` is an error.
    """
    match = re.fullmatch(r"([0-9]+)(b|k|m|g|t|kb|mb|gb|tb)", memory)
    if match is None:
        raise ValueError(
            f"memory {memory!r} is not a byte count with a lowercase suffix "
            "(e.g. '3g', '512m', '1048576b')"
        )
    size = int(match.group(1)) * _MEMORY_SUFFIXES[match.group(2)]
    if size > MEMORY_MAX:
        raise ValueError(f"memory {memory!r} is more than 1 EiB, more than any machine has")
    return size


def declared_to_bytes(memory: str) -> int:
    """A size a command declares it will use (`hold --memory`, a rule's `memory`, a request's
    `memory`): as `memory_to_bytes`, and more than 0, since a contained command is held to it
    (`MemoryHigh`) and 0 would hold it to no memory at all."""
    size = memory_to_bytes(memory)
    if size == 0:
        raise ValueError(f"memory {memory!r} is 0: a command cannot be held to no memory")
    return size
