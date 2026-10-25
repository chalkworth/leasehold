"""Containment's names: what leasehold calls the trees it puts commands in, and how a user turns
containment off. How a tree is made is the host's (`leasehold.host`; on Linux a systemd user
scope, `leasehold.host.systemd`).

A lease's command runs in `lease-<id>.scope`, in `leasehold.slice`, or in the slice of the
`leasehold run` session it runs under (`leasehold-<session>_<token>.slice`, named to its
commands in `LEASEHOLD_SLICE`). `LEASEHOLD_CONTAIN=off` turns containment off for one command
and what it starts.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

#: The slice all of leasehold's scopes live under.
ROOT_SLICE = "leasehold.slice"
#: `LEASEHOLD_CONTAIN=off` turns containment off for one command and what it starts.
ENV = "LEASEHOLD_CONTAIN"
#: The session slice `leasehold run` made, for the scopes of the commands under it.
ENV_SLICE = "LEASEHOLD_SLICE"
#: How the reason a command is not contained starts when its caller runs under a memory limit
#: a scope would escape.
UNDER_LIMIT = "caller under a memory limit"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.]")
_SLICE = re.compile(r"^leasehold-[A-Za-z0-9_.]{1,100}\.slice$")


def describe(found) -> str:
    """How commands are held here, in words, from the host's support for `start`
    (`leasehold.host.Support`): `enforced (systemd)`, or `advisory: <why not>`."""
    return f"enforced ({found.via})" if found.how != "none" else f"advisory: {found.reason}"


def off(env: Mapping[str, str]) -> bool:
    """Whether `env` turns containment off (`LEASEHOLD_CONTAIN=off`)."""
    return env.get(ENV, "").lower() in ("off", "0", "no", "false")


def root_slice_cgroup(uid: int | None = None) -> str:
    """Where `ROOT_SLICE` is in the cgroup tree for user `uid` (default ours), relative to the
    cgroup root: under the user manager. Every contained command's scope is in it, a
    `leasehold run` session's slice included."""
    uid = os.getuid() if uid is None else uid
    return f"user.slice/user-{uid}.slice/user@{uid}.service/{ROOT_SLICE}"


def unit_part(name: str) -> str:
    """`name` made safe inside a unit name. A `-` in a slice name means nesting, so it goes."""
    return _UNSAFE.sub("_", name)[:80] or "_"


def session_slice(session: str, token: str) -> str:
    """The slice one `leasehold run` puts its session in: the session's name for people to
    read, and `token`, which is the run's own, so no two runs ever share a slice."""
    return f"leasehold-{unit_part(session)[:40]}_{unit_part(token)}.slice"


def is_session_slice(name: str) -> bool:
    return bool(_SLICE.match(name))


def agent_unit(session: str, token: str) -> str:
    """The scope `leasehold run` starts its agent in, in `session_slice(session, token)`."""
    return f"agent-{unit_part(session)[:40]}_{token}.scope"


def lease_unit(lease_id: str) -> str:
    """The scope a lease's command runs in: `lease-<id>.scope`."""
    return f"lease-{unit_part(lease_id)}.scope"


def lease_slice(env: Mapping[str, str] | None = None) -> str:
    """The slice a lease's scope goes in: the one `leasehold run` named in `LEASEHOLD_SLICE`,
    else `leasehold.slice`."""
    env = os.environ if env is None else env
    slice_ = env.get(ENV_SLICE) or ""
    return slice_ if is_session_slice(slice_) else ROOT_SLICE
