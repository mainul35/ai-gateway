"""The llama-server builds this machine has, and which model architectures each can load.

A GGUF names its architecture (general.architecture: qwen35moe, qwen4exp, nemotron_h_moe...), and a
llama.cpp build loads only the architectures it was written for. The builds here differ: Ollama's
llama-server runs qwen3.6 but not Qwen3.8-Flash-Next (qwen4exp), which llama-b11025 runs (2026-10-20).

Each build is asked, not guessed from release notes: it is given a GGUF that holds nothing but the
architecture's name, and refuses it either with "unknown model architecture" or for want of the
model's other keys - which means it got past the check. Half a second a question; the answers are
kept in state/arch-support.json per build, until that build's file changes.

Knowing the name is not loading the model, though. Nemotron 3.5 Bolt is nemotron_h_moe, a name
llama-b11025 knows, and it still failed there: its expert counts are per layer, which only b11662 and
later read (2026-10-23). So a model is also loaded, without being downloaded (load_test): each file's
header - every key and every tensor's shape and type - is fetched from Hugging Face and written
before a sparse run of zeros as long as the real file, and each build loads that on the CPU, image
reader too. What the header can be wrong about, the build says, before 80 GB are fetched for nothing.
"""
import hashlib
import json
import logging
import os
import shutil
import struct
import subprocess
import tempfile
import threading
import time

import requests

from app import settings
from app.engine.profiles import load_profiles
from app.settings import PROJECT_ROOT
from utils import hf_client

log = logging.getLogger("engine")

CACHE_FILE = os.path.join(PROJECT_ROOT, "state", "arch-support.json")
LOAD_CACHE_FILE = os.path.join(PROJECT_ROOT, "state", "load-tests.json")
STAND_INS = os.path.join(PROJECT_ROOT, "state", "load-test")
LOAD_TIMEOUT = 180          # seconds a build gets to load a stand-in
HEADER_LIMIT = 256 * 1024**2  # a header past this (a huge vocabulary) is not fetched
_lock = threading.Lock()
_load_lock = threading.Lock()


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


# --- loading a model without downloading it -----------------------------------------------------

class _Short(Exception):
    """The header goes on past the bytes fetched so far."""


# GGUF value types: their sizes, and how to read one (8 = string, 9 = array)
_FIXED = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}


def _header_length(data):
    """Where a GGUF's tensor data begins, read from its first bytes: the header, every key and every
    tensor's description, then padding to the alignment. _Short when `data` ends before that."""
    pos = 0

    def take(n):
        nonlocal pos
        if pos + n > len(data):
            raise _Short()
        pos += n
        return data[pos - n:pos]

    def u32():
        return struct.unpack("<I", take(4))[0]

    def u64():
        return struct.unpack("<Q", take(8))[0]

    def string():
        return take(u64())

    def value(kind):
        if kind == 8:
            return string()
        if kind == 9:
            inner, count = u32(), u64()
            if inner in _FIXED:
                take(_FIXED[inner] * count)
                return None
            for _ in range(count):
                value(inner)
            return None
        raw = take(_FIXED[kind])
        return struct.unpack("<I", raw)[0] if kind == 4 else None

    if take(4) != b"GGUF":
        raise ValueError("not a GGUF file")
    version = u32()
    if version < 2:
        raise ValueError(f"GGUF version {version} is too old")
    tensors, keys = u64(), u64()
    alignment = 32
    for _ in range(keys):
        key = string()
        kind = u32()
        if kind not in _FIXED and kind not in (8, 9):
            raise ValueError(f"unknown GGUF value type {kind}")
        found = value(kind)
        if key == b"general.alignment" and found:
            alignment = found
    for _ in range(tensors):
        string()
        take(8 * u32())     # dimensions
        take(4 + 8)         # type, offset
    return (pos + alignment - 1) // alignment * alignment


def _fetch_header(model_id, path):
    """The header of one of a repository's files, fetched by range a few MB at a time."""
    url = f"https://huggingface.co/{model_id}/resolve/main/{path}"
    want = 4 * 1024**2
    while True:
        response = requests.get(url, headers={**hf_client._headers(), "Range": f"bytes=0-{want - 1}"},
                                timeout=60)
        response.raise_for_status()
        data = response.content
        try:
            return data[:_header_length(data)]
        except _Short:
            if len(data) < want or want >= HEADER_LIMIT:
                raise ValueError(f"{path}: header longer than {len(data)} bytes")
            want *= 4


