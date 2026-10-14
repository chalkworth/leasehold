# Security

## Reporting a problem

Please report a vulnerability privately, through GitHub's **Report a vulnerability** button on
the repository's Security tab, not in a public issue. Say what an attacker can do, on what
setup, and how to reproduce it. You will get an answer within a week, and a fix or a plan
within 30 days.

Supported: the latest release. leasehold is before 1.0, so fixes go into a new release rather
than back into old ones.

## What leasehold trusts

leasehold decides who may use a machine's capacity. It is not a sandbox: it never confines
what a process does once it runs. Its trust model:

- **The socket is the boundary.** The daemon answers on a unix socket. In user mode it is the
  user's own (`0600`, in a `0700` directory), so only that user can reach it. In system mode it
  is `0660` with group `socket_group`: every member may take leases.
- **The kernel names the user.** Each request is charged to the uid the kernel reports for
  the connection (`SO_PEERCRED`), never to anything the request says. A user may renew,
  release or give up only their own leases and places in line.
- **Administration is narrower.** Switching lending (`/v1/lend`) and overriding a pool's
  `max` (`/v1/pools/<name>`) need root, the daemon's own user, or a member of `admin_group`.
- **Fencing is by process identity or by TTL.** A lease held by a process ends when that
  process (pid and start time, so a reused pid does not count) is gone; others end when their
  holder stops renewing. A `/proc` mounted `hidepid` weakens the first to the pid alone:
  `leasehold doctor` says so.
- **Observed programs** (`[[observe]]`) are found by reading `/proc`: the daemon reads every
  process's command line it can see. Rules match names, never run anything.
- **`leasehold ui`** listens on `127.0.0.1` only, and every data route needs the random token
  in the link it prints. The token never appears in a process's arguments (the browser is
  handed a private file), the page moves it out of its address into the tab's
  `sessionStorage`, and requests for any other `Host` are refused (DNS rebinding). The page is
  read-only.
- **`leasehold report`** sends the status object to a URL you configure: https unless it is
  loopback, the token read from a file that is not world-readable (`0640 root:leasehold` in
  the unit), redirects not followed.
- **Agent integrations never run what a model asks.** The Claude Code hooks and PATH shims
  read a command to decide its lease and never change it; the MCP server has no tool that
  runs a command.

## Known limits

- Anyone who can reach the socket can fill the queue up to the per-user limits (`quota`), and
  hold capacity until their leases end. Give the socket to people you would give a shell to.
- The ledger (SQLite, in the daemon's state directory) is trusted. Whoever can write it can
  grant themselves anything; keep its directory to the daemon's user.
- Matching commands (`[commands]`) reads the command line as the shell would split it, without
  running it. A program reached through a variable, an alias or a script is not seen. It
  keeps agents from piling work onto the machine; it is not a control.
