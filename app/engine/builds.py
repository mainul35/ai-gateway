"""The llama-server builds this machine has, and which model architectures each can load.

A GGUF names its architecture (general.architecture: qwen35moe, qwen4exp, nemotron_h_moe...), and a
llama.cpp build loads only the architectures it was written for. The builds here differ: Ollama's
llama-server runs qwen3.6 but not Qwen3.8-Flash-Next (qwen4exp), which llama-b11025 runs (2026-10-20).

Each build is asked, not guessed from release notes: it is given a GGUF that holds nothing but the
architecture's name, and refuses it either with "unknown model architecture" or for want of the
model's other keys - which means it got past the check. Half a second a question; the answers are
kept in state/arch-support.json per build, until that build's file changes.
"""
import json
import logging
import os
import struct
import subprocess
import tempfile
import threading

from app import settings
from app.engine.profiles import load_profiles
from app.settings import PROJECT_ROOT

log = logging.getLogger("engine")

CACHE_FILE = os.path.join(PROJECT_ROOT, "state", "arch-support.json")
_lock = threading.Lock()


def builds():
    """[{"name", "binary", "env", "default"}]: the configured llama-server, then the others the
    profiles name (Ollama's), each once."""
    found, seen = [], set()
    default = settings.get("engine.llama_server.path", "LLAMA_SERVER_PATH") or ""
    candidates = [(default, {})] + [(p.binary, p.env or {}) for p in load_profiles().values() if p.binary]
    for binary, env in candidates:
        if not binary or binary in seen or not os.path.isfile(binary):
            continue
        seen.add(binary)
        folder = os.path.basename(os.path.dirname(binary))
        name = "Ollama's llama-server" if "/ollama/" in binary else f"llama.cpp {folder.replace('llama-', '')}"
        found.append({"name": name, "binary": binary, "env": env, "default": binary == default})
    return found


def _gguf_naming_only(architecture):
    def string(text):
        data = text.encode()
        return struct.pack("<Q", len(data)) + data
    # GGUF v3: magic, version, no tensors, one key: general.architecture (value type 8, a string)
    return (b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + string("general.architecture")
            + struct.pack("<I", 8) + string(architecture))


def _ask(build, architecture):
    with tempfile.NamedTemporaryFile(suffix=".gguf", delete=False) as f:
        f.write(_gguf_naming_only(architecture))
    try:
        out = subprocess.run([build["binary"], "-m", f.name, "--host", "127.0.0.1", "--port", "0", "-ngl", "0"],
                             capture_output=True, text=True, timeout=60, env={**os.environ, **build["env"]})
        return "unknown model architecture" not in (out.stdout + out.stderr)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("could not ask %s about %s: %s", build["binary"], architecture, e)
        return None
    finally:
        os.unlink(f.name)


def supports(architecture):
    """[{"name", "binary", "default", "supported"}] for every build; supported is None when the build
    could not be asked. Blocking (runs the builds): call it from a thread."""
    if not architecture:
        return []
    with _lock:
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        answers, changed = [], False
        for build in builds():
            stamp = f"{build['binary']}@{int(os.path.getmtime(build['binary']))}"
            known = cache.setdefault(stamp, {})
            if architecture not in known:
                known[architecture] = _ask(build, architecture)
                changed = True
            answers.append({k: build[k] for k in ("name", "binary", "default")} | {"supported": known[architecture]})
        if changed:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE + ".new", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(CACHE_FILE + ".new", CACHE_FILE)
        return answers
