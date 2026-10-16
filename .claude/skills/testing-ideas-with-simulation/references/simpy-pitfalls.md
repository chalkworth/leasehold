# SimPy pitfalls that change reported numbers

Read when: before reporting a SimPy result, or when results look wrong, a run hangs, or a model uses `run(until=...)`,
`AnyOf` (`a | b`) or time-averaged statistics. Behaviour is SimPy 4.1.2.

## Horizon and stopping

- `env.run(until=10)` is half-open: the clock reaches 10 but events scheduled exactly at 10 are not processed.
  Time-based totals therefore cover `[0, 10)`; use a horizon that is not on an event boundary, or accept it.
- `env.run(until=event)` returns the event's value, but `event.processed` may still be `False` straight after.
  Rely on the returned value or your own state. It raises `RuntimeError` if the schedule empties first.
- `env.run()` with no argument never ends if any process loops forever (`while True: yield env.timeout(...)`).
- A loop that yields `env.timeout(0)` (or never yields) advances no time, so `until` is never reached and the run
  hangs. Every loop must wait on something that takes time. To diagnose, step manually with an event cap:
  `while env.peek() < horizon and steps < cap: env.step()`.
- Entities still in the system at the horizon are unfinished, not zero-wait. Decide in advance: report them as
  censored, stop arrivals and drain the system, or end on a completion event. These answer different questions.

## Events and conditions

- `yield a | b` (`AnyOf`) does not cancel the loser. A losing resource request stays queued unless it is inside a
  `with resource.request() as request:` block (leaving the block cancels it) or you call `request.cancel()`. A leaked
  request silently holds a server and distorts every later wait.
- Both events of a tie may be present in an `AnyOf` result; test `x in result` for each rather than assuming one winner.
- `env.process(worker)` is wrong; pass the generator: `env.process(worker(env))`. A function with no reachable
  `yield` is not a generator. Yield events, never bare numbers.
- Events at the same time and priority run in the order they were scheduled. If a result depends on tie order, the
  model is fragile: perturb times slightly to check.

## Measuring

- Queue length and utilisation are time averages, not averages of samples. Sampling at events overweights busy
  periods. Record `(time, value)` at every change, add a final point exactly at the horizon, and compute
  `sum(value_i * (t_{i+1} - t_i)) / (end - start)` over the window after warm-up.
- A customer-average wait and a time-average queue length are different quantities; Little's law links them only in
  steady state (`mean queue = lambda * mean wait`), which makes it a useful consistency check.
- Sampling resource state right after `request()` can show the slot already taken or the request already queued.
  Record domain events (arrival, service start, departure) in your own code instead of reading resource internals.
- Wrapping `env.step` or reading `env._queue` uses private details; fine for a debug trace, not for results.
- Time is unitless. Name parameters with their unit (`service_minutes`) and convert once at the boundary.
