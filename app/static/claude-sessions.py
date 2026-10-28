#!/usr/bin/env python3
"""Shows this computer's Claude Code sessions in the AI gateway's playground (Code mode), and lets them
be talked to from there.

    ai-gateway sessions                     # started by the launcher, which gives it the key
    python claude-sessions.py --gateway https://ai-gateway.example.com --key sk-...

`ai-gateway launch claude` starts it in the background (--background) when it is not already running;
one runs per computer, and one started in the foreground takes over from it.

It reads the sessions Claude Code keeps for the launcher (~/.claude-gateway/projects, or --config-dir)
and reports them to the gateway every half minute: title, folder, branch, model and latest messages.
Nothing connects in to this computer: it asks the gateway for work. A message sent to a session from
the playground is run here, in the session's folder, as

    claude --resume <session> -p --output-format stream-json

and what Claude Code does is sent back as it happens. File edits are allowed; commands only when this
program was started with --allow-commands and the playground asks for them. Stop it with Ctrl+C and the
sessions stop being reachable (they stay listed, greyed, for a while).

Only standard-library Python: nothing to install. The key is the gateway's own API key (--key, or
GATEWAY_KEY / AI_GATEWAY_API_KEY).
"""
import argparse
import glob
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPORT_SECONDS = 30
KEPT_MESSAGES = 40          # of each session, sent when it changes
TEXT_LIMIT = 4000           # of one message
TAIL_BYTES = 1_500_000      # of a session's file read for its latest messages
RUNNING_SECONDS = 60        # a session written to this recently is working
# The one agent running on a computer listens locally on the port it writes here, and greets with this
PORT_FILE = os.path.join(os.path.expanduser("~"), ".ai-gateway", "sessions-agent.port")
GREETING = b"ai-gateway sessions agent\n"
# What the launcher gives Claude Code (bin/ai-gateway.sh): the same for a session run from here
CLAUDE_ENV = {"CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1", "CLAUDE_CODE_DISABLE_THINKING": "1",
              "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
              "CLAUDE_CODE_AUTO_MODE_SERVER": "0", "DISABLE_ERROR_REPORTING": "1", "DISABLE_FEEDBACK_COMMAND": "1",
              "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}


def log(*words):
    print(time.strftime("%H:%M:%S"), *words, flush=True)


class Gateway:
    def __init__(self, url, key):
        self.url, self.key = url.rstrip("/"), key

    def call(self, method, path, body=None, timeout=60):
        """(status, json or None). Raises nothing for HTTP errors: their status says it."""
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url + path, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.key}")
        # Cloudflare turns Python's own User-Agent (Python-urllib) away with a 403 before the gateway sees it
        request.add_header("User-Agent", "ai-gateway-sessions-agent/1")
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"null")
            except ValueError:
                return e.code, None

    def awake(self):
        """Whether the gateway's machine is up. Asked of the wake front first, which answers for itself:
        the agent's own requests never wake a machine that is asleep. Without a wake front, it is up."""
        try:
            status, answer = self.call("GET", "/__wake/status", timeout=10)
        except (OSError, ValueError):
            return False
        return status != 200 or bool((answer or {}).get("up"))


# --- reading the sessions -----------------------------------------------------------------------

def _text(content):
    """A message's words: its text blocks; a tool use or result as one short line."""
    if isinstance(content, str):
        return content.strip(), "user"
    words, kind = [], None
    for block in content or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and block.get("text"):
            words.append(block["text"])
        elif block.get("type") == "tool_use":
            target = block.get("input") or {}
            hint = target.get("file_path") or target.get("command") or target.get("pattern") or target.get("path") or ""
            words.append(f"[{block.get('name')}{': ' + str(hint)[:120] if hint else ''}]")
            kind = kind or "tool"
        elif block.get("type") == "tool_result":
            kind = kind or "tool"
    return "\n".join(words).strip(), kind


def _entries(path, tail=False):
    with open(path, "rb") as f:
        if tail:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            f.readline() if f.tell() else None          # a partial first line
        for raw in f:
            try:
                yield json.loads(raw)
            except ValueError:
                continue


def read_session(path):
    """What the gateway is told of one session file, and its latest messages."""
    sid = os.path.splitext(os.path.basename(path))[0]
    title, cwd, branch, model, said = None, None, None, None, []
    for entry in _entries(path):                         # the first question names it
        if entry.get("type") == "summary" and entry.get("summary"):
            title = entry["summary"]
        if entry.get("type") == "user" and not title:
            text, kind = _text((entry.get("message") or {}).get("content"))
            if text and kind != "tool" and not text.startswith("<"):
                title = text.splitlines()[0][:120]
        if title:
            break
    for entry in _entries(path, tail=True):
        cwd = entry.get("cwd") or cwd
        branch = entry.get("gitBranch") or branch
        if entry.get("type") not in ("user", "assistant"):
            continue
        message = entry.get("message") or {}
        if entry["type"] == "assistant" and message.get("model") and not message["model"].startswith("<"):
            model = message["model"]                     # "<synthetic>": Claude Code's own notices
        text, kind = _text(message.get("content"))
        # Empty, or put in by Claude Code itself (<command-...>, <task-notification>...): not said by anyone
        if not text or (entry["type"] == "user" and text.startswith("<")):
            continue
        role = "tool" if kind == "tool" and entry["type"] == "assistant" else entry["type"]
        if role == "user" and kind == "tool":
            continue                                     # a tool's output: too much to keep
        said.append({"role": role, "text": text[:TEXT_LIMIT], "at": entry.get("timestamp")})
    modified = os.path.getmtime(path)
    return {"session_id": sid, "title": (title or os.path.basename(cwd or "") or "Untitled")[:200],
            "workspace_path": cwd, "branch_name": branch, "model": model,
            "status": "running" if time.time() - modified < RUNNING_SECONDS else "idle",
            "last_active": datetime.fromtimestamp(modified, timezone.utc).isoformat(),
            "transcript": said[-KEPT_MESSAGES:]}, modified


