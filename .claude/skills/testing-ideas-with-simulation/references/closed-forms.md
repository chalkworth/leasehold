# Closed forms to try before simulating

Read when: checking a theory about waiting, capacity or staffing, before deciding whether a simulation is needed.

Arrival rate is `lambda`, service rate per server is `mu` (1 / mean service time), servers `c`, offered load
`a = lambda / mu`, utilisation `rho = a / c`. Use one time unit throughout.

## Always true (no distribution assumptions)

- **Little's law:** `L = lambda * W` (items in a system = throughput x time in it). Apply it to the queue, the whole
  system, or the servers alone. Utilisation of a single server is `lambda * mean service time`.
- **Stability:** if `rho >= 1` the queue grows without bound in the long run. Waits then depend on the horizon, so
  simulate a finite horizon or report the growth rate.
- **Bottleneck:** throughput cannot exceed the slowest stage's capacity. If the claimed bottleneck stage is at
  `rho` well under 1, the theory that it limits throughput is refuted by arithmetic alone.

## Models and formulas

| Situation | Model | Mean wait in queue |
| -- | -- | -- |
| One server, random arrivals, exponential service | M/M/1 | `rho / (mu - lambda)` |
| One server, any service-time spread | M/G/1, Pollaczek-Khinchine | `rho * mean_service * (1 + cv^2) / (2 * (1 - rho))` |
| `c` identical servers, one queue | M/M/c, Erlang C | `C(c, a) / (c * mu - lambda)` |
| Few units that return after service (robots needing help) | Finite source, machine repair | birth-death sum, see below |

`cv` is service-time standard deviation over mean (1 exponential, 0 constant). Waiting time scales with
`(1 + cv^2) / 2`, so constant service halves the M/M/1 wait and a heavy tail (`cv` of 2) multiplies it by 2.5.

**Erlang C**, the probability an arrival has to wait:

```
C(c, a) = (a^c / c!) * (1 / (1 - rho)) / ( sum_{k=0}^{c-1} a^k / k!  +  (a^c / c!) * (1 / (1 - rho)) )
```

Stdlib version (the recursion is Erlang B, numerically stable for large `c`):

```python
def erlang_c(servers: int, offered_load: float) -> float:
    blocking = 1.0
    for count in range(1, servers + 1):
        blocking = offered_load * blocking / (count + offered_load * blocking)
    utilisation = offered_load / servers
    return blocking / (1.0 - utilisation * (1.0 - blocking))
```

Check: `erlang_c(2, 1.0)` is 1/3 (to rounding); `erlang_c(12, 10.0)` is about 0.449; `erlang_c(1, a)` is `a`.

**Finite source (machine repair):** with `n` units, state `k` = units waiting or in service. Weights
`w[0] = 1`, `w[k] = w[k-1] * ((n - k + 1) / mean_time_between_requests) / (min(k, c) / mean_service)`;
probabilities are `w / sum(w)`. Mean units waiting or served is `sum(k * p[k])`. An infinite-source M/M/c with the
same total request rate overstates congestion, because waiting units stop generating requests.

## Where the closed form stops

These break the formulas, so simulate: arrivals in bursts or on a schedule; service times with unknown or
multimodal shape; priority classes or preemption; shared resources held across stages; blocking or finite buffers;
abandonment; time-varying staffing; a finite horizon or a start-up transient shorter than the system's settling time;
tail or rare-event probabilities rather than means. M/M/c still gives a first answer and the check for step 5.

Two rules of thumb: waits explode as `rho` approaches 1 (at `rho = 0.9` the M/M/1 wait is nine service times), and
averages hide tails, so ask for a percentile when the claim is about worst cases.
