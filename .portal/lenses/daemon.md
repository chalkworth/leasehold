+++
id = "daemon"
title = "What the daemon promises"
inputs = ["diff", "files", "standards"]
applies_to = ["src/leasehold/"]
required = true
version = 1
+++
leasehold decides who may use a machine's CPUs and memory, and other tools trust its answer.
Read this change for a way it breaks a promise the daemon makes.

- Admission: is `admission` still a pure function of its inputs? Can a lease get more slots
  or memory than the pool has, or a lease that grows pass over a waiting minimum?
- State: can the ledger and what the daemon reports disagree, for example after a crash, a
  restart, an expired lease or a holder that vanished? Is every write that must be atomic
  done in one transaction?
- Concurrency: is shared state read and written under the lock that guards it? Can a request
  and the tick race so that a lease is lost, granted twice or never ended?
- Clients: does a status field, error code, exit code or config key change without an entry
  in `CHANGELOG.md`? Does an older client or config still get a clear answer?

Name the sequence of events that leads to the wrong result. A finding that only says the code
looks racy is not one.
