# Contributing

Issues and pull requests are welcome. For anything larger than a fix, open an issue first, so
we can agree on the shape before you write it.

## Setup

```sh
git clone https://github.com/chalkworth/leasehold && cd leasehold
uv sync                                          # Python 3.12+, the dev tools included
uv tool install pre-commit && pre-commit install # ruff and the import contracts on each commit
```

## Checks

Everything CI runs:

```sh
uv run ruff check
uv run ruff format --check
uv run lint-imports          # which module may import which (.importlinter)
uv run pytest -q             # about two minutes; Linux only
```

Tests start real daemons on real unix sockets in temporary directories. A few need tools:
`make` 4.4 for the jobserver test, which is skipped without it.

## How the code is laid out

`AGENTS.md` describes the layers and the rules each module keeps. In short:

- The library (`model`, `admission`, `ledger`, `quota`) is pure policy and storage, and never
  imports the daemon.
- Everything that talks to a daemon (`hold`, the views, the agent integrations) goes through
  `leasehold.client`.
- The core depends on the standard library and pydantic only. Anything else is an optional
  extra (`mcp`).

## Changes that others depend on

The command line and its exit codes, the config file, the HTTP API and its `status` schema,
`--json` output and the library modules are public. A change to any of them gets a row in
`CHANGELOG.md`, saying what to do instead.

## Licence

By contributing, you agree that your contribution is licensed under the Apache License 2.0,
the licence of this project.
