"""The tools a coding task's model works with, bound to one task's worktree.

Each stage of a task offers its own set, and ends only through its own exit tool: exploring ends with
submit_plan, editing with finish. Which stage comes next is decided by the runner, never by the
model - the rule local_coding_agent was built on, because a small model asked "what now?" wanders.

Everything here reads or writes inside the worktree only. Commands run in the sandbox, not here.
"""
import fnmatch
import json
import os

from app import knowledge
from app.coding import edits, remote, sandbox, workspace

MAX_READ_LINES = 300
MAX_HITS = 80


def _spec(name, description, properties=None, required=()):
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties or {}, "required": list(required)}}}


READING = [
    _spec("list_dir", "Lists a directory of the project: sub-directories end with /.",
          {"path": {"type": "string", "description": "Relative to the project root. Default: the root."}}),
    _spec("find_files", "Finds files by name or path pattern, e.g. *.py, src/**/Page*.java, Dockerfile.",
          {"pattern": {"type": "string"}}, ["pattern"]),
    _spec("search", "Searches the project's files for text and returns matching lines with file and line "
                    "number. Use it to find where something is defined or used.",
          {"text": {"type": "string", "description": "Exact text to find, e.g. a function name."},
           "path": {"type": "string", "description": "Optional: only under this directory or file."},
           "regex": {"type": "boolean", "description": "Treat text as a regular expression."}}, ["text"]),
    _spec("read_file", f"Reads lines of a file, numbered. Up to {MAX_READ_LINES} lines at a time.",
          {"path": {"type": "string"}, "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
          ["path"]),
    _spec("run_command", "Runs a shell command in a sandbox copy of the project (no network unless the "
                         "project allows it) and returns its output - e.g. to run the tests, a linter, "
                         "or a script that reproduces the problem.",
          {"command": {"type": "string"}}, ["command"]),
]
KNOWLEDGE = _spec("knowledge_search", "Searches the indexed code and documentation of this project and "
                                      "related ones by meaning - for when you do not know the name to search for.",
                  {"query": {"type": "string"}}, ["query"])
SUBMIT_PLAN = _spec("submit_plan", "Ends exploring. Give the plan: which files change, what changes in each, "
                                   "and how you will know it works.",
                    {"plan": {"type": "string"}}, ["plan"])
ANSWER = _spec("answer", "Ends the task without changing anything, for a task that only asks a question about "
                         "the project. Give the full answer, naming the files it comes from.",
               {"answer": {"type": "string"}}, ["answer"])
EDITING = [
    _spec("edit_file", "Changes a file by replacing one exact snippet. `search` must be copied from the file "
                       "as it is now (read it first) and must match in only one place; include a few "
                       "surrounding lines to make it unique. `replace` is what goes in its place.",
          {"path": {"type": "string"}, "search": {"type": "string"}, "replace": {"type": "string"}},
          ["path", "search", "replace"]),
    _spec("create_file", "Creates a new file with the given content.",
          {"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
]
FINISH = _spec("finish", "Ends editing once every change in the plan is made. The project's checks run "
                         "next; if they fail you will be shown why and asked to fix it.",
               {"summary": {"type": "string", "description": "What was changed and why, for the reviewer."}},
               ["summary"])


def explore_tools(with_knowledge):
    return READING + ([KNOWLEDGE] if with_knowledge else []) + [SUBMIT_PLAN, ANSWER]


def edit_tools(with_knowledge):
    return READING + ([KNOWLEDGE] if with_knowledge else []) + EDITING + [FINISH]


class Tools:
    """Runs tool calls against one task's worktree. call() returns (text, failed, outcome) where
    outcome is set for the calls the runner acts on: submit_plan, finish, and successful edits."""

    def __init__(self, project, worktree):
        self.project = project
        self.root = worktree

    async def call(self, name, args):
        handler = getattr(self, f"_{name}", None)
        if handler is None or name.startswith("_"):
            return f"There is no tool called {name}.", True, None
        try:
            return await handler(**{k: v for k, v in (args or {}).items() if v is not None})
        except edits.EditError as e:
            return str(e), True, None
        except TypeError as e:
            return f"Wrong arguments for {name}: {e}", True, None
        except (OSError, workspace.GitError, ValueError) as e:
            return f"{e.__class__.__name__}: {e}", True, None
        except remote.NotConnected:
            raise                               # the task cannot go on; the runner stops it
        except remote.ClientError as e:
            return f"The computer could not do it: {e}", True, None

    # --- reading -------------------------------------------------------------------------------

    async def _list_dir(self, path="."):
        target = edits.inside(self.root, path) if path not in ("", ".", "./") else os.path.realpath(self.root)
        if not os.path.isdir(target):
            return f"{path} is not a directory.", True, None
        names = sorted(os.listdir(target))
        rows = [n + ("/" if os.path.isdir(os.path.join(target, n)) else "") for n in names if n != ".git"]
        return "\n".join(rows[:300]) or "(empty)", False, None

    async def _files(self):
        listed = await workspace.git(["ls-files", "--cached", "--others", "--exclude-standard"], cwd=self.root)
        return listed.splitlines()

    async def _find_files(self, pattern):
        files = await self._files()
        hits = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(os.path.basename(f), pattern)
                or fnmatch.fnmatch(f, f"*{pattern}*")]
        if not hits:
            return f"No file matches {pattern}.", False, None
        more = f"\n(... and {len(hits) - 200} more)" if len(hits) > 200 else ""
        return "\n".join(hits[:200]) + more, False, None

    async def _search(self, text, path=None, regex=False):
        args = ["grep", "--untracked", "-n", "-I", "--full-name", "-E" if regex else "-F", "-e", text]
        if path:
            edits.inside(self.root, path)
            args += ["--", path]
        try:
            found = await workspace.git(args, cwd=self.root)
        except workspace.GitError as e:
            if "failed:" in str(e) and str(e).rstrip().endswith("failed:"):
                return f"Nothing matches {text!r}.", False, None     # git grep exits 1 for no match
            raise
        lines = [line[:240] for line in found.splitlines()]
        if not lines:
            return f"Nothing matches {text!r}.", False, None
        more = f"\n(... {len(lines) - MAX_HITS} more; narrow it with path)" if len(lines) > MAX_HITS else ""
        return "\n".join(lines[:MAX_HITS]) + more, False, None

    async def _read_file(self, path, start_line=1, end_line=None):
        target = edits.inside(self.root, path)
        if not target.is_file():
            return f"{path} does not exist.", True, None
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        tail = f"\n(lines {start}-{end} of {len(lines)})"
        return (body or "(empty file)") + tail, False, None

    async def _knowledge_search(self, query):
        repo = self.project.name.split("/")[-1]
        hits = await knowledge.search(query, source=f"*{repo}", limit=6)
        if not hits:
            return "Nothing indexed matches. Use search and find_files on the files directly.", False, None
        return "\n\n".join(f"{h['path']}:{h['start_line']}-{h['end_line']}\n{h['content']}" for h in hits), False, None

    async def _run_command(self, command):
        code, output = await sandbox.run(self.root, self.project.sandbox_image, command,
                                         network=self.project.sandbox_network, timeout=600)
        status = "timed out" if code is None else f"exit code {code}"
        return f"[{status}]\n{output}", False, None

    # --- writing -------------------------------------------------------------------------------

    async def _edit_file(self, path, search, replace):
        result = edits.apply_edit(self.root, path, search, replace)
        return f"Changed {path}:\n{result.diff[-3000:]}", False, {"edited": path}

    async def _create_file(self, path, content):
        edits.create_file(self.root, path, content)
        return f"Created {path}.", False, {"edited": path}

    # --- stage exits ---------------------------------------------------------------------------

    async def _submit_plan(self, plan):
        return "Plan received.", False, {"plan": str(plan)}

    async def _finish(self, summary):
        return "Finished editing.", False, {"finish": str(summary)}

    async def _answer(self, answer):
        return "Answer received.", False, {"answer": str(answer)}


class RemoteTools(Tools):
    """The same tools for a folder on somebody's computer, done through app/coding/remote.py.

    Edits are worked out here, from the text the computer sends, and only the result goes back to be
    written. The text of every file before the task first changed it is kept in `originals` (None for
    a file the task created): the diff is measured against it, and Undo writes it back."""

    def __init__(self, project, can_run, originals=None, current=None):
        super().__init__(project, None)
        self.can_run = can_run
        self.originals = dict(originals or {})
        self.current = dict(current or {})       # what this task wrote, kept so a continued task diffs right
        self._listing = None

    async def _list_dir(self, path="."):
        path = "" if path in ("", ".", "./") else edits.check_relative(path)
        names = await remote.call(self.project.id, "list", path=path)
        return "\n".join(names[:300]) or "(empty)", False, None

    async def _files(self):
        if self._listing is None:
            self._listing = await remote.call(self.project.id, "files")
        return self._listing

    async def _search(self, text, path=None, regex=False):
        lines = await remote.call(self.project.id, "search", text=text, regex=bool(regex),
                                  path=edits.check_relative(path) if path else "")
        if not lines:
            return f"Nothing matches {text!r}.", False, None
        more = f"\n(... {len(lines) - MAX_HITS} more; narrow it with path)" if len(lines) > MAX_HITS else ""
        return "\n".join(line[:240] for line in lines[:MAX_HITS]) + more, False, None

    async def _text(self, path):
        path = edits.check_relative(path)
        if path in self.current:
            return path, self.current[path]
        return path, await remote.call(self.project.id, "read", path=path)

    async def _read_file(self, path, start_line=1, end_line=None):
        path, content = await self._text(path)
        if content is None:
            return f"{path} does not exist.", True, None
        lines = content.splitlines()
        start = max(1, int(start_line or 1))
        end = min(len(lines), int(end_line or start + MAX_READ_LINES - 1), start + MAX_READ_LINES - 1)
        body = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
        return (body or "(empty file)") + f"\n(lines {start}-{end} of {len(lines)})", False, None

    async def _run_command(self, command):
        if not self.can_run:
            return ("Commands cannot run here: this folder is connected through a browser, which can "
                    "read and write files but not run programs. Check the change by reading it instead."), True, None
        result = await remote.call(self.project.id, "run", command=command)
        code = result.get("code")
        status = "not run: the owner declined it" if result.get("declined") else (
            "timed out" if code is None else f"exit code {code}")
        return f"[{status}]\n{result.get('output', '')}", False, None

    async def _write(self, path, before, after):
        if path not in self.originals:
            self.originals[path] = before
        await remote.call(self.project.id, "write", path=path, content=after)
        self.current[path] = after
        if self._listing is not None and before is None:
            self._listing.append(path)

    async def _edit_file(self, path, search, replace):
        path, original = await self._text(path)
        if original is None:
            return f"{path} does not exist; use create_file for a new file", True, None
        updated, diff = edits.apply_text(original, path, search, replace)
        await self._write(path, original, updated)
        return f"Changed {path}:\n{diff[-3000:]}", False, {"edited": path}

    async def _create_file(self, path, content):
        path, existing = await self._text(path)
        if existing is not None:
            return f"{path} already exists; change it with edit_file", True, None
        content = content if content.endswith("\n") else content + "\n"
        await self._write(path, None, content)
        return f"Created {path}.", False, {"edited": path}

    def diff(self):
        return "\n".join(edits.diff_of(path, before, self.current.get(path, before))
                         for path, before in sorted(self.originals.items()) if self.current.get(path) != before)


def remote_tools_for(stage, can_run):
    """A local project's tools: no knowledge base (it is not indexed), and run_command only when a
    helper, not a browser, is connected."""
    base = [t for t in READING if can_run or t["function"]["name"] != "run_command"]
    return base + ([SUBMIT_PLAN, ANSWER] if stage == "explore" else EDITING + [FINISH])


def arguments(raw):
    if isinstance(raw, dict):
        return raw
    try:
        loaded = json.loads(raw or "{}")
        return loaded if isinstance(loaded, dict) else {}
    except (ValueError, TypeError):
        return None
