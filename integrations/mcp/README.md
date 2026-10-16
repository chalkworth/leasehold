# leasehold for coding agents (MCP)

`leasehold mcp` is an MCP server on stdio. It lets a coding agent see how busy the machine is and
take a lease before heavy work, so a full test suite or a build waits its turn instead of
overloading a machine that people, CI and other agents share.

It needs the `mcp` extra:

```sh
uv tool install 'leasehold[mcp]'          # then the command is `leasehold mcp`
uvx --from 'leasehold[mcp]' leasehold mcp # or run it without installing
```

A daemon must be running (`leasehold serve`); `leasehold doctor` says which one a client finds.
The server finds it the way every leasehold command does; pass `--socket PATH` before `mcp` to
name one.

## The tools

- `machine_status`: free slots, each pool's use and queue, who waits and why, CPU, memory and IO
  pressure, and whether lending is open, in one paragraph ending in a recommendation ("2 slots
  free: a work lease of weight up to 2 would start now").
- `acquire_lease(weight, pool, kind, reason, wait_s, lease_id)`: ask for `weight` slots, about one
  per parallel job. It waits at most 20 s and answers granted, with the job count to use
  (`make -j4`), or queued, with the place in line and why.
- `wait_for_capacity(lease_id, wait_s)`: wait up to 20 s more for a queued lease.
- `release_lease(lease_id)`: give a lease back when the work is done.
- `list_my_leases()`: what this session holds and has waiting.

No tool runs a command, on purpose. A lease tells the agent how much of the machine it may
use; the agent runs the work with its own shell tool, under its own permissions. The server acts
only on leases it made itself (ids `mcp.<hex>`, labelled `agent=mcp`) and refuses any other id.

## How leases end

Leases die with the agent session. The server renews every lease it holds, and releases them
all when the agent closes the session (stdin ends) or on SIGTERM. If the server is killed
outright, it stops renewing and each lease ends one TTL (30 s) later.

A queued request keeps its place while the agent keeps asking about it: the server keeps the
place for 120 s after each `wait_for_capacity` or `acquire_lease` call naming its id, so the agent
can do other work between waits. If the daemon grants it meanwhile, the lease is handed over on
the next call. A place, or a grant, that sees no call for 120 s is given back.

A `lend` lease may be revoked when other work needs its slot. The tools then answer `revoked`,
never `granted`, and say to stop that work and release the lease.

## Configuration

Each block runs `leasehold mcp` from PATH. The files beside this README hold the same snippets.

### Claude Code

```sh
claude mcp add leasehold -- leasehold mcp                    # this project, for you
claude mcp add --scope project leasehold -- leasehold mcp    # in .mcp.json, for everyone
```

or, at the project root, `.mcp.json` ([`claude-code/.mcp.json`](claude-code/.mcp.json)):

```json
{
  "mcpServers": {
    "leasehold": {
      "type": "stdio",
      "command": "leasehold",
      "args": ["mcp"]
    }
  }
}
```

Docs: <https://code.claude.com/docs/en/mcp>

### Codex CLI

`~/.codex/config.toml`, or a trusted project's `.codex/config.toml`
([`codex/config.toml`](codex/config.toml)):

```toml
[mcp_servers.leasehold]
command = "leasehold"
args = ["mcp"]
```

or `codex mcp add leasehold -- leasehold mcp`. Codex's default tool timeout (60 s) is well above
the 20 s the server waits at most.

Docs: <https://learn.chatgpt.com/docs/extend/mcp?surface=cli>

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json` for every project
([`cursor/mcp.json`](cursor/mcp.json)):

```json
{
  "mcpServers": {
    "leasehold": {
      "type": "stdio",
      "command": "leasehold",
      "args": ["mcp"]
    }
  }
}
```

Docs: <https://cursor.com/docs/context/mcp>

### Gemini CLI

`~/.gemini/settings.json`, or `.gemini/settings.json` in the project
([`gemini/settings.json`](gemini/settings.json)):

```json
{
  "mcpServers": {
    "leasehold": {
      "command": "leasehold",
      "args": ["mcp"]
    }
  }
}
```

or `gemini mcp add leasehold leasehold mcp` (project scope; `--scope user` for every project).

Docs: <https://geminicli.com/docs/tools/mcp-server/>
