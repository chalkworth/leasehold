"""Who gets in: leasehold's admission decision used as a library, with no daemon.

An 8-slot machine with two pools. A CI job already holds 4 slots; four more items wait. `decide`
says which of them to admit now, and `blocker` says why each of the rest still waits.
"""

from leasehold.admission import AdmissionPolicy, Held, State, Waiting, blocker, decide
from leasehold.model import MACHINE, Pool, PoolSet

pools = PoolSet(
    slots=8,
    pools={
        "work": Pool(reserved=4, max=8, priority=0),  # the owner's work, served first
        "external": Pool(reserved=0, max=4, priority=1),  # CI jobs
    },
)
policy = AdmissionPolicy.from_pools(pools)

held = (Held(pool="external", host=MACHINE, user="ci", weight=4),)


def waiting(n: int, user: str, name: str, pool: str, weight: int, session=None) -> Waiting:
    """The n-th item to join the line. `order` is its place in its session's FIFO."""
    return Waiting(("example", f"{user}/{name}"), pool, user, weight, (n, 0), session)


line = (
    waiting(1, "ann", "build-1", "work", 2, session="agent-1"),
    waiting(2, "ann", "build-2", "work", 2, session="agent-1"),
    waiting(3, "ann", "lint-1", "work", 2, session="agent-2"),
    waiting(4, "bob", "test-1", "work", 2),
    waiting(5, "ci", "job-2", "external", 4),
)
state = State(held=held, waiting=line)

admitted = decide(state, policy)
print("admitted now, in order:")
for key in admitted:
    item = next(w for w in line if w.key == key)
    print(f"  {key[1]:<12} {item.pool:<9} weight {item.weight}")

# The machine once those are in: what decide admitted is held, the rest still waits.
after = State(
    held=held
    + tuple(Held(w.pool, MACHINE, w.user, w.weight, w.session) for w in line if w.key in admitted),
    waiting=tuple(w for w in line if w.key not in admitted),
)
print("still waiting:")
for item in after.waiting:
    why = blocker(after, policy, item.key)
    print(f"  {item.key[1]:<12} {item.pool:<9} weight {item.weight}: {why}")
