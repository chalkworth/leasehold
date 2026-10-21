#!/usr/bin/env python3
"""Advisory notes on test bloat, for a PostToolUse hook in Claude Code and Codex.

After an edit to a Python test file, the hook compares the file before and after the edit and
reports only what the edit added to tests it added or changed:

- dup: a new test whose body matches another test's apart from literals and local names, in the
  same file or, under the same test name, in another test file of the repository.
- sleep: a new `time.sleep` or `asyncio.sleep` longer than SLEEP_S among the test's own
  statements. A sleep inside a helper the test defines holds something open and is left alone.
- timeout: a timeout-like keyword whose literal value went up in a test that already had it.
- retry: a new flaky, rerun or retry marker.
- skip: a new unconditional skip or xfail. A skip under `if`, a `skipif`, or an xfail with a
  condition or `raises=` is a guard and stays quiet.
- prose: a new assertion against an English sentence of PROSE_WORDS words or more that the test
  does not feed in itself.
- table: stacked parametrize decorators that gained an axis and now multiply to TABLE_ROWS cases
  or more, TABLE_GROWTH or more of them new, net of tests the same edit folded in.

Each (file, kind) and each (file, test) is reported once per session and agent. A message holds
at most MAX_NOTES notes, and a session and agent get at most SESSION_CAP messages. A
`# test-nudge: ok` comment on or above a test silences it. `TEST_NUDGE=off` silences the hook.

Where the text before the edit comes from:

- Edit and Write: `tool_response.originalFile`. Claude Code sends it, but its hooks reference
  does not document it. Without it, the hook compares the file on disk with git's staged copy,
  and says nothing when git has no staged copy.
- Bash: `tool_response.bashEditDiff.changedFiles` names the files a command changed, and each
  one is compared with git's staged copy. Claude Code fills the field in auto and
  bypassPermissions modes, and in every mode when `bashEditDiffEnabled` is set. Without it,
  Bash edits are not checked.
- Codex apply_patch: the hunks in `tool_input.command`, undone on the file on disk.

The hook reads the event on stdin and prints one `additionalContext` JSON object or nothing. It
never blocks, and it always exits 0. Any error ends in silence.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

SLEEP_S = 0.1
PROSE_WORDS = 6
PROSE_PLAIN_SHARE = 0.8  # share of words that must be plain English words
TABLE_ROWS = 8
TABLE_GROWTH = 4
MIN_DUP_NODES = 12  # a body smaller than this is too generic to call a duplicate
MOVE_MIN_TESTS = 3  # this many cross-file matches to one file, half the new tests: a move
MAX_NOTES = 2  # per message
SESSION_CAP = 4  # messages per session and agent
GIT_TIMEOUT_S = 2

TEST_PATH = re.compile(r"(^|/)(tests?/.*\.py|test_[^/]*\.py|[^/]*_test\.py)$")
SUPPORT_PATH = re.compile(
    r"(^|/)(conftest|support|helpers?|fakes?)\.py$|/(fakebin|fixtures|golden)/"
)
TIMEOUT_KW = re.compile(r"(^|_)(timeout|deadline|wait|grace)(_s|_ms|_secs?)?$", re.IGNORECASE)
RETRY_NAMES = {"flaky", "rerun", "reruns", "retry", "retries", "repeat"}
RETRY_KEYWORDS = {"reruns", "retries", "max_runs"}
SLEEPS = {"time.sleep", "asyncio.sleep", "sleep", "anyio.sleep", "trio.sleep"}
OK_MARK = "test-nudge: ok"
WORD = re.compile(r"^[A-Za-z][a-z']*[,.;:!?]?$")
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


@dataclass(frozen=True)
class Note:
    kind: str
    subject: str  # the test it is about, part of the rate-limit key
    text: str
    where: str = ""  # for a cross-file duplicate, the other file


# ---------------------------------------------------------------- reading tests


def tests_of(tree: ast.Module) -> dict:
    """Test functions by qualified name, at module level and one class level down."""
    out = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test"
        ):
            out[node.name] = node
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef | ast.AsyncFunctionDef) and sub.name.startswith(
                    "test"
                ):
                    out[f"{node.name}::{sub.name}"] = sub
    return out


def dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Call):
        return dotted(node.func)
    return ""


def own_nodes(fn):
    """Nodes of the test's own statements, not of functions, lambdas or classes it defines."""
    stack = [s for s in fn.body if not isinstance(s, NESTED)]
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, NESTED))


