---
name: leasehold
description: Use when a Bash command waited for, or ran without, a leasehold lease; when one was refused because the machine is busy; or before starting heavy work (builds, test suites, containers) on a shared machine.
---

# leasehold

This machine is shared. leasehold gives each heavy command (builds, test suites, container
builds) a lease of some slots, so commands take turns instead of piling on and slowing
everyone. This plugin takes the lease for you, before each heavy Bash command, and gives it
back afterwards. Light commands (git, ls, cat, grep) never wait.

## Before heavy work

`leasehold status` shows how busy the machine is: free slots, who holds what, who waits and
why. When it is full, do light work first (read code, write tests, edit) and run the heavy
command later.

`leasehold commands --test "<command>"` says whether a command takes a lease, and how many
slots.

## When a command waited, or was refused

- **"pytest waited 42s in line for its lease"**: the wait was not the command being slow.
- **"granted 4 slots; run it with at most 4 jobs"**: if you pick the worker count, use that.
- **Refused: "machine busy ...: cargo test (session cc-...) holds 8 slots, running 2m10s ...
  The command did not run."** No turn came in time. Do lighter work (read code, edit, run one
  test), for as long as "Retry in about N minutes" says when it says so, then run it again.
  A shim or `leasehold hold` in your command says the same and exits 75.
- **"machine busy ...; running pytest without a lease".** It ran anyway. If it was slow, that
  is why.

## When memory ended a command

"leasehold: ... killed for memory ... This is not a test failure." The kernel killed the
command because memory ran out (the machine's, or a limit's); its tests did not fail. Do not
debug the tests and do not re-run it unchanged. Do what the line says: wait for the other heavy
work to finish, or run fewer workers (`-n 2`) or a narrower selection.

## Exit codes of `leasehold hold` and `leasehold acquire`

| Code | Meaning |
| -- | -- |
| 75 | Busy: still waiting when the timeout came. Retry later. |
| 76 | Revoked: lending ended, the command was stopped. Retry. |
| 77 | Refused: the request can never be granted (too heavy for its pool). Ask for less. |
| 69 | No daemon (`hold` only with `--if-unavailable fail`; it otherwise runs the command without a lease). Run the command without leasehold. |

Never work around a lease: running a heavy command under another name only makes everyone's
work slower, yours included.
