"""Who may do what on the machine agent's socket.

Anyone who can open the socket may take, renew and give back their own leases. Switching lending
and changing pool limits change what everyone else gets, so only an administrator may: root, the
user the daemon runs as, or a member of the config's `admin_group`.
"""

from __future__ import annotations

import grp
import os
import pwd
from collections.abc import Callable


class Forbidden(PermissionError):
    """A request the asking user may not make."""


def in_group(user: str, group: str) -> bool:
    """Whether `user` is in `group`, as the system's name service says (files, SSSD, LDAP):
    its primary group or a supplementary one. An unknown user or group is never."""
    try:
        gid = grp.getgrnam(group).gr_gid
        return gid in os.getgrouplist(user, pwd.getpwnam(user).pw_gid)
    except (KeyError, OSError):
        return False


def may_administer(
    uid: int | None,
    user: str | None,
    *,
    admin_group: str | None,
    daemon_uid: int | None = None,
    member: Callable[[str, str], bool] = in_group,
) -> bool:
    """Whether the peer (`uid`, `user`) is an administrator. An unknown peer never is."""
    if uid is None:
        return False
    if uid == 0 or uid == (os.getuid() if daemon_uid is None else daemon_uid):
        return True
    return admin_group is not None and user is not None and member(user, admin_group)