def body_of(fn) -> list:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    return body


def _num(node) -> float | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, int | float)
        and not isinstance(node.value, bool)
    ):
        return float(node.value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):  # 5 * 60
        a, b = _num(node.left), _num(node.right)
        return a * b if a is not None and b is not None else None
    return None


# ---------------------------------------------------------------- duplicate shape


def _locals(fn) -> set[str]:
    names = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
    return names


def shape(fn) -> tuple[str, int]:
    """Hash of the body with literals blanked and locals renamed by first use, and its node
    count. Decorators, name and docstring are dropped. Globals and attribute names stay, so two
    tests that call different helpers never match."""
    fn = copy.deepcopy(fn)
    local, rename = _locals(fn), {}
    module = ast.Module(body=body_of(fn), type_ignores=[])
    size = 0
    for node in ast.walk(module):
        size += 1
        if isinstance(node, ast.Constant):
            node.value = "_"
        elif isinstance(node, ast.Name) and node.id in local:
            node.id = rename.setdefault(node.id, f"v{len(rename)}")
        elif isinstance(node, ast.arg):
            node.arg = rename.setdefault(node.arg, f"v{len(rename)}")
    digest = hashlib.sha1(ast.dump(module, include_attributes=False).encode()).hexdigest()
    return digest, size


def literal_dump(fn) -> str:
    return ast.dump(ast.Module(body=body_of(fn), type_ignores=[]))


def parametrized(fn) -> bool:
    return any("parametrize" in dotted(d) for d in fn.decorator_list)


def dup_text(name: str, other: str, target, fn, cross_file: bool) -> str:
    if cross_file:
        return (
            f"{name} has the same body as {other} apart from literals, so the behaviour is "
            "now tested in two files; one test parametrized over both cases would cover it once."
        )
    if target is not None and literal_dump(target) == literal_dump(fn):
        return (
            f"{name} has the same body as {other}; only the parametrize rows differ, and they "
            f"fit in {other}'s table."
        )
    if target is not None and parametrized(target):
        return (
            f"{name} has the same body as {other} apart from literals; it fits as a row of "
            f"{other}'s parametrize table."
        )
    return (
        f"{name} has the same body as {other} apart from literals; the two fit as rows of one "
        "parametrize table."
    )


def duplicates(old: dict, new: dict, added: list, repo_shapes: dict, ok) -> list[Note]:
    """Added tests that match a surviving old test, another new test, or (same name) a test in
    another file. A whole file moved or copied is not reported."""
    index: dict[str, str] = {}
    for name, fn in old.items():
        if name in new:  # a test removed in this edit is not a match target
            index.setdefault(shape(fn)[0], name)
    for name in new:
        if name not in added:
            index.setdefault(shape(new[name])[0], name)
    notes = []
    for name in added:
        fn = new[name]
        digest, size = shape(fn)
        other, where = index.get(digest), ""
        theirs = repo_shapes.get(digest, "")
        if other is None and theirs.rsplit("::", 1)[-1] == name.rsplit("::", 1)[-1]:
            other, where = theirs, theirs.split("::", 1)[0]
        if other and size >= MIN_DUP_NODES and not ok(fn):
            text = dup_text(name, other, new.get(other), fn, bool(where))
            notes.append(Note("dup", name, text, where))
        index.setdefault(digest, name)
    by_file: dict[str, int] = {}
    for note in notes:
        if note.where:
            by_file[note.where] = by_file.get(note.where, 0) + 1
    moved = {f for f, c in by_file.items() if c >= MOVE_MIN_TESTS and c * 2 >= len(added)}
    return [n for n in notes if not n.where or n.where not in moved]


