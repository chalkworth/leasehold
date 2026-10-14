"""Who may administer the machine agent (leasehold.access): root, the daemon's own user and the
members of `admin_group`; an unknown peer never."""

from __future__ import annotations

import grp
import os
import pwd

from leasehold.access import in_group, may_administer

DAEMON = 1000


def member(user: str, group: str) -> bool:
    return (user, group) == ("ann", "operators")


def admin(uid: int | None, user: str | None, group: str | None = "operators") -> bool:
    return may_administer(uid, user, admin_group=group, daemon_uid=DAEMON, member=member)


def test_root_and_the_daemons_own_user_are_administrators():
    assert admin(0, "root", group=None)
    assert admin(DAEMON, "svc", group=None)


def test_a_member_of_the_admin_group_is_an_administrator_and_others_are_not():
    assert admin(1001, "ann")
    assert not admin(1002, "bob")
    assert not admin(1001, "ann", group=None)  # no admin group: only root and the daemon


def test_an_unknown_peer_is_never_an_administrator():
    assert not admin(None, "ann")
    assert not admin(None, "root")
    assert not admin(1003, None)


def test_the_daemons_uid_defaults_to_this_process():
    assert may_administer(os.getuid(), None, admin_group=None)


def test_membership_counts_the_primary_group_and_the_supplementary_ones():
    me = pwd.getpwuid(os.getuid())
    for gid in os.getgrouplist(me.pw_name, me.pw_gid):
        assert in_group(me.pw_name, grp.getgrgid(gid).gr_name)
    assert not in_group(me.pw_name, "no-such-group-here")
    assert not in_group("no-such-user-here", grp.getgrgid(me.pw_gid).gr_name)