class Sessions:
    def __init__(self, config_dir, days):
        self.config_dir, self.days = config_dir, days
        self.sent = {}          # session -> the file time last reported with its messages
        self.where = {}         # session -> its folder, for running a message there

    def report(self, gateway, name, allow_commands):
        since = time.time() - self.days * 86400
        files = [f for f in glob.glob(os.path.join(self.config_dir, "projects", "*", "*.jsonl"))
                 if os.path.getmtime(f) >= since]
        files.sort(key=os.path.getmtime, reverse=True)
        sessions, stamps = [], {}
        for path in files[:150]:
            try:
                info, modified = read_session(path)
            except OSError:
                continue
            if not info["workspace_path"]:
                continue                                 # opened and closed with nothing said
            self.where[info["session_id"]] = info["workspace_path"]
            if self.sent.get(info["session_id"]) == modified:
                info.pop("transcript")                   # unchanged: the gateway has it
            stamps[info["session_id"]] = modified
            sessions.append(info)
        status, answer = gateway.call("POST", "/chat/sessions/register",
                                      {"pc_name": name, "sessions": sessions, "allow_commands": allow_commands})
        if status == 200:
            self.sent.update(stamps)
        else:
            self.sent.clear()                            # everything again next time
            log("the gateway did not take the report:", status, answer)
        return status


# --- running a message --------------------------------------------------------------------------

def _short(value, limit=300):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…"


def events_of(line):
    """Claude Code's stream-json, as the playground shows it: what it says, what it uses, what came back."""
    try:
        event = json.loads(line)
    except ValueError:
        return []
    kind = event.get("type")
    out = []
    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                out.append({"kind": "text", "text": block["text"]})
            elif block.get("type") == "tool_use":
                out.append({"kind": "tool", "name": block.get("name"), "input": _short(block.get("input"))})
    elif kind == "user":
        for block in (event.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                text = content if isinstance(content, str) else " ".join(
                    c.get("text", "") for c in content or [] if isinstance(c, dict))
                out.append({"kind": "tool_result", "text": _short(text, 600), "error": bool(block.get("is_error"))})
    elif kind == "result":
        out.append({"kind": "result", "text": event.get("result") or "", "error": bool(event.get("is_error")),
                    "seconds": round((event.get("duration_ms") or 0) / 1000, 1), "turns": event.get("num_turns")})
    return out


def run(gateway, job, sessions, config_dir, allow_commands):
    """One message, run in its session; every event sent back as it comes, in batches."""
    job_id = job["job_id"]
    claude = shutil.which("claude")
    folder = job.get("workspace_path") or sessions.where.get(job["session_id"])
    if not claude:
        gateway.call("POST", "/chat/sessions/agent/events", {"job_id": job_id, "done": True,
                     "error": "Claude Code (claude) is not on this computer's PATH"})
        return
    if not folder or not os.path.isdir(folder):
        gateway.call("POST", "/chat/sessions/agent/events", {"job_id": job_id, "done": True,
                     "error": f"The session's folder is not on this computer: {folder}"})
        return
    commands = job.get("permission") == "commands" and allow_commands
    mode = "bypassPermissions" if commands else "acceptEdits"
    env = {**os.environ, **CLAUDE_ENV, "CLAUDE_CONFIG_DIR": config_dir,
           "ANTHROPIC_BASE_URL": gateway.url, "ANTHROPIC_AUTH_TOKEN": gateway.key}
    env.pop("ANTHROPIC_API_KEY", None)
    if job.get("model"):
        for name in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                     "ANTHROPIC_DEFAULT_HAIKU_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL"):
            env[name] = job["model"]
    log(f"running a message in session {job['session_id'][:8]} ({folder}), {mode}")
    process = subprocess.Popen([claude, "--resume", job["session_id"], "-p", "--output-format", "stream-json",
                                "--verbose", "--permission-mode", mode],
                               cwd=folder, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    process.stdin.write(job["text"])
    process.stdin.close()
    pending, last_sent, lock = [], time.monotonic(), threading.Lock()

    def flush(done=False, error=None):
        nonlocal pending, last_sent
        with lock:
            batch, pending = pending, []
        if batch or done:
            status, _ = gateway.call("POST", "/chat/sessions/agent/events",
                                     {"job_id": job_id, "events": batch, "done": done, "error": error})
            if status == 404 and not done:
                process.terminate()                     # nobody is waiting for it any more
        last_sent = time.monotonic()

    for line in process.stdout:
        with lock:
            pending.extend(events_of(line))
        if time.monotonic() - last_sent > 0.5:
            flush()
    process.wait()
    problem = None
    if process.returncode:
        problem = (process.stderr.read() or "").strip()[-1500:] or f"claude ended with code {process.returncode}"
    flush(done=True, error=problem)
    log(f"done with session {job['session_id'][:8]}" + (f": {problem}" if problem else ""))


# --- the two loops ------------------------------------------------------------------------------

def reporting(gateway, sessions, name, allow_commands, stop):
    while not stop.is_set():
        try:
            if gateway.awake():
                status = sessions.report(gateway, name, allow_commands)
                if status in (401, 403):
                    log("the gateway refused the key; stopping")
                    stop.set()
        except (OSError, ValueError) as e:
            log("could not report:", e)
        stop.wait(REPORT_SECONDS)


def _other_agent():
    """A connection to the agent already running on this computer, or None. It is known by the port it
    wrote down and by its greeting there: a stale file, or another program on that port, is not it."""
    try:
        with open(PORT_FILE, encoding="utf-8") as f:
            port = int(f.read().strip())
    except (OSError, ValueError):
        return None
    try:
        other = socket.create_connection(("127.0.0.1", port), timeout=2)
    except OSError:
        return None
    try:
        other.settimeout(2)
        if other.recv(len(GREETING)) == GREETING:
            return other
    except OSError:
        pass
    other.close()
    return None


def _only_one(take_over):
    """A local listener that makes this the computer's one agent: None when another runs and this one
    is not to take over. Taking over asks the other one to stop, and waits until it has."""
    for _ in range(50):
        other = _other_agent()
        if other is None:
            break
        with other:
            if not take_over:
                return None
            other.sendall(b"stop\n")
        time.sleep(0.2)
    else:
        return None
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))                 # any free port: fixed ones can be reserved
    listener.listen(4)
    os.makedirs(os.path.dirname(PORT_FILE), exist_ok=True)
    with open(PORT_FILE, "w", encoding="utf-8") as f:
        f.write(str(listener.getsockname()[1]))
    return listener


