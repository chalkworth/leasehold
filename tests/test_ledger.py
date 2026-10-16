"""The machine-wide ledger (leasehold.ledger): one transaction per decision, so racing processes
never over-admit; dead or reused-pid holders are reclaimed; TTL leases lapse unless renewed; a
lease can be imposed beyond the pools' limits; each waiting item's place and blocker can be
read; the file is transient; and an unusable directory is an error, never a silent pass."""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from support import Clock

from leasehold import ledger as ledger_module
from leasehold.admission import AdmissionPolicy, PoolPolicy
from leasehold.ledger import (
    SCHEMA_VERSION,
    STALE_S,
    Admitted,
    Holder,
    Ledger,
    LedgerItem,
    LedgerUnavailable,
    Queued,
    Refused,
)
from leasehold.model import MACHINE
from leasehold.process import process_birth

#: Three slots: `work` reserves one, `ci` may use all three but two per user.
POLICY = AdmissionPolicy(
    slots={MACHINE: 3},
    pools={
        "work": PoolPolicy(MACHINE, reserved=1, max=3, priority=0),
        "ci": PoolPolicy(MACHINE, reserved=0, max=3, priority=2, per_user_max_running=2),
    },
)
#: One slot, one pool.
ONE = AdmissionPolicy(slots={MACHINE: 1}, pools={"ci": PoolPolicy(MACHINE, 0, 1)})


def item(
    item_id: str,
    pool: str = "ci",
    user: str | None = "ann",
    weight: int = 1,
    scope: str = "ci",
    kind: str = "work",
    session: str | None = None,
) -> LedgerItem:
    return LedgerItem(
        scope=scope,
        item_id=item_id,
        kind=kind,
        user=user,
        pool=pool,
        weight=weight,
        session=session,
    )


def position(outcome) -> int:
    """The place in line of an outcome that must be `Queued`."""
    assert isinstance(outcome, Queued), outcome
    return outcome.position


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


def dead_holder() -> Holder:
    """A process that existed, with its real birth token, and has since exited."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    birth = process_birth(proc.pid)
    proc.kill()
    proc.wait()
    return Holder(proc.pid, birth)


@pytest.fixture
def sleeper():
    """A live child process and its `Holder`; killed afterwards."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield proc, Holder(proc.pid, process_birth(proc.pid))
    finally:
        proc.kill()
        proc.wait()


# --- an unusable ledger -------------------------------------------------------------------------