# ---------------------------------------------------------------- sleeps, timeouts, markers


def sleeps(fn) -> list[float]:
    out = []
    for node in own_nodes(fn):
        if isinstance(node, ast.Call) and dotted(node.func) in SLEEPS and node.args:
            value = _num(node.args[0])
            if value is not None and value > SLEEP_S:
                out.append(value)
    return out


def timeouts(fn) -> dict[str, float]:
    """Largest literal per timeout-like keyword or timeout marker, in seconds."""
    out: dict[str, float] = {}
    for node in [*ast.walk(fn), *fn.decorator_list]:
        if not isinstance(node, ast.Call):
            continue
        if dotted(node.func).endswith("mark.timeout") and node.args:
            value = _num(node.args[0])
            if value is not None:
                out["mark.timeout"] = max(out.get("mark.timeout", 0), value)
        for kw in node.keywords:
            value = _num(kw.value)
            if kw.arg and TIMEOUT_KW.search(kw.arg) and value is not None:
                value = value / 1000 if kw.arg.lower().endswith("_ms") else value
                out[kw.arg] = max(out.get(kw.arg, 0), value)
    return out


def markers(fn) -> set[str]:
    """Retry markers and unconditional skips, as 'retry:<name>' and 'skip:<name>'."""
    out = set()
    for dec in fn.decorator_list:
        name = dotted(dec)
        last = name.rsplit(".", 1)[-1]
        if last in RETRY_NAMES or "flaky" in name:
            out.add(f"retry:{last}")
        if name.endswith(("mark.skip", "mark.xfail")):
            targeted = isinstance(dec, ast.Call) and (
                dec.args or any(k.arg in {"condition", "raises"} for k in dec.keywords)
            )
            if not targeted:
                out.add(f"skip:{last}")
        if isinstance(dec, ast.Call):
            out.update(f"retry:{k.arg}" for k in dec.keywords if k.arg in RETRY_KEYWORDS)
    for stmt in body_of(fn):  # bare statements only: a skip under `if` is a guard
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            name = dotted(stmt.value.func)
            if name in {"pytest.skip", "pytest.xfail"}:
                out.add(f"skip:{name}")
    return out


# ---------------------------------------------------------------- prose


def is_sentence(text: str) -> bool:
    """PROSE_WORDS words or more, nearly all plain English words, on at most two lines."""
    if text.count("\n") > 1:
        return False
    words = text.split()
    if len(words) < PROSE_WORDS:
        return False
    return sum(bool(WORD.match(w)) for w in words) / len(words) >= PROSE_PLAIN_SHARE


def _asserted_strings(node) -> list:
    if isinstance(node, ast.Compare) and any(
        isinstance(op, ast.Eq | ast.In | ast.NotEq) for op in node.ops
    ):
        return [
            s
            for s in [node.left, *node.comparators]
            if isinstance(s, ast.Constant) and isinstance(s.value, str)
        ]
    if isinstance(node, ast.Call) and dotted(node.func) in {"pytest.raises", "pytest.warns"}:
        return [
            k.value
            for k in node.keywords
            if k.arg == "match"
            and isinstance(k.value, ast.Constant)
            and isinstance(k.value.value, str)
        ]
    return []


def prose(fn) -> list[str]:
    """Sentences the test asserts on and does not pass in anywhere else."""
    asserted = [s for node in ast.walk(fn) for s in _asserted_strings(node)]
    asserted_ids = {id(s) for s in asserted}
    elsewhere = {
        n.value
        for n in ast.walk(fn)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in asserted_ids
    }
    return [
        s.value
        for s in asserted
        if is_sentence(s.value) and not any(s.value in e for e in elsewhere)
    ]


# ---------------------------------------------------------------- parametrize tables


