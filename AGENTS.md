# Working on leasehold

Notes for anyone changing this repository, people and coding agents alike. `CLAUDE.md` points
here.

## Checks

```sh
uv sync --group bench     # the bench group adds SimPy; without it tests/test_sim.py is skipped
uv run pytest -q          # about a minute; daemon tests use real unix sockets under /tmp
uv run ruff check && uv run ruff format --check
uv run lint-imports       # the layer contracts in .importlinter
```

`pre-commit install` runs ruff and the import contracts on every commit.

## Changes reach `main` by pull request

- `main` takes no direct pushes. Work on a branch, open a pull request, and let CI (`lint`,
  `test (3.12)`, `test (3.13)`, `test (3.14)`, `build`) pass on it, up to date with `main`.
- Merges are squash merges, made by the maintainer. A coding agent opens the pull request and
  stops there: it never merges, approves or enables auto-merge, even when its credentials would
  let it.
- Review threads are resolved before merging. The branch is deleted on merge.
- A release is a `v*` tag on `main` and a published GitHub release, which publishes to PyPI.

## Shape of the code

- The library is pure: `model` (pools), `admission` (the decision), `elastic` (elastic
  leases' targets), `ledger` (SQLite), `quota`. It never imports the daemon. `.importlinter` enforces the layers; fix an upward
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
- `.agents/skills/` and `.claude/skills/` hold skills vendored from
  [chalkworth/skills](https://github.com/chalkworth/skills), pinned in `.agents/skills.toml` and
  `.agents/skills.lock.json`. Don't edit them here: change the skill upstream, bump `ref`, and run
  `uvx --from git+https://github.com/chalkworth/skills@<ref> chalk-skills vendor`. CI fails on
  a local edit.
