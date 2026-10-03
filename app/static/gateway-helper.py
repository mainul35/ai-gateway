#!/usr/bin/env python3
"""Lets the AI gateway work on a folder on this computer: read, edit, search, and run its checks.

    python gateway-helper.py --gateway https://ai-gateway.example.com --folder C:\\code\\myproject

The gateway does the thinking; this program is its hands on this machine. It connects out to the
gateway (nothing connects in), registers the folder as a project under your account, and then does
what a coding task asks of it, one small step at a time: list a directory, read a file, write one,
search, or run a command. Commands run here, on your computer, so each one is shown and asked about
first - unless you start it with --yes.

Only standard-library Python: nothing to install. The API key comes from the gateway's Dashboard
(Your API keys); give it with --key or in the GATEWAY_KEY environment variable.

Files keep their line endings: a file with Windows line endings is written back with them.
"""
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "dist", "build", "target", "out", ".next",
             "__pycache__", ".idea", ".vscode", ".gradle", "coverage", ".mypy_cache", ".pytest_cache",
             ".angular", ".cache", "obj", ".terraform"}
MAX_READ = 2_000_000
KEY_FILES = ("package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "pom.xml", "mvnw",
             "build.gradle", "build.gradle.kts", "gradlew", "go.mod", "Cargo.toml", "pyproject.toml",
             "setup.py", "requirements.txt", "requirements-dev.txt", "pytest.ini", "conftest.py",
             "tests/conftest.py", "tests", "test", "app")
READ_FILES = ("package.json", "pyproject.toml", "requirements.txt", "requirements-dev.txt")


class Refused(Exception):
    pass