def _stop_when_asked(listener):
    """Another agent started here in the foreground (`ai-gateway sessions`) asks this one to make way."""
    while True:
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(2)
            try:
                connection.sendall(GREETING)
                asked = connection.recv(16).startswith(b"stop")
            except OSError:
                asked = False
        if asked:
            log("another sessions agent started on this computer; this one stops")
            listener.close()
            os._exit(0)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gateway", required=True, help="the gateway's address")
    parser.add_argument("--key", default=os.environ.get("GATEWAY_KEY") or os.environ.get("AI_GATEWAY_API_KEY"),
                        help="the gateway API key (or GATEWAY_KEY)")
    parser.add_argument("--name", default=socket.gethostname(), help="this computer's name in the playground")
    parser.add_argument("--config-dir", default=os.path.join(os.path.expanduser("~"), ".claude-gateway"),
                        help="where Claude Code keeps the launcher's sessions")
    parser.add_argument("--days", type=int, default=14, help="sessions used within this many days are listed")
    parser.add_argument("--allow-commands", action="store_true",
                        help="let a message from the playground run commands too, not only edit files")
    parser.add_argument("--background", action="store_true",
                        help="started by `ai-gateway launch`: nothing to do when an agent already runs here")
    args = parser.parse_args()
    if not args.key:
        sys.exit("No API key: give --key, or set GATEWAY_KEY")
    listener = _only_one(take_over=not args.background)
    if listener is None:
        if args.background:
            return
        sys.exit("Another sessions agent is running on this computer and did not stop")
    threading.Thread(target=_stop_when_asked, args=(listener,), daemon=True).start()
    gateway, sessions, stop = Gateway(args.gateway, args.key), Sessions(args.config_dir, args.days), threading.Event()
    log(f"reporting {args.config_dir} to {gateway.url} as {args.name}"
        + (" (commands allowed)" if args.allow_commands else " (file edits only)"))
    threading.Thread(target=reporting, args=(gateway, sessions, args.name, args.allow_commands, stop), daemon=True).start()
    try:
        while not stop.is_set():
            try:
                if not gateway.awake():
                    stop.wait(30)
                    continue
                status, job = gateway.call("GET", f"/chat/sessions/agent/next?pc={urllib.request.quote(args.name)}",
                                           timeout=60)
            except (OSError, ValueError):
                stop.wait(10)
                continue
            if status == 200 and job:
                threading.Thread(target=run, args=(gateway, job, sessions, args.config_dir, args.allow_commands),
                                 daemon=True).start()
            elif status in (401, 403):
                sys.exit("The gateway refused the key")
            elif status != 204:
                stop.wait(10)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
