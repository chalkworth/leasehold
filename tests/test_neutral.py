"""No file the repository ships names a project, machine or host on a private list: the source,
tests, scripts, deploy files, examples, integrations, docs, workflows and the top-level
documents, the changelog included.

The list is kept as salted SHA-256 digests, so this file does not name what it looks for. Some
names count anywhere inside a word (`xyzmach_...`), others only as whole words, so that ordinary
words that contain them are spared."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCANNED = [
    "src", "tests", "scripts", "deploy", "examples", "integrations", "docs", ".github", "bench",
    ".claude-plugin", "README.md", "CHANGELOG.md", "SECURITY.md", "CONTRIBUTING.md",
    "AGENTS.md", "CLAUDE.md", "pyproject.toml",
]  # fmt: skip
SKIPPED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SALT = b"leasehold-neutral-v1:"
#: Names found anywhere inside a word: (length, digest prefix).
ANYWHERE = {
    (3, "5a759d0c023d52888ea9"),
    (17, "1afe92d4da54fce23ae4"),
    (17, "0d1f6d5a5ea97718ad4e"),
    (16, "6ea6a677a43bdf224a94"),
    (9, "e475534bc29cf6b80d15"),
    (6, "7f582dfb884f70b8d367"),
}
#: Names found only as whole words (not next to a letter or digit).
WHOLE = {
    (5, "38fea8b23c38d9c028e7"),
    (3, "d0ee845248f2e40027e5"),
    (5, "2fa20f59036ed7f2d596"),
}
_RUNS = re.compile(r"[a-z0-9_.\-]+")
_WORDS = re.compile(r"[a-z0-9]+")


def digest(word: str) -> str:
    return hashlib.sha256(SALT + word.encode()).hexdigest()[:20]


def named(text: str, anywhere=ANYWHERE, whole=WHOLE) -> set[str]:
    """The listed names in `text` (any case)."""
    text = text.lower()
    found = {w for w in set(_WORDS.findall(text)) if (len(w), digest(w)) in whole}
    lengths = {n for n, _ in anywhere}
    for run in set(_RUNS.findall(text)):
        for n in lengths:
            for i in range(len(run) - n + 1):
                part = run[i : i + n]
                if (n, digest(part)) in anywhere:
                    found.add(part)
    return found


def _files() -> list[Path]:
    out = []
    for name in SCANNED:
        path = ROOT / name
        if path.is_file():
            out.append(path)
        elif path.is_dir():
            out += [
                p
                for p in path.rglob("*")
                if p.is_file() and not SKIPPED_DIRS & set(p.relative_to(ROOT).parts)
            ]
    return sorted(out)


def _text(path: Path) -> str | None:
    """The file as text, or None for a binary file."""
    raw = path.read_bytes()
    if b"\0" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _where(path: Path, text: str, names: Iterable[str]) -> list[str]:
    return [
        f"{path.relative_to(ROOT)}:{number}"
        for number, line in enumerate(text.lower().splitlines(), 1)
        if any(name in line for name in names)
    ]


def test_no_shipped_file_names_anything_on_the_list():
    files = _files()
    assert any(p.parts[-2:] == ("leasehold", "model.py") for p in files)  # the scan reaches src
    found = []
    for path in files:
        text = _text(path)
        if text is not None and (names := named(text)):
            found += _where(path, text, names)
    assert found == [], "\n".join(found)


def test_the_scan_catches_each_kind_of_name_and_spares_ordinary_words():
    anywhere = {(7, digest("zorblax"))}
    whole = {(4, digest("quib"))}
    assert named("a token ZORBLAXmach_123 here", anywhere, whole) == {"zorblax"}
    assert named("path/to/zorblax-thing", anywhere, whole) == {"zorblax"}
    assert named("one Quib, two", anywhere, whole) == {"quib"}
    assert named("quibble quibs xquib", anywhere, whole) == set()