class Folder:
    def __init__(self, root, ask=True):
        self.root = os.path.realpath(root)
        self.ask = ask
        self.endings = {}           # path -> "\r\n" for files that had Windows line endings

    def path(self, relative):
        relative = (relative or "").replace("\\", "/").strip("/")
        if relative.startswith("/") or ":" in relative or ".." in relative.split("/"):
            raise Refused(f"{relative} is outside the folder")
        if ".git" in relative.split("/"):
            raise Refused("the .git directory is not to be touched")
        full = os.path.realpath(os.path.join(self.root, relative))
        if full != self.root and not full.startswith(self.root + os.sep):
            raise Refused(f"{relative} is outside the folder")
        return full

    # --- operations ---------------------------------------------------------------------------

    def op_list(self, path=""):
        full = self.path(path)
        if not os.path.isdir(full):
            raise Refused(f"{path} is not a directory")
        return [n + ("/" if os.path.isdir(os.path.join(full, n)) else "")
                for n in sorted(os.listdir(full)) if n != ".git"]

    def _walk(self):
        for directory, subdirectories, names in os.walk(self.root):
            subdirectories[:] = sorted(d for d in subdirectories if d not in SKIP_DIRS)
            for name in sorted(names):
                yield os.path.relpath(os.path.join(directory, name), self.root).replace(os.sep, "/")

    def op_files(self):
        return list(self._walk())[:20000]

    def op_read(self, path):
        full = self.path(path)
        if not os.path.isfile(full):
            return None
        if os.path.getsize(full) > MAX_READ:
            raise Refused(f"{path} is larger than {MAX_READ // 1_000_000} MB")
        with open(full, "rb") as f:
            raw = f.read()
        if b"\x00" in raw[:8000]:
            raise Refused(f"{path} is not a text file")
        text = raw.decode("utf-8", errors="replace")
        if "\r\n" in text:
            self.endings[path] = "\r\n"
            text = text.replace("\r\n", "\n")
        return text

    def op_write(self, path, content):
        full = self.path(path)
        if path not in self.endings and os.path.isfile(full):
            self.op_read(path)                      # learn its line endings before replacing it
        if self.endings.get(path) == "\r\n":
            content = content.replace("\r\n", "\n").replace("\n", "\r\n")
        os.makedirs(os.path.dirname(full), exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(full), suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(content.encode("utf-8"))
        os.replace(temporary, full)
        print(f"  wrote {path}")
        return True

    def op_delete(self, path):
        full = self.path(path)
        if os.path.isfile(full):
            os.remove(full)
            print(f"  removed {path}")
        return True

    def op_search(self, text, regex=False, path=""):
        pattern = re.compile(text if regex else re.escape(text))
        found, base = [], path.strip("/")
        for relative in self._walk():
            if base and not (relative == base or relative.startswith(base + "/")):
                continue
            full = os.path.join(self.root, relative)
            try:
                if os.path.getsize(full) > MAX_READ:
                    continue
                with open(full, encoding="utf-8", errors="strict") as f:
                    for number, line in enumerate(f, 1):
                        if pattern.search(line):
                            found.append(f"{relative}:{number}:{line.rstrip()[:200]}")
                            if len(found) >= 500:
                                return found
            except (UnicodeDecodeError, OSError):
                continue
        return found

    def op_run(self, command):
        print(f"\nThe gateway wants to run, in {self.root}:\n    {command}")
        if self.ask:
            answer = input("Run it? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("  declined")
                return {"declined": True, "output": "The owner of this computer declined to run it."}
        try:
            done = subprocess.run(command, shell=True, cwd=self.root, capture_output=True, timeout=1500)
        except subprocess.TimeoutExpired:
            return {"code": None, "output": "stopped after 25 minutes without finishing"}
        output = (done.stdout + done.stderr).decode("utf-8", errors="replace")
        print(f"  exit code {done.returncode}")
        return {"code": done.returncode, "output": output[-12000:]}

    def snapshot(self):
        present = [n for n in KEY_FILES if os.path.exists(os.path.join(self.root, n))]
        contents = {}
        for name in READ_FILES:
            full = os.path.join(self.root, name)
            if os.path.isfile(full):
                with open(full, encoding="utf-8", errors="replace") as f:
                    contents[name] = f.read(200_000)
        has_python = any(r.endswith(".py") for r, _ in zip(self._walk(), range(5000)))
        return {"present": present, "contents": contents, "has_python": has_python}


class Gateway:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def request(self, method, path, body=None, timeout=60):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
            "User-Agent": "gateway-helper/1"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise SystemExit(f"The gateway said {e.code}: {detail}") if e.code in (401, 403) else RuntimeError(
                f"HTTP {e.code}: {detail}")


def serve(gateway, folder, project_id):
    print(f"Serving {folder.root} as project #{project_id}. Leave this running; Ctrl+C stops it.\n")
    while True:
        try:
            request = gateway.request("GET", f"/coding/remote/{project_id}/next?client=helper&can_run=1",
                                      timeout=40).get("request")
        except (RuntimeError, urllib.error.URLError, TimeoutError, OSError) as e:
            print(f"  could not reach the gateway ({e}); trying again in 5 s")
            time.sleep(5)
            continue
        if not request:
            continue
        handler = getattr(folder, "op_" + request["op"], None)
        try:
            if handler is None:
                raise Refused(f"unknown operation {request['op']}")
            answer = {"id": request["id"], "ok": True, "result": handler(**(request.get("args") or {}))}
        except (Refused, OSError, re.error, TypeError) as e:
            answer = {"id": request["id"], "ok": False, "error": str(e)}
        try:
            gateway.request("POST", f"/coding/remote/{project_id}/answer", answer)
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            print(f"  could not send the answer ({e})")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", required=True, help="the gateway's address, e.g. https://ai-gateway.example.com")
    parser.add_argument("--folder", required=True, help="the project folder on this computer")
    parser.add_argument("--name", help="the project's name in the gateway (default: the folder's name)")
    parser.add_argument("--key", default=os.environ.get("GATEWAY_KEY"), help="API key (or GATEWAY_KEY)")
    parser.add_argument("--yes", action="store_true", help="run commands without asking first")
    args = parser.parse_args()
    if not args.key:
        sys.exit("Give an API key with --key or GATEWAY_KEY (Dashboard > Your API keys).")
    if not os.path.isdir(args.folder):
        sys.exit(f"{args.folder} is not a folder")
    folder = Folder(args.folder, ask=not args.yes)
    gateway = Gateway(args.gateway, args.key)
    registration = {"name": args.name or os.path.basename(folder.root), "client": "helper",
                    "snapshot": folder.snapshot()}
    for attempt in range(1, 7):
        try:
            project = gateway.request("POST", "/coding/projects/local", registration)
            break
        except (RuntimeError, urllib.error.URLError, OSError) as e:
            if attempt == 6:
                sys.exit(f"Could not reach the gateway at {args.gateway}: {e}")
            print(f"Could not reach the gateway ({e}); trying again in 5 s")
            time.sleep(5)
    print(f"Connected as project \"{project['name']}\" ({project.get('detected', 'unknown kind')}); "
          f"checks: {project['verify_command'] or 'none set'}")
    try:
        serve(gateway, folder, project["id"])
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
