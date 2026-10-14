# leasehold as a library

The admission decision is a pure function: give it what is held, what waits and the pools, and it
returns the items to admit now. No daemon, socket or ledger is involved, so a scheduler of your own
can use the same rules as `leasehold serve`.

```sh
python3 decide.py
```

`decide.py` builds the three inputs and prints the answer:

1. A `PoolSet` (`leasehold.model`): 8 slots, a `work` pool that reserves 4 and may use all 8, and
   an `external` pool that may use 4. `AdmissionPolicy.from_pools` turns it into the policy
   `decide` reads.
2. A `State` (`leasehold.admission`): a CI job holding 4 slots in `external` (`Held`), and five
   items in line (`Waiting`). Each `Waiting` has a key, a pool, a user, a weight, its place in its
   session's line (`order`) and an optional session.
3. `decide(state, policy)` returns the keys to admit, in order. `blocker(state, policy, key)` says
   in words why a waiting item is not admitted.

The output:

```
admitted now, in order:
  ann/build-1  work      weight 2
  bob/test-1   work      weight 2
still waiting:
  ann/build-2  work      weight 2: 1 ahead of it in pool work
  ann/lint-1   work      weight 2: 0 of 8 slots are free; this needs 2
  ci/job-2     external  weight 4: pool external holds 4 of 4; this needs 4
```

ann queued three items before bob's one, yet bob is admitted second: users take turns. ann's
`lint-1` is ahead of her `build-2` although it queued later, because it is in her other session
(`agent-2`), and one user's sessions take turns too. `ci/job-2` waits because `external` is at its
`max`, not because the machine is full.

`decide` changes nothing. To admit the items, move them from `waiting` to `held` yourself, as the
script does before it asks `blocker`. To keep the users' rotation across calls, pass
`State.served` and `State.session_served` (who was served last); `leasehold.ledger.Ledger` keeps
them in SQLite for you.