def table(fn) -> tuple[int, int] | None:
    """(cases the parametrize decorators produce, stacked axes), or None when there is no
    parametrize or one of its row lists is not a literal."""
    total, axes = 1, 0
    for dec in fn.decorator_list:
        if isinstance(dec, ast.Call) and "parametrize" in dotted(dec.func) and len(dec.args) >= 2:
            rows = dec.args[1]
            if not isinstance(rows, ast.List | ast.Tuple):
                return None
            total *= max(len(rows.elts), 1)
            axes += 1
    return (total, axes) if axes else None


# ---------------------------------------------------------------- one test's notes


def test_notes(name: str, fn, prev, folded: int) -> list[Note]:
    """Notes on what one added or changed test gained; prev is its old version or None."""
    notes = []
    new_sleeps, old_sleeps = sleeps(fn), sleeps(prev) if prev else []
    if len(new_sleeps) > len(old_sleeps) or (
        new_sleeps and max(new_sleeps) > max(old_sleeps or [0])
    ):
        notes.append(
            Note(
                "sleep",
                name,
                f"{name} sleeps {max(new_sleeps):g}s in its own body; a wait on the condition "
                "itself is faster and does not flake under load.",
            )
        )
    if prev is not None:
        old_t = timeouts(prev)
        for kw, value in timeouts(fn).items():
            if kw in old_t and value > old_t[kw]:
                notes.append(
                    Note(
                        "timeout",
                        name,
                        f"{name} raises {kw} from {old_t[kw]:g}s to {value:g}s; a longer wait "
                        "usually hides a race without fixing it.",
                    )
                )
                break
    for mark in sorted(markers(fn) - (markers(prev) if prev else set())):
        kind, what = mark.split(":", 1)
        if kind == "retry":
            text = f"{name} gains a {what} marker; a retry hides the flake and leaves its cause in place."
        else:
            text = (
                f"{name} gains an unconditional {what}; a test that never runs still costs "
                "reading and proves nothing."
            )
        notes.append(Note(kind, name, text))
    old_prose = set(prose(prev)) if prev else set()
    fresh = [p for p in prose(fn) if p not in old_prose]
    if fresh:
        short = fresh[0] if len(fresh[0]) <= 50 else fresh[0][:47] + "..."
        notes.append(
            Note(
                "prose",
                name,
                f'{name} asserts on the sentence "{short}", which breaks on a reword with no '
                "change in behaviour; an exit code, field or short stable token would hold.",
            )
        )
    now = table(fn)
    if now:
        cases, axes = now
        before = table(prev) if prev else None
        old_cases, old_axes = before or ((1, 0) if prev else (0, 0))
        grew = cases - old_cases - folded
        if cases >= TABLE_ROWS and grew >= TABLE_GROWTH and axes >= 2 and axes > old_axes:
            notes.append(
                Note(
                    "table",
                    name,
                    f"{name} stacks {axes} parametrize axes into {cases} cases; a cross product "
                    "runs every combination, though most of them take the same path through the "
                    "code.",
                )
            )
    return notes


def analyse(before: str, after: str, repo_shapes=None) -> list[Note]:
    """Notes on what the edit from before to after added. repo_shapes maps shape hashes to
    'file::test' in other files, or is a callable that builds that map; it is called only when
    the edit added a test."""
    new_tree = ast.parse(after)
    try:
        old_tree = ast.parse(before) if before else ast.Module(body=[], type_ignores=[])
    except SyntaxError:
        old_tree = ast.Module(body=[], type_ignores=[])
    old, new = tests_of(old_tree), tests_of(new_tree)
    lines = after.splitlines()

    def ok(fn) -> bool:
        start = min([d.lineno for d in fn.decorator_list] + [fn.lineno]) - 1
        end = getattr(fn, "end_lineno", fn.lineno)
        return any(OK_MARK in line for line in lines[max(start - 1, 0) : end])

    added = [n for n in new if n not in old]
    changed = [n for n in new if n in old and ast.dump(new[n]) != ast.dump(old[n])]
    if not added and not changed:
        return []
    if callable(repo_shapes):
        repo_shapes = repo_shapes() if added else {}
    notes = duplicates(old, new, added, repo_shapes or {}, ok)
    folded = sum((table(old[r]) or (1,))[0] for r in old if r not in new)
    for name in added + changed:
        if not ok(new[name]):
            notes.extend(test_notes(name, new[name], old.get(name), folded))
    return notes


