# Working on leasehold

Notes for anyone changing this repository, people and coding agents alike. `CLAUDE.md` points
here.

## Checks

```sh
uv sync
uv run pytest -q          # about a minute; daemon tests use real unix sockets under /tmp
uv run ruff check && uv run ruff format --check
uv run lint-imports       # the layer contracts in .importlinter
```

`pre-commit install` runs ruff and the import contracts on every commit.

## Shape of the code

- The library is pure: `model` (pools), `admission` (the decision), `ledger` (SQLite),
  `quota`. It never imports the daemon. `.importlinter` enforces the layers; fix an upward
  import rather than listing it as an exception.
- The daemon is `machine` (config) → `agent` (requests, ticks) → `api` (routes, no sockets) →
  `server` (the unix socket). `status` builds the status object as a pure function.
- `paths` says where each mode (system, user) keeps things; `discovery` is how a client finds
  a daemon; `doctor` checks a setup; `template` renders the config `init` writes.
- `cli` is a package: one module per group of commands, each with a `register(sub)` and
  handlers `(args, ctx) -> exit code`. A new command goes in the module of its group, or a new
  module listed in `cli/__init__.py`.
- Anything that talks to a daemon (`hold`, `handoff`, `report`, the views) does so through
  `leasehold.client` only.
- One module, one job. A new behaviour that does not belong to an existing module's docstring
  gets its own module.

## Conventions

- Comments and docstrings state what is guaranteed, once, in plain words. No task history.
- New behaviour comes with a test that would fail without it. Daemon behaviour is tested
  against a real daemon on a temporary socket (`tests/support.py`).
- Every change to a public name, config key, CLI flag, exit code, status field or error code is
  listed in `CHANGELOG.md` under the next release.
