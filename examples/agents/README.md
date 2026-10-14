# Coding agents sharing a machine

Coding agents run builds and test suites in bursts, often several agents per person. Give each
agent's heavy commands a lease and the machine takes them in turns, instead of running all of
them at once.

Two levels of turn-taking decide who goes next in a pool:

- **Users.** The daemon charges each lease to the Unix user on the other end of its socket. The
  next item is the one of the user with the fewest leases running in the pool, then the user served
  longest ago. A user with one item waiting gets in within one round, however many items another
  user has queued.
- **Sessions.** Inside one user's turn, that user's sessions take turns by the same rule. A
  session is whatever name the client gives (`--session`, or `LEASEHOLD_SESSION`); items without
  one share a single session per user. One session's own items run in the order they were asked
  for. More sessions only divide their user's own turns: they never take another user's.

In everyday use, `leasehold run -- AGENT` gives each agent a session of its own, and its heavy
commands take leases through the PATH shims; nothing needs `--session`. The script below names
the sessions by hand so that the order is plain to see.

So three agents run as three users (ann, bob and carol, each in the `leasehold` group of the
machine's daemon) share the machine as three users do. Three agents of one person share that
person's turns, one session each.

## Session fairness, against a daemon

`run.sh` shows the second level with one user. Start a daemon of your own on this directory's
`machine.toml` (one slot, so commands run one at a time), then run the script:

```sh
export LEASEHOLD_CONFIG=$PWD/machine.toml
leasehold serve &
./run.sh
```

`serve` refuses to start if a daemon of yours already answers on its socket: stop it first.

The script holds the slot, then queues five commands in this order: `a1`, `a2` and `a3` from
session `agent-a`, then `b1` from `agent-b` and `c1` from `agent-c`. Each is
`leasehold hold --session SESSION -- sh -c "echo NAME >>order"`. `leasehold leases` shows the
line before the slot frees:

```
LEASE                             KIND  POOL  W  USER  SESSION  EXPIRES  STATE
ab9c3cc3467144b58e8049d0c2f17a35  work  work  1  ann   setup    +13s     held
054c6e0a2ca641c0b3a94ea61b0e9d42  work  work  1  ann   agent-a  +15s     queued #1: pool work holds 1 of 1; this needs 1
14404dbeb79444078de5a26f7c3a8e01  work  work  1  ann   agent-b  +15s     queued #2: 1 ahead of it in pool work
8929a3b32ed046ab9361efda5e6b2c97  work  work  1  ann   agent-c  +15s     queued #3: 2 ahead of it in pool work
46cb7d57a8114866b83248059d4f1a6c  work  work  1  ann   agent-a  +15s     queued #4: 3 ahead of it in pool work
0f5f327ceb5143f4844a1fca3b8e7d20  work  work  1  ann   agent-a  +15s     queued #5: 4 ahead of it in pool work
```

Then the script frees the slot, and the commands run in turn:

```
ran in this order: a1 b1 c1 a2 a3
```

Without sessions (all five from one session, or none) the order would be the order of asking,
`a1 a2 a3 b1 c1`: agent-b and agent-c would wait behind all of agent-a's work.

## Several users

To see the first level, run the machine's daemon (`leasehold serve --system`, see the main
README) and give each agent its own Unix user in the `leasehold` group. Nothing in the commands
changes: the daemon reads the user from the socket. `[pools.work] per_user_max_running` caps how
many leases one user may have running in a pool at once, whatever their turn.

## Wiring an agent

Each of these gives an agent's heavy commands a lease without the agent typing
`leasehold hold`. They read the same `[commands]` rules (`leasehold commands` lists them;
`leasehold commands --test "uv run pytest -x"` says which one a command matches and its
weight). [`docs/reference.md`](../../docs/reference.md#coding-agents) has the detail.

**Claude Code.** Install the plugin from this repository:

```
/plugin marketplace add chalkworth/leasehold
/plugin install leasehold@leasehold
```

Before each Bash command, its hook takes a lease for a heavy one, in the Claude Code session's
turn (`cc-<session id>`), and gives it back when the command ends. The command is not changed.
Sub-agents share their parent's session.

**Any agent, through PATH.** Shims run `pytest`, `cargo`, `make` and the rest under a lease
for whoever calls them through PATH. Give each agent its own session, so its commands take
turns with the other agents':

```sh
leasehold shims install
export PATH="$HOME/.local/share/leasehold/shims:$PATH"
export LEASEHOLD_SESSION=agent-a     # in agent-a's environment; agent-b for the next
```

**Agents that speak MCP** (Codex, Cursor, Gemini CLI, Claude Code): `leasehold mcp` lets the
agent ask how busy the machine is and take a lease before heavy work, and releases what it holds
when the session ends. It never runs commands. [`integrations/mcp/`](../../integrations/mcp/)
has each client's config.

**The rest.** Paste [`integrations/AGENTS.md.snippet`](../../integrations/AGENTS.md.snippet)
into the repository's `AGENTS.md` or `CLAUDE.md`: it tells the agent to run heavy commands
under `leasehold hold` and what the exit codes mean.
