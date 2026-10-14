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

- **It waited.** Nothing to do: it ran once there was room.
- **Refused: "machine busy for 20s: cargo test (session cc-...) holds 8 slots; ...; queued
  #1".** The message names what holds the machine and where your command stood in line. Do not
  retry straight away. Do lighter work (read code, edit, run one test), or check `leasehold
  status`, then run it again.
- **"machine busy ...; running pytest without a lease".** It ran anyway. If it was slow, that
  is why.

## Exit codes of `leasehold hold` and `leasehold acquire`

| Code | Meaning |
| -- | -- |
| 75 | Busy: still waiting when the timeout came. Retry later. |
| 76 | Revoked: lending ended, the command was stopped. Retry. |
| 77 | Refused: the request can never be granted (too heavy for its pool). Ask for less. |
| 69 | No daemon (`hold` only with `--if-unavailable fail`; it otherwise runs the command without a lease). Run the command without leasehold. |

Never work around a lease: running a heavy command under another name only makes everyone's
work slower, yours included.