def _stand_in(model_id, files, readers, sizes):
    """A folder holding every file under its own name: the real header, then zeros (sparse, so it
    takes no disk) to the real length. The model file to give a build, and its image reader."""
    folder = os.path.join(STAND_INS, hashlib.sha1(f"{model_id}|{files[0]}".encode()).hexdigest()[:16])
    os.makedirs(folder, exist_ok=True)
    try:
        for path in files + readers:
            target = os.path.join(folder, os.path.basename(path))
            header = _fetch_header(model_id, path)
            with open(target, "wb") as f:
                f.write(header)
                f.truncate(max(sizes.get(path) or 0, len(header)))
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise
    reader = os.path.join(folder, os.path.basename(readers[0])) if readers else None
    return folder, os.path.join(folder, os.path.basename(files[0])), reader


def _first_problem(output):
    """The line saying why a build would not load it, as specific as the log has it; None when no
    line says - a build killed from outside, by memory or a signal, has not refused the model."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for words in ("error loading model", "wrong shape", "wrong type", "unknown model architecture",
                  "Failed to load CLIP", "unknown projector", "failed to load", "error"):
        for line in lines:
            if words.lower() in line.lower():
                # the timestamp and level llama-server puts first
                return line.split(" E ", 1)[-1][:300]
    return None


def _load(build, model, reader):
    """Whether this build loads the stand-in, on the CPU only: (True, ""), (False, why), or (None, what
    happened) when it stopped without saying it could not."""
    args = [build["binary"], "-m", model, "-ngl", "0", "-c", "512", "--no-warmup", "--no-repack",
            "--host", "127.0.0.1", "--port", "0"]
    if reader:
        args += ["--mmproj", reader, "--no-mmproj-offload"]
    # No GPU at all: a model may be answering someone on it, and this loads zeros
    env = {**os.environ, **build["env"], "CUDA_VISIBLE_DEVICES": ""}
    with tempfile.TemporaryFile("w+") as out:
        process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, env=env)
        started = time.monotonic()
        try:
            while time.monotonic() - started < LOAD_TIMEOUT:
                time.sleep(0.5)
                out.seek(0)
                text = out.read()
                if "model loaded" in text or "server is listening" in text:
                    return True, ""
                if process.poll() is not None:
                    problem = _first_problem(text)
                    if problem is None:
                        return None, f"it stopped (exit {process.returncode}) without saying why"
                    return False, problem
            return None, f"it was still loading after {LOAD_TIMEOUT} s"
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def load_test(model_id, files, readers, sizes):
    """[{"name", "binary", "default", "loads", "problem"}] for every build: whether it loads this file
    (and its image reader), tried on a stand-in rather than guessed. loads is None when the test could
    not be made at all. Blocking - fetches headers and runs the builds - so call it from a thread;
    answers are kept per build and file in state/load-tests.json."""
    if not files:
        return []
    with _load_lock:
        try:
            with open(LOAD_CACHE_FILE, encoding="utf-8") as f:
                cache = json.load(f)
        except (OSError, ValueError):
            cache = {}
        subject = f"{model_id}/{files[0]}" + (f"+{readers[0]}" if readers else "")
        todo = [b for b in builds()
                if f"{b['binary']}@{int(os.path.getmtime(b['binary']))}|{subject}" not in cache]
        folder, fresh = None, {}
        if todo:
            try:
                folder, model, reader = _stand_in(model_id, files, readers, sizes)
            except (requests.RequestException, ValueError, OSError) as e:
                log.warning("could not make a stand-in for %s: %s", subject, e)
                model = None
            try:
                for build in todo:
                    stamp = f"{build['binary']}@{int(os.path.getmtime(build['binary']))}|{subject}"
                    if model is None:
                        continue            # not kept: the next check tries again
                    loads, problem = _load(build, model, reader)
                    log.info("load test of %s in %s: %s", subject, build["name"], "loads" if loads else problem)
                    fresh[stamp] = {"loads": loads, "problem": problem}
                    if loads is not None:       # an inconclusive run is tried again next time
                        cache[stamp] = fresh[stamp]
            finally:
                if folder:
                    shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(os.path.dirname(LOAD_CACHE_FILE), exist_ok=True)
            with open(LOAD_CACHE_FILE + ".new", "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(LOAD_CACHE_FILE + ".new", LOAD_CACHE_FILE)
        answers = []
        for build in builds():
            stamp = f"{build['binary']}@{int(os.path.getmtime(build['binary']))}|{subject}"
            known = cache.get(stamp) or fresh.get(stamp) or {}
            answers.append({k: build[k] for k in ("name", "binary", "default")}
                           | {"loads": known.get("loads"), "problem": known.get("problem", "")})
        return answers