# ---------------------------------------------------------------- repository index


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _save_json(path: str, data) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def _file_shapes(full: str) -> dict[str, str]:
    shapes: dict[str, str] = {}
    try:
        with open(full, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
    except (OSError, SyntaxError, ValueError):
        return shapes
    for name, fn in tests_of(tree).items():
        digest, size = shape(fn)
        if size >= MIN_DUP_NODES:
            shapes.setdefault(digest, name)
    return shapes


def repo_index(root: str, skip: str, cache_path: str) -> dict[str, str]:
    """Shape hash -> 'file::test' for every other test file git knows, cached by mtime."""
    cache = _load_json(cache_path) or {}
    try:
        listed = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "*.py"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        ).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return {}
    out: dict[str, str] = {}
    fresh = {}
    for rel in listed:
        full = os.path.join(root, rel)
        if not is_test(rel) or os.path.abspath(full) == os.path.abspath(skip):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        stamp = f"{st.st_mtime_ns}:{st.st_size}"
        hit = cache.get(rel)
        shapes = hit[1] if hit and hit[0] == stamp else _file_shapes(full)
        fresh[rel] = [stamp, shapes]
        for digest, name in shapes.items():
            out.setdefault(digest, f"{rel}::{name}")
    _save_json(cache_path, fresh)
    return out


# ---------------------------------------------------------------- reading the event


def is_test(path: str) -> bool:
    return path.endswith(".py") and bool(TEST_PATH.search(path)) and not SUPPORT_PATH.search(path)


