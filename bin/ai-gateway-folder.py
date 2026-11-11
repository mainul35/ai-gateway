#!/usr/bin/env python3
"""Connects the folder it is run in to one conversation in the AI gateway's playground, so the agent
there can read it - nothing else. The playground gives the command to run, with a one-time link:

    irm https://ai-gateway.example.com/downloads/ai-gateway-folder.py | python - LINK     (PowerShell)
    curl -fsSL https://ai-gateway.example.com/downloads/ai-gateway-folder.py | python3 - LINK

Run it in the project folder you want the agent to read (or pass --folder). It only reads files: it
lists folders, searches files for text and reads lines of a file, inside this folder and never above
it. It leaves out dependency, build and hidden folders and files that hold credentials, the same
lists the gateway uses for its own index, and the gateway checks every line again before the model
sees it. Every request is printed here as it comes. Nothing connects in to this computer: it asks
the gateway for work. Ctrl+C ends the connection.

Only standard-library Python: nothing to install, no API key (the link is good once, for one
conversation, for a few minutes).
"""
import argparse
import fnmatch
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

GATEWAY = "https://ai-gateway.mainul35.dev"      # the gateway puts its own address here when served
AGENT = "ai-gateway-folder/1"
SEARCH_FILE_BYTES = 2_000_000                     # a bigger file is data, not code
SEARCH_FILES = 20_000
NEXT_TIMEOUT = 45                                 # the gateway answers a poll within 25 s


def say(*words):
    print(time.strftime("%H:%M:%S"), *words, flush=True)


class Gone(Exception):
    """The gateway has ended this connection: say why and stop."""


