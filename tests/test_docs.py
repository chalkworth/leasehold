"""The docs stay true to the code: the command-line reference is the parser's, and every link
between the repository's files resolves."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from leasehold.api import ROUTES

ROOT = Path(__file__).resolve().parents[1]
DOCS = [
    ROOT / "README.md",
    ROOT / "docs" / "reference.md",
    ROOT / "docs" / "bench.md",
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
    *sorted((ROOT / "examples").glob("*/README.md")),
    *sorted((ROOT / "integrations").rglob("*.md")),
]
LINK = re.compile(r"\]\(([^)\s]+)\)")


def _generator():
    spec = importlib.util.spec_from_file_location("gen", ROOT / "scripts" / "gen_cli_reference.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_reference_describes_every_api_route_and_no_other():
    text = (ROOT / "docs" / "reference.md").read_text()
    for method, path in ROUTES:
        assert f"`{method} {path}" in text, f"docs/reference.md does not describe {method} {path}"
    named = set(re.findall(r"`(GET|POST|PUT|PATCH|DELETE) (/v1/[^`?\s]*)", text))
    assert named <= set(ROUTES), f"docs/reference.md names routes there are not: {named}"


def test_the_command_line_reference_is_the_parsers():
    gen = _generator()
    text = gen.REFERENCE.read_text()
    assert gen.rewrite(text) == text, "run scripts/gen_cli_reference.py"


@pytest.mark.parametrize(
    "doc", [d for d in DOCS if d.exists()], ids=lambda d: str(d.relative_to(ROOT))
)
def test_links_between_files_resolve(doc):
    for target in LINK.findall(doc.read_text()):
        if re.match(r"[a-z]+:", target) or target.startswith("#"):
            continue  # a URL, or a place on the same page
        path = (doc.parent / target.split("#", 1)[0]).resolve()
        assert path.exists(), f"{doc.relative_to(ROOT)} links to {target}, which is not there"