def staged(root: str, path: str) -> str | None:
    """Git's staged copy of path, or None when git has none."""
    try:
        result = subprocess.run(
            ["git", "show", f":./{os.path.relpath(path, root)}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def read(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def reverse_patch(after: str, hunks: list[list[str]]) -> str | None:
    """Undo apply_patch hunks on the file's new text; None when a hunk cannot be found."""
    text = after
    for hunk in hunks:
        old = "\n".join(line[1:] for line in hunk if line[:1] in (" ", "-"))
        new = "\n".join(line[1:] for line in hunk if line[:1] in (" ", "+"))
        if new not in text:
            return None
        text = text.replace(new, old, 1)
    return text


def patch_files(patch: str) -> list[tuple[str, bool, list[list[str]]]]:
    """(path, added, hunks) for each file a Codex apply_patch body adds or updates."""
    out: list = []
    current, hunk = None, None
    for line in patch.splitlines():
        match = re.match(r"\*\*\* (Add|Update) File: (.+)", line)
        if match:
            current, hunk = (match.group(2).strip(), match.group(1) == "Add", []), None
            out.append(current)
        elif line.startswith("*** "):
            current = current if line.startswith("*** End of File") else None
        elif current is not None and line.startswith("@@"):
            hunk = []
            current[2].append(hunk)
        elif current is not None and line[:1] in (" ", "-", "+"):
            if hunk is None:
                hunk = []
                current[2].append(hunk)
            hunk.append(line)
    return out


def _edit_tool(tool: str, tin: dict, resp: dict, root: str) -> list[tuple[str, str, str]]:
    path = tin.get("file_path") or resp.get("filePath") or ""
    if not is_test(path):
        return []
    before = resp.get("originalFile")
    if tool == "Write":
        after = tin.get("content", "")
        if before is None:
            before = "" if resp.get("type") == "create" else staged(root, path)
    elif before is not None:
        after = before
        for edit in tin.get("edits") or [tin]:
            old, new = edit.get("old_string", ""), edit.get("new_string", "")
            after = after.replace(old, new, -1 if edit.get("replace_all") else 1)
    else:
        after, before = read(path), staged(root, path)
    return [] if before is None else [(path, before, after)]


def _bash_tool(resp: dict) -> list[tuple[str, str | None]]:
    """(path, before) for each test file a Bash command changed, from bashEditDiff."""
    diff = resp.get("bashEditDiff")
    if not isinstance(diff, dict) or diff.get("skipped"):
        return []
    created = {
        f.get("filePath")
        for f in diff.get("files") or []
        if isinstance(f, dict) and f.get("created")
    }
    return [
        (path, "" if path in created else None)
        for path in diff.get("changedFiles") or []
        if isinstance(path, str) and is_test(path)
    ]


def edits(event: dict) -> list[tuple[str, str, str]]:
    """(path, before, after) for each test file the tool call changed and whose text before the
    call is known."""
    tool = event.get("tool_name", "")
    tin = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    resp = event.get("tool_response") if isinstance(event.get("tool_response"), dict) else {}
    root = event.get("cwd") or os.getcwd()
    if tool in {"Write", "Edit", "MultiEdit"}:
        return _edit_tool(tool, tin, resp, root)
    out = []
    if tool == "apply_patch":
        for rel, added, hunks in patch_files(tin.get("command", "")):
            path = os.path.join(root, rel)
            if is_test(path) and os.path.exists(path):
                after = read(path)
                before = "" if added else reverse_patch(after, hunks)
                before = staged(root, path) if before is None else before
                if before is not None:
                    out.append((path, before, after))
    elif tool == "Bash":
        for path, known in _bash_tool(resp):
            before = known if known is not None else staged(root, path)
            if before is not None and os.path.exists(path):
                out.append((path, before, read(path)))
    return out


# ---------------------------------------------------------------- rate limit and output


def state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "test-nudge")


def rate_limit(event: dict, path: str, notes: list[Note]) -> list[Note]:
    """Once per (file, kind) and once per (file, test) per session and agent, at most
    MAX_NOTES notes a message and SESSION_CAP messages."""
    session = f"{event.get('session_id', 'x')}-{event.get('agent_id', 'main')}"
    state_path = os.path.join(state_dir(), re.sub(r"[^A-Za-z0-9_-]", "_", session) + ".json")
    state = _load_json(state_path) or {"seen": [], "messages": 0}
    if state["messages"] >= SESSION_CAP:
        return []
    seen, keep = set(state["seen"]), []
    for note in notes:
        keys = {f"{path}|{note.kind}", f"{path}|{note.subject}"}
        if keys & seen:
            continue
        seen |= keys
        keep.append(note)
    keep = keep[:MAX_NOTES]
    if keep:
        state["messages"] += 1
        state["seen"] = sorted(seen)
        _save_json(state_path, state)
    return keep


def run(event: dict) -> str:
    """The hook's stdout for one event: one additionalContext JSON object, or ''."""
    root = event.get("cwd") or os.getcwd()
    cache = os.path.join(
        state_dir(), "index-" + hashlib.sha1(root.encode()).hexdigest()[:12] + ".json"
    )
    parts = []
    for path, before, after in edits(event):
        found = analyse(before, after, lambda path=path: repo_index(root, path, cache))
        notes = rate_limit(event, path, found)
        if notes:
            parts.append(f"{os.path.relpath(path, root)}: " + " ".join(n.text for n in notes))
    if not parts:
        return ""
    context = "test-nudge, " + " ".join(parts)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": context}}
    )


def main() -> None:
    if os.environ.get("TEST_NUDGE", "").lower() == "off":
        return
    try:
        event = json.load(sys.stdin)
        text = run(event) if isinstance(event, dict) else ""
    except Exception:  # noqa: BLE001 - an advisory hook must never interrupt work
        text = ""
    if text:
        print(text)


if __name__ == "__main__":
    main()
    sys.exit(0)