class Gateway:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.secret = ""

    def call(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("User-Agent", AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if self.secret:
            request.add_header("X-Device-Secret", self.secret)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode("utf-8", "replace")
                return response.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                detail = json.loads(text).get("detail")
            except ValueError:
                detail = None
            if e.code in (401, 404, 410):
                raise Gone(detail or f"HTTP {e.code}")
            raise OSError(detail or f"HTTP {e.code}")


class Folder:
    def __init__(self, root, skip, limits):
        self.root = os.path.realpath(root)
        self.dirs = set(skip.get("dirs") or ())
        self.names = {n.lower() for n in skip.get("names") or ()}
        self.secret = [p.lower() for p in skip.get("secret") or ()]
        self.limits = limits

    # what is left out
    def skip_dir(self, name):
        return name in self.dirs or name.startswith(".")

    def skip_file(self, name):
        lowered = name.lower()
        return lowered in self.names or any(fnmatch.fnmatch(lowered, p) for p in self.secret)

    def where(self, relative):
        """The real path of something inside the folder, or ValueError for anything outside it."""
        relative = str(relative or "").replace("\\", "/")
        parts = [p for p in relative.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts) or relative.startswith("/") or ":" in relative:
            raise ValueError("that path is outside the connected folder")
        path = os.path.realpath(os.path.join(self.root, *parts))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise ValueError("that path leads outside the connected folder")
        for part in parts[:-1]:
            if self.skip_dir(part):
                raise ValueError(f"{part} is one of the folders left out")
        if parts and os.path.isfile(path) and self.skip_file(parts[-1]):
            raise ValueError(f"{parts[-1]} is the kind of file that holds credentials, so it is not read")
        return path

    def relative(self, path):
        return os.path.relpath(path, self.root).replace(os.sep, "/")

    def walk(self, start):
        for directory, subdirectories, names in os.walk(start):
            subdirectories[:] = sorted(d for d in subdirectories if not self.skip_dir(d)
                                       and not os.path.islink(os.path.join(directory, d)))
            yield directory, subdirectories, sorted(n for n in names if not self.skip_file(n))

    def files(self, start):
        """Every file to look in under `start` (or `start` itself, when it is a file)."""
        if os.path.isfile(start):
            yield start
            return
        for directory, _, names in self.walk(start):
            for name in names:
                yield os.path.join(directory, name)

    # the three things it does
    def list(self, args):
        start = self.where(args.get("path"))
        if not os.path.isdir(start):
            raise ValueError("that is not a folder")
        depth = max(1, min(4, int(args.get("depth") or 2)))
        pattern = (args.get("pattern") or "").lower()
        limit = int(self.limits.get("list_entries") or 600)
        entries, truncated = [], False
        base = start.count(os.sep)
        for directory, subdirectories, names in self.walk(start):
            level = directory.count(os.sep) - base
            if level >= depth:
                subdirectories[:] = []
                continue
            for name in subdirectories:
                if not pattern or fnmatch.fnmatch(name.lower(), pattern):
                    entries.append({"path": self.relative(os.path.join(directory, name)), "type": "dir"})
            for name in names:
                if pattern and not fnmatch.fnmatch(name.lower(), pattern):
                    continue
                path = os.path.join(directory, name)
                try:
                    size = os.path.getsize(path)
                except OSError:
                    size = None
                entries.append({"path": self.relative(path), "type": "file", "size": size})
            if len(entries) >= limit:
                truncated = True
                break
        return {"entries": entries[:limit], "truncated": truncated}

    def search(self, args):
        start = self.where(args.get("path"))
        text = str(args.get("text") or "")
        try:
            matcher = re.compile(text if args.get("regex") else re.escape(text), re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"that regular expression does not compile: {e}")
        glob = (args.get("glob") or "").lower()
        limit = int(self.limits.get("search_hits") or 200)
        width = int(self.limits.get("line_chars") or 300)
        hits = []
        for files, path in enumerate(self.files(start), 1):
            if files > SEARCH_FILES:
                return {"hits": hits, "truncated": True}
            if glob and not fnmatch.fnmatch(os.path.basename(path).lower(), glob):
                continue
            content = _text_of(path, SEARCH_FILE_BYTES)
            if content is None:
                continue
            for number, line in enumerate(content.splitlines(), 1):
                if matcher.search(line):
                    hits.append({"path": self.relative(path), "line": number, "text": line.strip()[:width]})
                    if len(hits) >= limit:
                        return {"hits": hits, "truncated": True}
        return {"hits": hits, "truncated": False}

    def read(self, args):
        path = self.where(args.get("path"))
        if not os.path.isfile(path):
            raise ValueError("there is no such file" if not os.path.exists(path) else "that is a folder, not a file")
        content = _text_of(path, None)
        if content is None:
            raise ValueError("that file is not text")
        lines = content.splitlines()
        start = max(1, int(args.get("start_line") or 1))
        end = max(start, int(args.get("end_line") or start + 399))
        text = "\n".join(lines[start - 1:end])[: int(self.limits.get("read_chars") or 40000)]
        return {"text": text, "total_lines": len(lines)}


def _text_of(path, limit):
    """A file's text, or None for a binary one (or one bigger than `limit`)."""
    try:
        if limit is not None and os.path.getsize(path) > limit:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    return raw.decode("utf-8", "replace")


def described(op, args):
    if op == "list":
        return f"list   {args.get('path') or '.'}" + (f"  ({args['pattern']})" if args.get("pattern") else "")
    if op == "search":
        return f"search \"{args.get('text')}\"" + (f" in {args['path']}" if args.get("path") else "")
    if op == "read":
        return f"read   {args.get('path')}  lines {args.get('start_line')}-{args.get('end_line')}"
    return op


def serve(gateway, folder, stopped):
    trouble = 0
    while not stopped.is_set():
        try:
            code, job = gateway.call("GET", "/chat/device/next", timeout=NEXT_TIMEOUT)
            if trouble:
                say("reached the gateway again")
            trouble = 0
        except Gone as e:
            say(f"The gateway ended the connection: {e}")
            stopped.set()
            return
        except (OSError, ValueError) as e:
            trouble += 1
            if trouble == 1:
                say(f"cannot reach the gateway ({e}); trying again")
            time.sleep(min(30, 2 * trouble))
            continue
        if code == 204 or not job:
            continue
        op, args = job.get("op"), job.get("args") or {}
        say(described(op, args))
        try:
            handler = {"list": folder.list, "search": folder.search, "read": folder.read}.get(op)
            if handler is None:
                raise ValueError(f"this program does not do {op}")
            body = {"id": job["id"], "result": handler(args)}
        except (ValueError, OSError) as e:
            say(f"  not done: {e}")
            body = {"id": job["id"], "error": str(e)}
        try:
            gateway.call("POST", "/chat/device/answer", body)
        except Gone as e:
            say(f"The gateway ended the connection: {e}")
            stopped.set()
            return
        except OSError as e:
            say(f"  the answer did not get through: {e}")


def main():
    parser = argparse.ArgumentParser(description="Connect this folder to a conversation in the AI gateway, read-only.")
    parser.add_argument("link", help="the one-time link from the command the playground showed")
    parser.add_argument("--folder", default=os.getcwd(), help="the folder to connect (default: this one)")
    parser.add_argument("--gateway", default=GATEWAY, help=f"the gateway's address (default {GATEWAY})")
    options = parser.parse_args()
    root = os.path.realpath(options.folder)
    if not os.path.isdir(root):
        sys.exit(f"{root} is not a folder")

    gateway = Gateway(options.gateway)
    try:
        _, connected = gateway.call("POST", "/chat/device/connect",
                                    {"link": options.link, "folder": os.path.basename(root) or root})
    except Gone as e:
        sys.exit(f"Not connected: {e}")
    except OSError as e:
        sys.exit(f"Cannot reach {options.gateway}: {e}")
    gateway.secret = connected["secret"]
    folder = Folder(root, connected.get("skip") or {}, connected.get("limits") or {})
    say(f"Connected {root} to the conversation, read-only.")
    say("The agent can list, search and read files in this folder. Each request is shown below. Ctrl+C to stop.")

    stopped = threading.Event()
    worker = threading.Thread(target=serve, args=(gateway, folder, stopped), daemon=True)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(0.5)
    except KeyboardInterrupt:
        stopped.set()
        try:
            gateway.call("POST", "/chat/device/bye", {}, timeout=5)
        except (Gone, OSError):
            pass
        say("Disconnected.")


if __name__ == "__main__":
    main()
