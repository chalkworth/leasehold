#!/usr/bin/env python3
"""Parse every ```mermaid fence in Markdown files with mermaid-cli, using a Chrome already here.

Usage: python3 check_mermaid.py PAGE.md [PAGE.md ...]

Prints one line per diagram, `path:line: ok` or `path:line: <parse error>`, and exits 0 when all
parse, 1 when any fails and 2 when it cannot run (no Chrome, no npx). Nothing is downloaded but
mermaid-cli itself: puppeteer's own Chrome download is switched off, and the Chrome is the first
found by `find_chrome`. `--find-chrome` prints that path and exits.

Standard library only.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FENCE = re.compile(r"^```mermaid[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
ON_PATH = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome")
# Browsers that tools such as Playwright and puppeteer keep in the user's cache, newest first.
CACHED = (
    ".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-*/chrome-headless-shell",
    ".cache/ms-playwright/chromium-*/chrome-linux*/chrome",
    ".cache/puppeteer/chrome-headless-shell/*/chrome-headless-shell-*/chrome-headless-shell",
    ".cache/puppeteer/chrome/*/chrome-*/chrome",
)
MAC_APPS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
MERMAID_CLI = ("npx", "-y", "@mermaid-js/mermaid-cli")


def _version_key(path: Path) -> list[int]:
    return [int(n) for n in re.findall(r"\d+", str(path))]


def find_chrome(home: Path | None = None) -> str | None:
    """A Chrome or Chromium executable: `$PUPPETEER_EXECUTABLE_PATH`, then PATH, then caches."""
    named = os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if named and os.access(named, os.X_OK):
        return named
    for name in ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    home = home or Path.home()
    for pattern in CACHED:
        candidates = [p for p in home.glob(pattern) if os.access(p, os.X_OK)]
        if candidates:
            return str(max(candidates, key=_version_key))
    return next((app for app in MAC_APPS if os.access(app, os.X_OK)), None)


def diagrams(text: str) -> list[tuple[int, str]]:
    """Each mermaid fence's body with the line number of its opening fence."""
    return [(text.count("\n", 0, m.start()) + 1, m.group(1)) for m in FENCE.finditer(text)]


def parse_error(body: str, chrome: str, work: Path) -> str | None:
    """mermaid-cli's error for one diagram, or None when it parses."""
    source, output = work / "diagram.mmd", work / "diagram.svg"
    source.write_text(body, encoding="utf-8")
    output.unlink(missing_ok=True)
    env = {**os.environ, "PUPPETEER_EXECUTABLE_PATH": chrome, "PUPPETEER_SKIP_DOWNLOAD": "true"}
    result = subprocess.run(
        [*MERMAID_CLI, "-q", "-i", str(source), "-o", str(output)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and output.is_file():
        return None
    lines = (result.stderr or result.stdout).strip().splitlines()
    return next((line for line in lines if "rror" in line), lines[0] if lines else "failed")


def main(argv: list[str]) -> int:
    chrome = find_chrome()
    if argv == ["--find-chrome"]:
        print(chrome or "no Chrome found")
        return 0 if chrome else 2
    if not argv:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    if chrome is None:
        print(
            "no Chrome or Chromium found; install one or set PUPPETEER_EXECUTABLE_PATH",
            file=sys.stderr,
        )
        return 2
    if shutil.which("npx") is None:
        print("npx not found; install Node.js to run mermaid-cli", file=sys.stderr)
        return 2
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for name in argv:
            path = Path(name)
            for line, body in diagrams(path.read_text(encoding="utf-8")):
                error = parse_error(body, chrome, Path(tmp))
                print(f"{path}:{line}: {error or 'ok'}")
                failed += error is not None
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