def test_a_missing_directory_is_an_error_naming_the_path(tmp_path):
    missing = tmp_path / "nope"
    with pytest.raises(LedgerUnavailable) as info:
        Ledger(missing / "ledger.sqlite3")
    assert str(missing) in info.value.message
    assert (info.value.problem, info.value.path) == ("missing", missing)
    assert info.value.code == "ledger_unavailable"
    assert not missing.exists()  # never created: a root-made directory would hide the mistake


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anything")
def test_an_unwritable_directory_is_an_error(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(LedgerUnavailable) as info:
            Ledger(locked / "ledger.sqlite3")
        assert "not writable" in info.value.message and info.value.problem == "unwritable"
    finally:
        locked.chmod(0o700)


def test_an_unopenable_file_is_an_error_carrying_the_sqlite_cause(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    path.write_bytes(b"not a database" * 512)
    with pytest.raises(LedgerUnavailable) as info:
        Ledger(path)
    assert info.value.problem == "unopenable" and info.value.path == path
    assert isinstance(info.value.__cause__, sqlite3.Error)


def test_a_statement_failing_after_opening_is_unavailable_not_a_raw_sqlite_error(
    tmp_path, monkeypatch
):
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    monkeypatch.setattr(ledger_module, "_BUSY_TIMEOUT_S", 0.1)
    other = sqlite3.connect(ledger.path, isolation_level=None)
    try:
        other.execute("BEGIN EXCLUSIVE")  # holds the write lock past the busy timeout
        with pytest.raises(LedgerUnavailable) as info:
            ledger.try_admit(item("a"), ONE)
        other.execute("ROLLBACK")
    finally:
        other.close()
    assert info.value.problem == "failing" and info.value.path == ledger.path
    assert isinstance(info.value.__cause__, sqlite3.OperationalError)
    assert ledger.try_admit(item("a"), ONE) == Admitted()  # usable again once the lock is gone


# --- admission through the ledger ---------------------------------------------------------------


def test_admit_queue_release_and_idempotence(ledger):
    for i in range(2):
        assert ledger.try_admit(item(f"r{i}"), POLICY) == Admitted()
    # The third ci slot is work's reservation: never lent.
    assert position(ledger.try_admit(item("r2", user="bob"), POLICY)) == 1
    assert ledger.try_admit(item("r0"), POLICY) == Admitted()  # a holder asking again
    assert ledger.try_admit(item("w0", pool="work"), POLICY) == Admitted()
    ledger.release(item("r0"))
    assert ledger.try_admit(item("r2", user="bob"), POLICY) == Admitted()
    snap = ledger.snapshot()["pools"]
    assert snap["ci"] == {
        "held_weight": 2,
        "running": 2,
        "queued": 0,
        "running_by_user": {"ann": 1, "bob": 1},
    }
    assert snap["work"]["held_weight"] == 1


def test_rows_carry_scope_session_and_labels(ledger):
    two = AdmissionPolicy(slots={MACHINE: 2}, pools={"ci": PoolPolicy(MACHINE, 0, 1)})
    labelled = LedgerItem("agents", "a", "work", None, "ci", 1, labels=(("job", "lint"),))
    ledger.try_admit(labelled, two)
    ledger.try_admit(item("b", session="term-1"), two)
    [lease] = ledger.leases()
    [queued] = ledger.queued()
    assert (lease["scope"], lease["session"], lease["labels"]) == ("agents", None, {"job": "lint"})
    assert (queued["scope"], queued["session"], queued["labels"]) == ("ci", "term-1", {})
    assert "cls" not in lease and "repo" not in lease


def test_an_impossible_item_is_refused_not_queued(ledger):
    outcome = ledger.try_admit(item("big", weight=4), POLICY)
    assert isinstance(outcome, Refused) and "max 3" in outcome.reason
    assert isinstance(ledger.try_admit(item("x", pool="gone"), POLICY), Refused)
    assert ledger.snapshot()["pools"] == {}


def test_enqueue_restores_order_and_dequeue_takes_an_item_out(ledger):
    assert ledger.try_admit(item("held"), ONE) == Admitted()
    ledger.enqueue(item("late"), queued_at=200.0)
    ledger.enqueue(item("early"), queued_at=100.0)  # a rebuild from a durable queued time
    assert position(ledger.try_admit(item("early"), ONE)) == 1
    assert position(ledger.try_admit(item("late"), ONE)) == 2
    ledger.dequeue(item("early"))
    assert position(ledger.try_admit(item("late"), ONE)) == 1
    ledger.release(item("held"))
    assert ledger.try_admit(item("late"), ONE) == Admitted()


def test_a_closed_pool_admits_nothing_and_keeps_its_line(ledger):
    closed = AdmissionPolicy(ONE.slots, ONE.pools, closed=frozenset({"ci"}))
    assert ledger.try_admit(item("a"), closed) == Queued(1, "pool ci is closed")
    assert ledger.settle(ONE) == [("ci", "a")]


def test_one_user_does_not_hold_up_another_across_calls(ledger):
    """The round-robin's memory lives in the ledger: with one slot, ann's fifty queued items
    and bob's one alternate rather than ann's all going first."""
    for i in range(50):
        ledger.enqueue(item(f"a{i}", user="ann"), queued_at=float(i))
    ledger.enqueue(item("b0", user="bob"), queued_at=100.0)
    assert ledger.try_admit(item("a0", user="ann"), ONE) == Admitted()
    assert position(ledger.try_admit(item("b0", user="bob"), ONE)) == 1
    ledger.release(item("a0"))
    assert ledger.try_admit(item("b0", user="bob"), ONE) == Admitted()


def test_the_rotation_between_sessions_survives_reopening_the_ledger(tmp_path):
    """With one slot, ann's session `a` is served, then its lease ends; a ledger opened afresh
    on the same file serves her session `b` next, though `a`'s next item is older."""
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    for i in range(3):
        first.enqueue(item(f"a{i}", session="a"), queued_at=float(i))
    first.enqueue(item("b0", session="b"), queued_at=10.0)
    assert first.settle(ONE) == [("ci", "a0")]
    first.release(item("a0"))
    reopened = Ledger(path)
    assert reopened.settle(ONE) == [("ci", "b0")]
    reopened.release(item("b0"))
    assert reopened.settle(ONE) == [("ci", "a1")]


# --- holders ------------------------------------------------------------------------------------


def test_a_dead_holders_lease_and_queue_entry_are_reclaimed(ledger):
    gone = dead_holder()
    # Written as if by the process while it lived (the reclaim runs before each decision).
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "INSERT INTO leases (scope, item_id, kind, user, host, pool, weight, "
            "holder_pid, holder_birth, holder_ns, beat_at, acquired_at) "
            "VALUES ('ci', 'old', 'work', 'ann', ?, 'ci', 1, ?, ?, ?, ?, 0)",
            (MACHINE, gone.pid, gone.birth, gone.ns, ledger._now()),
        )
        conn.execute(
            "INSERT INTO queue (scope, item_id, kind, user, pool, weight, "
            "holder_pid, holder_birth, holder_ns, beat_at, queued_at) VALUES "
            "('ci', 'waiting', 'work', 'ann', 'ci', 1, ?, ?, ?, ?, 0)",
            (gone.pid, gone.birth, gone.ns, ledger._now()),
        )
    assert ledger.try_admit(item("new"), ONE) == Admitted()
    assert ledger.snapshot()["pools"]["ci"]["running"] == 1
    assert ledger.snapshot()["pools"]["ci"]["queued"] == 0


def test_a_reused_pid_does_not_keep_a_lease(ledger):
    """The pid is alive -- it is this test's -- but not the process that took the lease."""
    impostor = Holder(os.getpid(), "not-my-birth")
    assert ledger.try_admit(item("stale"), ONE, holder=impostor) == Admitted()
    assert ledger.try_admit(item("fresh"), ONE) == Admitted()
    assert ledger.snapshot()["pools"]["ci"]["running"] == 1


def test_a_holder_in_another_pid_namespace_is_judged_by_its_beat(tmp_path):
    """Two containers can share the ledger but not a pid namespace: there a pid means nothing,
    so a holder there keeps its lease while it beats and loses it once its beat is `STALE_S`
    old -- never because its pid happens to be dead (or alive) here."""
    clock = Clock(1000.0)
    ledger = Ledger(tmp_path / "ledger.sqlite3", now=clock)
    elsewhere = Holder(1, "someone's-birth", "pid:[1]")  # pid 1 of another container
    assert ledger.try_admit(item("theirs"), ONE, holder=elsewhere) == Admitted()
    clock.now += STALE_S - 1
    assert position(ledger.try_admit(item("mine"), ONE)) == 1  # still theirs
    ledger.beat(elsewhere)
    clock.now += STALE_S - 1
    assert position(ledger.try_admit(item("mine"), ONE)) == 1  # the beat kept it
    clock.now += 2
    assert ledger.try_admit(item("mine"), ONE) == Admitted()  # gone quiet: reclaimed
    assert not ledger.leased(item("theirs"))


def _contend(path: str, worker: int, results, release) -> None:
    ledger = Ledger(Path(path))
    wide = AdmissionPolicy(slots={MACHINE: 3}, pools={"ci": PoolPolicy(MACHINE, 0, 3)})
    admitted = 0
    for i in range(15):
        outcome = ledger.try_admit(
            LedgerItem(f"scope-{worker}", f"w{worker}-{i}", "work", f"u{worker}", "ci", 1), wide
        )
        admitted += isinstance(outcome, Admitted)
    results.put((worker, admitted, ledger.snapshot()["pools"]["ci"]["held_weight"]))
    release.wait(60)  # hold the leases (stay alive) until the parent has looked


def test_two_processes_contending_never_over_admit(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    results, release = ctx.Queue(), ctx.Event()
    path = str(tmp_path / "ledger.sqlite3")
    Ledger(tmp_path / "ledger.sqlite3")
    procs = [ctx.Process(target=_contend, args=(path, w, results, release)) for w in (1, 2)]
    for proc in procs:
        proc.start()
    try:
        got = [results.get(timeout=120) for _ in procs]
        assert all(held <= 3 for _, _, held in got)
        snap = Ledger(tmp_path / "ledger.sqlite3").snapshot()["pools"]["ci"]
        assert snap["held_weight"] == 3 and snap["running"] == 3
        # An item can be admitted by the other process's transaction (it admits whatever
        # fits), so the outcomes seen may undercount the leases, never overcount them.
        assert sum(admitted for _, admitted, _ in got) <= 3
    finally:
        release.set()
        for proc in procs:
            proc.join(60)
    # Both holders have exited: their leases go with them.
    assert Ledger(tmp_path / "ledger.sqlite3").snapshot()["pools"] == {}


# --- TTL leases ---------------------------------------------------------------------------------


def test_a_ttl_lease_lapses_unless_renewed_and_its_pid_is_never_consulted(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "l.sqlite3", now=clock)
    assert ledger.try_admit(item("a"), ONE, ttl_s=10) == Admitted()
    clock.now += 9
    assert ledger.renew(("ci", "a")).status == "ok"
    clock.now += 9  # 18 s after the grant, 9 after the renewal
    assert position(ledger.try_admit(item("b"), ONE, ttl_s=10)) == 1
    clock.now += 2
    assert ledger.renew(("ci", "a")).status == "unknown"
    assert ledger.try_admit(item("b"), ONE, ttl_s=10) == Admitted()


def test_a_ttl_place_in_line_is_kept_by_polling_and_lost_without(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "l.sqlite3", now=clock)
    ledger.try_admit(item("held"), ONE)  # process-fenced: this test process
    assert position(ledger.try_admit(item("w"), ONE, ttl_s=5)) == 1
    clock.now += 4
    assert position(ledger.try_admit(item("w"), ONE, ttl_s=5)) == 1  # the poll renews it
    clock.now += 4
    assert ledger.renew(("ci", "w")).status == "queued"
    clock.now += 6
    assert ledger.queued() == []


def test_grants_carry_increasing_fencing_numbers(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3")
    two = AdmissionPolicy(slots={MACHINE: 2}, pools={"ci": PoolPolicy(MACHINE, 0, 2)})
    ledger.try_admit(item("a"), two, ttl_s=10)
    ledger.try_admit(item("b"), two, ttl_s=10)
    assert [r["grant_no"] for r in ledger.leases()] == [1, 2]


def test_a_revoked_lease_holds_its_weight_only_until_its_deadline(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "l.sqlite3", now=clock)
    ledger.try_admit(item("lent", kind="lend"), ONE, ttl_s=10)
    ledger.try_admit(item("mine", kind="work"), ONE, ttl_s=10)
    assert ledger.revoke(kind="lend", reason="busy", grace_s=30) == [("ci", "lent")]
    assert ledger.revoke(kind="lend", reason="again", grace_s=30) == []  # once only
    for _ in range(4):  # renewing all along: still held, still revoked
        clock.now += 9
        renewal = ledger.renew(("ci", "lent"))
        assert (renewal.status, renewal.reason) == ("revoked", "busy")
        assert renewal.expires_at <= 1_000_000.0 + 40
    clock.now += 9  # 45 s: past grace + TTL
    assert ledger.renew(("ci", "lent")).status == "unknown"


def test_process_fenced_leases_never_expire_by_time(tmp_path):
    """A lease without a TTL is judged by its holder process alone."""
    clock = Clock()
    ledger = Ledger(tmp_path / "l.sqlite3", now=clock)
    ledger.try_admit(item("job-1"), ONE)
    clock.now += 10_000
    assert ledger.renew(("ci", "job-1")).status == "ok"
    assert len(ledger.leases()) == 1 and ledger.leases()[0]["expires_at"] is None


# --- imposed leases -----------------------------------------------------------------------------


#: Two slots: `work` may use one, `observed` two.
OBSERVED = AdmissionPolicy(
    slots={MACHINE: 2},
    pools={
        "work": PoolPolicy(MACHINE, reserved=0, max=1, priority=0),
        "observed": PoolPolicy(MACHINE, reserved=0, max=2, priority=3),
    },
)


def test_an_imposed_lease_may_pass_max_and_blocks_admission_until_it_ends(ledger, sleeper):
    proc, holder = sleeper
    seen = item("seen", pool="observed", user=None, weight=3, scope="observer", kind="observed")
    assert ledger.impose(seen, OBSERVED, holder=holder) is True
    assert ledger.snapshot()["pools"]["observed"]["held_weight"] == 3  # past its max of 2
    assert ledger.try_admit(item("more", pool="observed"), OBSERVED) == Queued(
        1, "pool observed holds 3 of 2; this needs 1"
    )
    assert ledger.try_admit(item("w", pool="work"), OBSERVED) == Queued(
        1, "0 of 2 slots are free; this needs 1"
    )
    proc.kill()
    proc.wait()
    assert ledger.settle(OBSERVED) == [("ci", "w"), ("ci", "more")]
    assert not ledger.leased(seen)


def test_a_dead_or_pid_reused_holder_is_not_imposed(ledger):
    seen = item("seen", pool="observed", kind="observed")
    assert ledger.impose(seen, OBSERVED, holder=dead_holder()) is False
    assert ledger.impose(seen, OBSERVED, holder=Holder(os.getpid(), "not-my-birth")) is False
    assert ledger.leases() == []


def test_imposing_the_same_item_twice_makes_one_lease(ledger, sleeper):
    _proc, holder = sleeper
    seen = item("seen", pool="observed", kind="observed")
    assert ledger.impose(seen, OBSERVED, holder=holder) is True
    assert ledger.impose(seen, OBSERVED, holder=holder) is False
    assert [r["item_id"] for r in ledger.leases()] == ["seen"]


# --- places in line -----------------------------------------------------------------------------


def test_lines_give_each_waiting_items_place_and_blocker(ledger):
    assert ledger.try_admit(item("held", user="cy"), ONE) == Admitted()
    ledger.enqueue(item("a0"), queued_at=1.0)
    ledger.enqueue(item("a1"), queued_at=2.0)
    ledger.enqueue(item("b0", user="bob"), queued_at=3.0)
    assert ledger.lines(ONE) == {
        ("ci", "a0"): (1, "pool ci holds 1 of 1; this needs 1"),
        ("ci", "b0"): (2, "1 ahead of it in pool ci"),
        ("ci", "a1"): (3, "2 ahead of it in pool ci"),
    }
    assert ledger.try_admit(item("b0", user="bob"), ONE) == Queued(2, "1 ahead of it in pool ci")


def test_lines_leave_out_a_dead_holders_place(ledger):
    ledger.try_admit(item("held"), ONE)
    ledger.enqueue(item("theirs"), holder=dead_holder())
    assert ledger.lines(ONE) == {}


# --- standing weight and the file ---------------------------------------------------------------


def test_standing_weight_is_not_stored_and_survives_deleting_the_ledger(tmp_path):
    """Standing weight is configuration: rebuilding the file does not give the slots back,
    and a later policy with a different weight applies to the same rows."""
    path = tmp_path / "ledger.sqlite3"

    def standing(weight: int) -> AdmissionPolicy:
        return AdmissionPolicy(
            slots={MACHINE: 3}, pools={"ci": PoolPolicy(MACHINE, 0, 3)}, standing={"ci": weight}
        )

    ledger = Ledger(path)
    assert ledger.try_admit(item("a"), standing(2)) == Admitted()
    assert isinstance(ledger.try_admit(item("b"), standing(2)), Queued)
    assert [row["item_id"] for row in ledger.leases()] == ["a"]
    assert ledger.snapshot()["pools"]["ci"] == {
        "held_weight": 1,
        "running": 1,
        "queued": 1,
        "running_by_user": {"ann": 1},
    }
    with sqlite3.connect(path) as conn:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"meta", "leases", "queue", "turns", "session_turns", "submits",
                      "sqlite_sequence"}  # fmt: skip
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    rebuilt = Ledger(path)
    assert rebuilt.leases() == []
    assert rebuilt.try_admit(item("c"), standing(2)) == Admitted()
    assert isinstance(rebuilt.try_admit(item("d", weight=2), standing(2)), Refused)
    assert rebuilt.try_admit(item("e"), standing(1)) == Admitted()
    assert [row["item_id"] for row in rebuilt.leases()] == ["c", "e"]


def test_the_ledger_is_transient_a_deleted_file_is_rebuilt_empty(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    first = Ledger(path)
    assert first.try_admit(item("r0"), POLICY) == Admitted()
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    again = Ledger(path)
    assert again.snapshot()["pools"] == {}
    assert again.try_admit(item("r0"), POLICY) == Admitted()  # its live holder re-acquires


def test_an_older_schema_is_rebuilt_empty(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"
            f"INSERT INTO meta VALUES ('schema_version', '{SCHEMA_VERSION - 1}');"
            "CREATE TABLE leases (repo TEXT, item_id TEXT);"
            "INSERT INTO leases VALUES ('old', 'x');"
        )
    ledger = Ledger(path)
    assert ledger.snapshot()["pools"] == {}
    assert ledger.try_admit(item("a"), POLICY) == Admitted()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone() == (
            str(SCHEMA_VERSION),
        )


# --- review fixes: ownership, impose, session memory ------------------------------------------


def test_a_key_another_user_holds_or_waits_under_is_refused(ledger):
    two = AdmissionPolicy(slots={MACHINE: 2}, pools={"ci": PoolPolicy(MACHINE, 0, 2)})
    assert isinstance(ledger.try_admit(item("x", user="ann"), two), Admitted)
    stolen = ledger.try_admit(item("x", user="bob"), two)
    assert isinstance(stolen, Refused) and "another user's" in stolen.reason
    assert isinstance(ledger.try_admit(item("x", user="ann"), two), Admitted)  # still hers
    ledger.try_admit(item("big", user="ann", weight=2), two)  # waits behind x
    assert isinstance(ledger.try_admit(item("big", user="bob", weight=2), two), Refused)


def test_impose_refuses_a_weight_below_one(ledger, sleeper):
    _, holder = sleeper
    with pytest.raises(ValueError, match="below 1"):
        ledger.impose(item("neg", weight=0), ONE, holder=holder)


def test_imposing_again_keeps_a_holder_in_another_namespace_alive(tmp_path):
    clock = Clock()
    ledger = Ledger(tmp_path / "ledger.sqlite3", now=clock)
    elsewhere = Holder(pid=4_000_000, birth=1, ns="pid:[4026531999]")  # not judgeable here
    assert not elsewhere.judgeable()
    assert ledger.impose(item("seen"), ONE, holder=elsewhere)
    for _ in range(3):
        clock.now += STALE_S * 0.75
        assert ledger.impose(item("seen"), ONE, holder=elsewhere) is False  # seen again
    assert [r["item_id"] for r in ledger.leases()] == ["seen"]
    clock.now += STALE_S + 1  # no longer seen: reclaimed
    assert ledger.leases() == []


def test_the_ledger_forgets_only_the_least_recently_served_sessions(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "SESSION_TURNS_KEPT", 3)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    for n in range(5):
        ledger.try_admit(item(f"i{n}", session=f"s{n}"), ONE)
        ledger.release(item(f"i{n}"))
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as conn:
        kept = [r[0] for r in conn.execute("SELECT session FROM session_turns ORDER BY seq")]
    assert kept == ["s2", "s3", "s4"]


def test_a_waiting_sessions_turn_is_never_forgotten(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_module, "SESSION_TURNS_KEPT", 1)
    ledger = Ledger(tmp_path / "ledger.sqlite3")
    two = AdmissionPolicy(slots={MACHINE: 2}, pools={"ci": PoolPolicy(MACHINE, 0, 2)})
    ledger.try_admit(item("a1", session="a"), two)  # a holds a lease
    ledger.try_admit(item("b1", session="b"), two)  # b too: the newest turn
    with sqlite3.connect(tmp_path / "ledger.sqlite3") as conn:
        kept = sorted(r[0] for r in conn.execute("SELECT session FROM session_turns"))
    assert kept == ["a", "b"]  # a is past the cap, but still holds something


def test_an_item_that_could_never_fit_is_refused_and_does_not_hold_up_its_pool(ledger):
    """Eight slots, four reserved for `a`: weight 6 in `b` is refused, and `b`'s next item is
    admitted instead of waiting behind it forever."""
    policy = AdmissionPolicy(
        slots={MACHINE: 8},
        pools={
            "a": PoolPolicy(MACHINE, reserved=4, max=8),
            "b": PoolPolicy(MACHINE, reserved=0, max=8, priority=1),
        },
    )
    big = ledger.try_admit(item("big", pool="b", weight=6), policy)
    assert isinstance(big, Refused) and "4 are reserved for other pools" in big.reason
    assert ledger.queued() == []
    assert isinstance(ledger.try_admit(item("small", pool="b", weight=1), policy), Admitted)


@pytest.mark.parametrize("weight", [0, -5])
def test_a_weight_below_one_is_never_admitted_or_queued(ledger, weight):
    """A negative weight would free capacity it never held and let its pool over-commit."""
    outcome = ledger.try_admit(item("neg", weight=weight), POLICY)
    assert isinstance(outcome, Refused) and "below 1" in outcome.reason
    assert ledger.leases() == [] and ledger.queued() == []
    with pytest.raises(ValueError, match="below 1"):
        ledger.enqueue(item("neg", weight=weight))


# --- declared memory -----------------------------------------------------------------------------


def test_declared_memory_is_kept_and_counted_where_the_policy_has_a_budget(ledger):
    budget = AdmissionPolicy(slots=POLICY.slots, pools=POLICY.pools, memory={MACHINE: 10 << 30})
    big = LedgerItem("ci", "big", "work", "ann", "ci", 1, memory=8 << 30)
    assert isinstance(ledger.try_admit(big, budget), Admitted)
    second = LedgerItem("ci", "more", "work", "bob", "ci", 1, memory=4 << 30)
    assert position(ledger.try_admit(second, budget)) == 1
    assert [r["memory"] for r in ledger.leases()] == [8 << 30]
    assert [r["memory"] for r in ledger.queued()] == [4 << 30]
    assert ledger.settle(POLICY) == [("ci", "more")]  # no budget: memory is not counted
    refused = ledger.try_admit(
        LedgerItem("ci", "x", "work", "ann", "ci", 1, memory=11 << 30), budget
    )
    assert isinstance(refused, Refused) and "memory budget" in refused.reason


def test_a_0_2_ledger_gains_the_memory_columns_and_keeps_its_rows(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    old_schema = ledger_module._SCHEMA.replace("    memory INTEGER,\n", "").replace(
        "    -- The memory the item declared, in bytes; NULL: none.\n", ""
    )
    assert "memory INTEGER" not in old_schema
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO queue (scope, item_id, kind, user, session, pool, weight, holder_pid, "
        "holder_birth, holder_ns, beat_at, queued_at, labels, ttl_s, expires_at) "
        "VALUES ('ci', 'old', 'work', 'ann', NULL, 'ci', 1, 1, NULL, NULL, 0, 0, NULL, 1e12, 1e12)"
    )
    conn.commit()
    conn.close()
    ledger = Ledger(path)
    [row] = ledger.queued()
    assert row["item_id"] == "old" and row["memory"] is None
    assert ledger.settle(POLICY) == [("ci", "old")]
    assert ledger.leases()[0]["memory"] is None


# --- ranges of weight ----------------------------------------------------------------------------

#: Six slots in one pool.
SIX = AdmissionPolicy(slots={MACHINE: 6}, pools={"ci": PoolPolicy(MACHINE, 0, 6)})


def test_a_range_is_granted_a_size_kept_as_the_leases_weight(tmp_path):
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock())
    ranged = LedgerItem("ci", "r", "work", "ann", "ci", 2, max_weight=4, memory_per_slot=1 << 30)
    assert ledger.try_admit(ranged, SIX, ttl_s=10) == Admitted()
    [row] = ledger.leases()
    assert (row["weight"], row["min_weight"], row["max_weight"]) == (4, 2, 4)
    # The total granted is recorded too, for a 0.3 process sharing the ledger.
    assert row["memory_per_slot"] == 1 << 30 and row["memory"] == 4 << 30
    assert ledger.snapshot()["pools"]["ci"]["held_weight"] == 4
    renewal = ledger.renew(ranged.key)
    assert (renewal.status, renewal.weight, renewal.min_weight, renewal.max_weight) == (
        "ok",
        4,
        2,
        4,
    )
    # A fixed weight is a range of one.
    fixed = LedgerItem("ci", "f", "work", "bob", "ci", 2)
    assert ledger.try_admit(fixed, SIX, ttl_s=10) == Admitted()
    row = next(r for r in ledger.leases() if r["item_id"] == "f")
    assert (row["weight"], row["min_weight"], row["max_weight"]) == (2, 2, 2)


def test_a_range_waits_in_line_with_its_most_and_is_judged_by_its_least(ledger):
    assert ledger.try_admit(item("held", weight=5), SIX) == Admitted()
    ranged = LedgerItem("ci", "r", "work", "bob", "ci", 2, max_weight=60)
    assert position(ledger.try_admit(ranged, SIX)) == 1  # never refused for its most
    [row] = ledger.queued()
    assert (row["weight"], row["max_weight"]) == (2, 60)
    ledger.release(item("held"))
    assert ledger.settle(SIX) == [("ci", "r")]
    assert ledger.leases()[0]["weight"] == 6


def test_asking_again_under_the_same_id_keeps_the_first_range(ledger):
    assert ledger.try_admit(item("held", weight=6), SIX) == Admitted()
    first = LedgerItem("ci", "r", "work", "bob", "ci", 1, max_weight=6)
    assert position(ledger.try_admit(first, SIX)) == 1
    again = LedgerItem("ci", "r", "work", "bob", "ci", 1, max_weight=2)
    assert position(ledger.try_admit(again, SIX)) == 1
    [row] = ledger.queued()
    assert (row["weight"], row["max_weight"]) == (1, 6)


def test_an_imposed_lease_counts_its_memory_per_slot(ledger, sleeper):
    _proc, holder = sleeper
    budget = AdmissionPolicy(
        slots={MACHINE: 6}, pools={"ci": PoolPolicy(MACHINE, 0, 6)}, memory={MACHINE: 4 << 30}
    )
    seen = LedgerItem("observer", "seen", "observed", None, "ci", 3, memory_per_slot=1 << 30)
    assert ledger.impose(seen, budget, holder=holder) is True
    [row] = ledger.leases()
    assert (row["weight"], row["min_weight"], row["max_weight"]) == (3, 3, 3)
    assert row["memory_per_slot"] == 1 << 30 and row["memory"] == 3 << 30
    heavy = LedgerItem("ci", "h", "work", "ann", "ci", 1, memory=2 << 30)
    assert ledger.try_admit(heavy, budget) == Queued(
        1,
        "the memory budget is used: 3.0 GiB of 4.0 GiB counted; this counts its declared 2.0 GiB",
    )


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"max_weight": 1}, "below weight"),
        ({"memory": 1, "memory_per_slot": 1}, "one or the other"),
        ({"memory_per_slot": -1}, "memory_per_slot"),
    ],
)
def test_a_ledger_item_with_an_impossible_range_or_memory_is_an_error(fields, message):
    with pytest.raises(ValueError, match=message):
        LedgerItem("ci", "x", "work", "ann", "ci", 2, **fields)


