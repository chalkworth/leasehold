"""leasehold stands alone: its modules import only the standard library, pydantic and leasehold
itself (and `mcp_server` the `mcp` extra), and each imports cleanly in a fresh interpreter."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

from support import CHILD_ENV, SRC

import leasehold

PACKAGE = Path(leasehold.__file__).resolve().parent
#: The modules that may import an optional extra, and which.
EXTRAS = {"mcp_server.py": {"mcp"}}


def _modules() -> list[Path]:
    return sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts)


def test_every_module_imports_only_the_stdlib_pydantic_and_leasehold():
    allowed = set(sys.stdlib_module_names) | {"pydantic", "__future__", "leasehold"}
    modules = _modules()
    assert len(modules) >= 7
    for source in modules:
        may = allowed | EXTRAS.get(source.relative_to(PACKAGE).as_posix(), set())
        tree = ast.parse(source.read_text(), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # relative: inside the package
                    continue
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            assert set(roots) <= may, f"{source.relative_to(SRC)} imports {roots}"


def test_every_module_imports_on_its_own():
    """Each module, imported first in its own interpreter: no import cycle hides behind the
    order the test suite happens to import them in."""
    names = sorted(
        ".".join(("leasehold", *p.relative_to(PACKAGE).with_suffix("").parts)).removesuffix(
            ".__init__"
        )
        for p in _modules()
        if p.stem != "__main__"
    )
    for name in names:
        result = subprocess.run(
            [sys.executable, "-c", f"import {name}"],
            capture_output=True,
            text=True,
            timeout=60,
            env=CHILD_ENV,
            check=False,
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"
