"""Applies a model's code changes as anchored search/replace edits, never as whole files.

Ported from local_coding_agent/edit_engine.py, whose reasoning holds: a small local model is far
more reliable at "find this exact snippet, put this in its place" than at writing a whole file back
correctly, and a search that does not match is a free check - the edit is wrong, and the model can
be told exactly why before anything touches the disk.

Three changes from the original:
- A search that matches in more than one place is refused rather than applied to the first: which
  one was meant is a guess, and a wrong guess edits code nobody looked at.
- When only the whitespace-tolerant match finds the snippet, the replacement is re-indented to where
  it landed, so a model that dropped the indentation does not drop it into the file as well.
- Every path is resolved inside the workspace; ../ and absolute paths are refused, since a path is
  something the model wrote.
"""
import difflib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class EditError(Exception):
    """The edit cannot be applied. The message is for the model, to try again with."""


@dataclass
class EditResult:
    file: str
    diff: str


def inside(root, relative):
    """The absolute path of `relative` under `root`, or EditError when it would leave it."""
    if not relative or os.path.isabs(relative) or relative.startswith(("/", "\\")):
        raise EditError(f"use a path relative to the project root, not {relative!r}")
    base = Path(root).resolve()
    target = (base / relative).resolve()
    if target != base and base not in target.parents:
        raise EditError(f"{relative} is outside the project")
    if ".git" in target.relative_to(base).parts:
        raise EditError("the .git directory is not to be edited")
    return target


def _strip(text):
    return "\n".join(line.strip() for line in text.splitlines())


def _indent_of(line):
    return line[:len(line) - len(line.lstrip())]


def _find(content, search):
    """(start, end, fuzzy) of the one place `search` is, or EditError saying why not."""
    exact = content.count(search)
    if exact == 1:
        start = content.find(search)
        return start, start + len(search), False
    if exact > 1:
        raise EditError(f"the search block appears {exact} times in the file; include more of the "
                        f"surrounding lines so it matches only the place you mean")

    # Whitespace-tolerant: the same lines, indented differently
    lines = content.splitlines(keepends=True)
    wanted = _strip(search)
    count = len(search.splitlines())
    hits = []
    for i in range(len(lines) - count + 1):
        window = "".join(lines[i:i + count])
        if _strip(window) == wanted:
            start = sum(len(l) for l in lines[:i])
            hits.append((start, start + len(window)))
    if len(hits) == 1:
        return hits[0][0], hits[0][1], True
    if len(hits) > 1:
        raise EditError(f"the search block matches {len(hits)} places (ignoring indentation); include "
                        f"more of the surrounding lines")
    raise EditError("the search block was not found in the file. It must be copied exactly from the "
                    f"file as it is now. The nearest lines are:\n{_nearest(content, search)}")


def _nearest(content, search, around=3):
    first = next((l.strip() for l in search.splitlines() if l.strip()), "")
    lines = content.splitlines()
    if first:
        for i, line in enumerate(lines):
            if first in line:
                lo, hi = max(0, i - around), min(len(lines), i + around + 1)
                return "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
    # Nothing shares the first line: the closest single line by similarity, for a hint
    best = difflib.get_close_matches(first, [l.strip() for l in lines], n=1, cutoff=0.5)
    if best:
        i = [l.strip() for l in lines].index(best[0])
        lo, hi = max(0, i - around), min(len(lines), i + around + 1)
        return "\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi))
    return "(nothing similar - the code may be in another file; search for it)"


def _reindent(replace, matched):
    """Moves the replacement to the indentation the matched lines really have."""
    have = next((l for l in matched.splitlines() if l.strip()), "")
    gave = next((l for l in replace.splitlines() if l.strip()), "")
    want, got = _indent_of(have), _indent_of(gave)
    if want == got:
        return replace
    out = []
    for line in replace.splitlines(keepends=True):
        if line.strip() and line.startswith(got):
            line = want + line[len(got):]
        elif line.strip():
            line = want + line.lstrip()
        out.append(line)
    return "".join(out)


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(temporary, path)
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise


def _diff(relative, before, after):
    return "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                          fromfile=f"a/{relative}", tofile=f"b/{relative}", lineterm=""))


def apply_edit(root, relative, search, replace):
    path = inside(root, relative)
    if not path.is_file():
        raise EditError(f"{relative} does not exist; use create_file for a new file")
    if not search:
        raise EditError("the search block is empty; copy the lines to replace from the file")
    original = path.read_text(encoding="utf-8")
    start, end, fuzzy = _find(original, search)
    if fuzzy:
        replace = _reindent(replace, original[start:end])
        # The fuzzy match takes whole lines, newline included; keep the file's line ending
        if original[start:end].endswith("\n") and not replace.endswith("\n"):
            replace += "\n"
    updated = original[:start] + replace + original[end:]
    if updated == original:
        raise EditError("the edit changes nothing")
    _write(path, updated)
    return EditResult(relative, _diff(relative, original, updated))


def create_file(root, relative, content):
    path = inside(root, relative)
    if path.exists():
        raise EditError(f"{relative} already exists; change it with edit_file")
    _write(path, content if content.endswith("\n") else content + "\n")
    return EditResult(relative, _diff(relative, "", content))