def test_a_0_3_ledger_gains_the_range_columns_in_place_and_keeps_its_rows(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    old_schema = ledger_module._SCHEMA
    for line in ("min_weight INTEGER,", "max_weight INTEGER,", "memory_per_slot INTEGER,"):
        old_schema = old_schema.replace(f"    {line}\n", "")
    conn = sqlite3.connect(path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO leases (scope, item_id, kind, user, session, host, pool, weight, "
        "holder_pid, holder_birth, holder_ns, beat_at, acquired_at, labels, ttl_s, expires_at, "
        "grant_no, memory) VALUES ('ci', 'held', 'work', 'ann', NULL, 'machine', 'ci', 2, 1, "
        "NULL, NULL, 0, 0, NULL, 1e12, 1e12, 1, NULL)"
    )
    conn.execute(
        "INSERT INTO queue (scope, item_id, kind, user, session, pool, weight, holder_pid, "
        "holder_birth, holder_ns, beat_at, queued_at, labels, ttl_s, expires_at, memory) "
        "VALUES ('ci', 'old', 'work', 'bob', NULL, 'ci', 1, 1, NULL, NULL, 0, 0, NULL, 1e12, "
        "1e12, NULL)"
    )
    conn.commit()
    for table in ("leases", "queue"):
        columns = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert "memory" in columns and not columns & {"max_weight", "memory_per_slot"}
    conn.close()
    ledger = Ledger(path)
    [held] = ledger.leases()
    assert held["item_id"] == "held" and held["min_weight"] is None and held["weight"] == 2
    [waiting] = ledger.queued()
    assert waiting["max_weight"] is None and waiting["memory_per_slot"] is None
    assert ledger.settle(SIX) == [("ci", "old")]  # a row from before ranges: its weight alone
    old = next(r for r in ledger.leases() if r["item_id"] == "old")
    assert (old["weight"], old["min_weight"], old["max_weight"]) == (1, 1, 1)
    assert ledger.renew(("ci", "held")).min_weight == 2  # read as a range of one


def _contend_ranges(path: str, worker: int, results, release) -> None:
    ledger = Ledger(Path(path))
    admitted = 0
    for i in range(10):
        key = f"w{worker}-{i}"
        ranged = LedgerItem(f"s{worker}", key, "work", f"u{worker}", "ci", 1, max_weight=3)
        admitted += isinstance(ledger.try_admit(ranged, SIX), Admitted)
    results.put((worker, admitted))
    release.wait(60)  # hold the leases (stay alive) until the parent has looked


def test_two_processes_granting_ranges_never_over_admit(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    results, release = ctx.Queue(), ctx.Event()
    path = str(tmp_path / "ledger.sqlite3")
    Ledger(tmp_path / "ledger.sqlite3")
    procs = [ctx.Process(target=_contend_ranges, args=(path, w, results, release)) for w in (1, 2)]
    for proc in procs:
        proc.start()
    try:
        for _ in procs:
            results.get(timeout=120)
        leases = Ledger(tmp_path / "ledger.sqlite3").leases()
        assert sum(r["weight"] for r in leases) <= 6
        assert all(1 <= r["weight"] <= 3 for r in leases)
    finally:
        release.set()
        for proc in procs:
            proc.join(60)


def _contend_elastic(path: str, worker: int, results, release) -> None:
    """Admit elastic ranges and move each to its target as fast as it can, while the other
    process does the same: the most held at once, as this process saw it."""
    ledger = Ledger(Path(path), settle_s=0)
    seen = 0
    mine = []
    for i in range(12):
        key = (f"s{worker}", f"e{worker}-{i}")
        item = LedgerItem(*key, "work", f"u{worker}", "ci", 1, max_weight=4, elastic=True)
        ledger.try_admit(item, SIX)
        mine.append(key)
        for held in mine:
            renewal = ledger.renew(held)
            if renewal.target is not None and renewal.target != renewal.weight:
                ledger.resize(held, renewal.target, SIX)
        if i % 3 == 2:
            ledger.release(mine.pop(0))
        ledger.settle(SIX)
        seen = max(seen, sum(r["weight"] for r in ledger.leases()))
    results.put((worker, seen))
    release.wait(60)


def test_two_processes_resizing_elastic_leases_never_over_admit(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    results, release = ctx.Queue(), ctx.Event()
    path = str(tmp_path / "ledger.sqlite3")
    Ledger(tmp_path / "ledger.sqlite3")
    procs = [ctx.Process(target=_contend_elastic, args=(path, w, results, release)) for w in (1, 2)]
    for proc in procs:
        proc.start()
    try:
        got = [results.get(timeout=120) for _ in procs]
        assert all(seen <= 6 for _, seen in got)
        leases = Ledger(tmp_path / "ledger.sqlite3").leases()
        assert sum(r["weight"] for r in leases) <= 6
        assert all(1 <= r["weight"] <= 4 for r in leases)
        assert any(r["weight"] > 1 for r in leases) or len(leases) == 6  # they did grow
    finally:
        release.set()
        for proc in procs:
            proc.join(60)


def test_a_per_slot_lease_counts_the_same_to_a_reader_from_before_memory_per_slot(tmp_path):
    # A 0.3 process reads only `memory`: the total granted, which is what admission counts.
    budget = AdmissionPolicy(
        slots={MACHINE: 6}, pools={"ci": PoolPolicy(MACHINE, 0, 6)}, memory={MACHINE: 8 << 30}
    )
    ledger = Ledger(tmp_path / "l.sqlite3", now=Clock())
    ranged = LedgerItem("ci", "r", "work", "ann", "ci", 2, max_weight=4, memory_per_slot=1 << 30)
    assert ledger.try_admit(ranged, budget, ttl_s=10) == Admitted()
    [row] = ledger.leases()
    now = budget.counted(MACHINE, row["weight"], row["memory"], row["memory_per_slot"])
    before = budget.counted(MACHINE, row["weight"], row["memory"])
    assert now == before == 4 << 30
    # Both set: the per-slot size wins, so a lease is never counted twice.
    assert budget.counted(MACHINE, 4, 99, 1 << 30) == 4 << 30
