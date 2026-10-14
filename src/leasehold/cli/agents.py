"""`mcp`: leasehold for coding agents, as an MCP server on stdio (`leasehold.mcp_server`).

It needs the `mcp` extra; without it, `mcp` says how to install it and exits 2. Nothing is ever
written to stdout but the protocol."""

from __future__ import annotations

import os
import sys

from leasehold.cli.leases import ENV_SESSION, session

#: What `mcp` says when the MCP SDK is missing.
INSTALL = """\
leasehold: `leasehold mcp` needs the MCP SDK, which is the optional `mcp` extra:
  uv tool install 'leasehold[mcp]'
or run it without installing:
  uvx --from 'leasehold[mcp]' leasehold mcp
"""


def register(sub) -> None:
    s = sub.add_parser(
        "mcp",
        help="an MCP server (stdio) for coding agents: status and leases, no command running",
        description="An MCP server (stdio) for coding agents: status and leases, no command "
        "running. Experimental: may change before 1.0.",
    )
    s.add_argument(
        "--session",
        default=os.environ.get(ENV_SESSION),
        help=f"the session its leases are filed under (default ${ENV_SESSION}, else its own)",
    )
    s.set_defaults(run=_mcp)


def _mcp(args, ctx) -> int:
    from leasehold.mcp_server import MissingExtra, serve

    try:
        serve(ctx.client(), session=session(args.session))
    except MissingExtra:
        sys.stderr.write(INSTALL)
        return 2
    return 0
