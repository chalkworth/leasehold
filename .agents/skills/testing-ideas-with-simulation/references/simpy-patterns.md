# SimPy patterns for contention models

Read when: writing a SimPy model that has a shared resource, priorities, preemption, reneging, a buffer or a stock
level. Examples target SimPy 4.1.2 and run as written.

## Choosing the primitive

| Modelled thing | Primitive | Waiting call |
| -- | -- | -- |
| Limited number of simultaneous users (operators, GPUs, backends) | `Resource(env, capacity)` | `request()` |
| Same, with a queue ordered by priority (lower number first) | `PriorityResource` | `request(priority=)` |
| Same, and a more urgent request can displace a current user | `PreemptiveResource` | `request(priority=, preempt=True)` |
| A stock level (battery charge, tokens, buffer bytes) | `Container(env, capacity, init)` | `put(amount)`, `get(amount)` |
| A queue of distinct items (jobs, frames) | `Store`; `FilterStore` to pick by predicate; `PriorityStore` | `put(item)`, `get()` |

`len(resource.queue)` counts only waiting requests; people in the system are `resource.count + len(resource.queue)`.
`PriorityResource` never displaces a current user; only `PreemptiveResource` does. A `PriorityStore` does not keep
equal priorities in arrival order: use `PriorityItem((priority, sequence_number), payload)` when ties must be FIFO.

## Replicated model with warm-up (template)

```python
import random
import statistics

import simpy

ARRIVAL_RATE = 5.0  # customers per minute
SERVICE_RATE = 2.0  # per server per minute
SERVERS = 3
WARM_UP_MINUTES = 200.0
HORIZON_MINUTES = 4000.0
REPLICATIONS = 20
BASE_SEED = 1234


def run_replication(seed: int) -> float:
    """Mean queue wait of customers who arrived after warm-up."""
    arrival_rng = random.Random(f"arrivals-{seed}")  # one RNG per random source
    service_rng = random.Random(f"service-{seed}")
    env = simpy.Environment()
    servers = simpy.Resource(env, capacity=SERVERS)
    waits: list[float] = []

    def customer():
        arrived = env.now
        with servers.request() as request:
            yield request
            if arrived >= WARM_UP_MINUTES:
                waits.append(env.now - arrived)
            yield env.timeout(service_rng.expovariate(SERVICE_RATE))

    def arrivals():
        while True:
            yield env.timeout(arrival_rng.expovariate(ARRIVAL_RATE))
            env.process(customer())

    env.process(arrivals())
    env.run(until=HORIZON_MINUTES)
    return statistics.fmean(waits)


replication_means = [run_replication(BASE_SEED + index) for index in range(REPLICATIONS)]
print(statistics.fmean(replication_means))  # about 0.72 here; the M/M/3 closed form is 0.70
```

Summarise `replication_means` with `scripts/queueing.py summarize`. Swap the sampler for the real distribution once
the model matches its closed form.

## Priority and preemption (work that resumes)

The displaced process receives `simpy.Interrupt`; its `cause.usage_since` says when it began using the resource. The
interrupted `timeout` is not cancelled, so track remaining work and re-request:

```python
import simpy


def background_job(env, operator, work_minutes, finished):
    remaining = work_minutes
    while remaining > 0:
        with operator.request(priority=5, preempt=True) as request:
            yield request
            started = env.now
            try:
                yield env.timeout(remaining)
                remaining = 0
            except simpy.Interrupt:
                remaining -= env.now - started  # keep partial progress
    finished.append(env.now)


def urgent_job(env, operator, arrive_at, work_minutes):
    yield env.timeout(arrive_at)
    with operator.request(priority=0, preempt=True) as request:
        yield request
        yield env.timeout(work_minutes)


env = simpy.Environment()
operator = simpy.PreemptiveResource(env, capacity=1)
finished: list[float] = []
env.process(background_job(env, operator, 10, finished))
env.process(urgent_job(env, operator, arrive_at=2, work_minutes=1))
env.run()
assert finished == [11]
```

A queued request with higher priority and `preempt=False` can stop a lower-priority preempting request from
displacing anyone; do not mix the two policies without testing the combination.

## Giving up (reneging) and timeouts

```python
def impatient_customer(env, resource, patience_minutes, outcomes):
    with resource.request() as request:
        result = yield request | env.timeout(patience_minutes)
        if request not in result:
            outcomes.append("reneged")  # leaving the with-block cancels the pending request
            return
        yield env.timeout(2)
        outcomes.append("served")
```

## Stock level (Container)

```python
def charging_check():
    env = simpy.Environment()
    battery = simpy.Container(env, capacity=100, init=40)

    def drive_then_charge():
        yield battery.get(30)  # waits if fewer than 30 units are left
        yield battery.put(50)

    env.process(drive_then_charge())
    env.run()
    assert battery.level == 60
```

Add a conservation check to every model: `initial + completed puts - completed gets == final level`.
A blocked `put` or `get` with nobody left to change the level is a deadlock: `env.run()` then returns with work
unfinished, so count unfinished work at the end.
