"""Starts, stops and swaps llama-server processes.

Only one large model fits in 24 GB, so profiles marked exclusive stop each other. A model is started
on the first request that needs it and unloaded again once it has been idle for its TTL.
"""
import asyncio
import contextlib
import logging
import os
import shutil
import time

import httpx

from app import settings
from app.settings import PROJECT_ROOT
from app.engine.profiles import load_profiles
from utils.ollama_client import ollama_host
from utils.system_info import detect_gpus

log = logging.getLogger("engine")

STARTUP_TIMEOUT = 600  # big models take a while to load from disk
REAPER_INTERVAL = 30
BUSY_WAIT = 90      # seconds a model waits for a busy one; under the 100 s Cloudflare allows a silent request
SMALL_OLLAMA_MODEL = 4 * 1024**3  # below this an Ollama model never makes a llama.cpp model give way


LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
LOG_CAP = 20 * 1024 * 1024       # per engine; the one before is kept as .1


def engine_log(name):
    return os.path.join(LOG_DIR, f"engine-{name}.log")


async def _drain(process, path):
    """An engine's output, into logs/engine-<name>.log, for as long as it runs."""
    os.makedirs(LOG_DIR, exist_ok=True)
    out = open(path, "ab")
    try:
        while True:
            chunk = await process.stderr.read(65536)
            if not chunk:
                break
            out.write(chunk)
            out.flush()
            if out.tell() > LOG_CAP:
                out.close()
                os.replace(path, path + ".1")
                out = open(path, "ab")
    finally:
        out.close()


class RunningModel:
    def __init__(self, profile, process):
        self.profile = profile
        self.process = process
        self.started_at = time.monotonic()
        self.last_used_at = time.monotonic()
        self.in_flight = 0
        self.drain = None

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.profile.port}"

    @property
    def idle_seconds(self):
        return 0 if self.in_flight else time.monotonic() - self.last_used_at

    def status(self):
        return {
            "name": self.profile.name,
            "port": self.profile.port,
            "pid": self.process.pid,
            "running": self.process.returncode is None,
            "uptime_seconds": int(time.monotonic() - self.started_at),
            "idle_seconds": int(self.idle_seconds),
            "in_flight": self.in_flight,
            "ttl_seconds": self.profile.ttl_seconds,
        }


class Supervisor:
    def __init__(self):
        self._running: dict[str, RunningModel] = {}
        self._lock = asyncio.Lock()
        self._reaper: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------

    def binary(self):
        configured = settings.get("engine.llama_server.path", "LLAMA_SERVER_PATH")
        return configured or shutil.which("llama-server") or ""

    def is_available(self):
        binary = self.binary()
        return bool(binary) and os.path.isfile(binary) and os.access(binary, os.X_OK)

    async def start_reaper(self):
        await self.stop_strays()
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle_loop())

    async def stop_strays(self):
        """Engines a previous gateway started and never stopped, stopped now.

        A restart that cannot wait for the old gateway to drain kills it (bin/deploy.sh gives it 45 s),
        and then nothing stops its engines: one kept qwen3.6 on the GPU for eight hours, unknown to
        the new gateway, which would have started a second copy on the same port and found no room
        (2026-10-19). An engine is known by its own command line - one of our profiles' names after
        --alias and its port after --port - so Ollama's runner, also a llama-server, is never touched.
        """
        ours = {(p.name, str(p.port)) for p in load_profiles().values()}
        tracked = {model.process.pid for model in self._running.values() if getattr(model, "process", None)}
        strays = []
        for pid in filter(str.isdigit, os.listdir("/proc") if os.path.isdir("/proc") else []):
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    args = f.read().decode(errors="replace").split("\0")
            except OSError:
                continue
            if not args or not os.path.basename(args[0]).startswith("llama-server") or int(pid) in tracked:
                continue
            named = dict(zip(args, args[1:]))
            if (named.get("--alias"), named.get("--port")) in ours:
                strays.append((int(pid), named["--alias"]))
        for pid, name in strays:
            log.warning("stopping %s (pid %s): left running by a gateway that did not stop it", name, pid)
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 15)
        for _ in range(20):
            if not any(os.path.exists(f"/proc/{pid}") for pid, _ in strays):
                return
            await asyncio.sleep(0.5)
        for pid, _ in strays:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, 9)

    async def shutdown(self):
        if self._reaper:
            self._reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reaper
            self._reaper = None
        for name in list(self._running):
            await self.stop(name)

    async def _reap_idle_loop(self):
        while True:
            await asyncio.sleep(REAPER_INTERVAL)
            for name, model in list(self._running.items()):
                ttl = model.profile.ttl_seconds
                if ttl and model.idle_seconds > ttl:
                    log.info("Unloading %s after %ss idle", name, int(model.idle_seconds))
                    await self.stop(name)

    # --- starting and stopping -------------------------------------------

    async def ensure_running(self, name):
        """Returns the RunningModel for this profile, starting it (and freeing VRAM) if needed."""
        profiles = load_profiles()
        profile = profiles.get(name)
        if profile is None:
            return None, f"No engine profile named '{name}'"
        if not self.is_available():
            return None, "llama-server binary not found; set engine.llama_server.path"

        # A model in the middle of someone's answer is never stopped to make room for another: that
        # cut the answer off, and alternating two requests kept the GPU reloading for everybody.
        # The new model waits for it, up to BUSY_WAIT seconds, and then says the GPU is busy.
        waited = 0.0
        while True:
            async with self._lock:
                existing = self._running.get(name)
                if existing and existing.process.returncode is None:
                    existing.last_used_at = time.monotonic()
                    return existing, None
                if existing:
                    self._running.pop(name, None)

                busy = [other_name for other_name, other in self._running.items()
                        if profile.exclusive and other.profile.exclusive and other_name != name
                        and other.in_flight > 0 and other.process.returncode is None]
                if not busy:
                    if profile.exclusive and settings.get_bool("engine.unload_ollama_first",
                                                               "ENGINE_UNLOAD_OLLAMA_FIRST"):
                        await self._unload_ollama_models()
                    if profile.exclusive:
                        for other_name, other in list(self._running.items()):
                            if other.profile.exclusive and other_name != name:
                                log.info("Stopping %s to free VRAM for %s", other_name, name)
                                await self._stop_locked(other_name)
                    return await self._start_locked(profile)
            if waited >= BUSY_WAIT:
                return None, (f"The GPU is busy answering with {', '.join(busy)}; "
                              f"try {name} again in a minute")
            if waited == 0:
                log.info("%s waits for %s to finish answering", name, ", ".join(busy))
            await asyncio.sleep(2)
            waited += 2

    async def make_room_for_ollama(self, model):
        """Stops idle llama.cpp models when an Ollama model is about to load and would not fit beside them.

        The other half of engine.unload_ollama_first. A llama.cpp model keeps the GPU for its whole TTL
        (qwen3-next-80b holds 22.5 GB for an hour), and Ollama loads into whatever VRAM is left: with
        under a gigabyte free it puts 0 of gemma4:31b's 61 layers on the GPU and runs all of it on the
        CPU, many times slower. Only when the model does not fit, though - the playground's small
        router model writes a search query between two turns of the 80B, and stopping the 80B for that
        would reload it on every turn. A llama.cpp model still answering someone is left alone.
        """
        if not settings.get_bool("engine.unload_ollama_first", "ENGINE_UNLOAD_OLLAMA_FIRST"):
            return
        if not any(m.profile.exclusive and m.process.returncode is None for m in self._running.values()):
            return
        host = ollama_host()
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                loaded = {e.get("name") or e.get("model"): e
                          for e in (await client.get(f"{host}/api/ps")).json().get("models") or []}
                here = loaded.get(model)
                if here and here.get("size_vram", 0) >= 0.9 * here.get("size", 1):
                    return  # already on the GPU
                tags = (await client.get(f"{host}/api/tags")).json().get("models") or []
                size = next((t.get("size") or 0 for t in tags if t.get("name") == model), 0)
                # The router (qwen3:1.7b) and embedding models are small enough to run on the CPU, and
                # never fit beside the 80B anyway: stopping it for them would reload it every turn
                if size < SMALL_OLLAMA_MODEL:
                    return
                if here:
                    # Loaded, but squeezed onto the CPU: drop it so it comes back on the GPU
                    await client.post(f"{host}/api/generate", json={"model": model, "keep_alive": 0})
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Could not ask Ollama about %s: %s", model, e)
            return
        free = sum(g["vram_free"] for g in await asyncio.to_thread(detect_gpus))
        # Weights plus room for the KV cache and compute buffers
        if free >= size * 1.15 + 1024**3:
            return
        busy = []
        async with self._lock:
            for name, running in list(self._running.items()):
                if not running.profile.exclusive:
                    continue
                if running.in_flight:
                    busy.append(name)
                    continue
                log.info("Stopping %s to free VRAM for Ollama's %s (%.1f GB free, %.1f GB needed)",
                         name, model, free / 1024**3, size / 1024**3)
                await self._stop_locked(name)
        if busy:
            # Ollama was once left to fall back to the CPU here. With a vision projector it cannot:
            # qwen3.6 died loading with "cudaMalloc failed: out of memory" while qwen3.6-1m answered
            # a Claude Code session, and the playground showed the raw CUDA error. Said plainly instead.
            log.warning("%s needs the GPU, but %s is answering a request", model, ", ".join(busy))
            return (f"The GPU is busy: {', '.join(busy)} is answering another request and holds all of "
                    f"its memory, so {model} cannot be loaded now. Try again when that finishes, or "
                    f"pick {busy[0]} or a small model.")
        return None

    def blocked_by_busy_gpu(self, name, backend):
        """Whether this model cannot be loaded now, because a model answering someone holds the GPU.

        For Auto, which should not pick it: an exclusive llama.cpp model would have to stop the busy
        one, and a large Ollama model would not fit beside it (make_room_for_ollama). The busy model
        itself is fine - its request waits its turn - and so are small Ollama models, run on the CPU.
        """
        busy = [n for n, m in self._running.items()
                if m.profile.exclusive and m.in_flight and m.process.returncode is None]
        if not busy or name in busy:
            return False
        if backend.kind == "llamacpp":
            profile = load_profiles().get(name)
            return bool(profile and profile.exclusive)
        if backend.kind == "ollama":
            return (backend.size_bytes or 0) >= SMALL_OLLAMA_MODEL
        return False

    async def unload_everything(self):
        """Stops every llama.cpp model and has Ollama drop its own, for a machine about to sleep.

        A suspended GPU keeps nothing worth waking up to: the first request after a wake reloads its
        model either way, and a CUDA process carried through suspend and resume can come back unable to
        use the card. Unloading first also means a model nobody is using never keeps the machine awake.
        A llama.cpp model still answering someone is left alone - the caller should not be suspending
        then, and says so. Returns what was done, for the log.
        """
        stopped, busy = [], []
        async with self._lock:
            for name, running in list(self._running.items()):
                if running.in_flight:
                    busy.append(name)
                    continue
                log.info("Unloading %s before the machine sleeps", name)
                await self._stop_locked(name)
                stopped.append(name)
            await self._unload_ollama_models(gpu_only=False)
        return {"stopped": stopped, "busy": busy}

    async def _unload_ollama_models(self, gpu_only=True):
        """Asks Ollama to drop its loaded models; they otherwise keep the GPU full (OLLAMA_KEEP_ALIVE=-1).

        Only the ones on the GPU, unless gpu_only is off: the router and embedding models run on the
        CPU (num_gpu 0), hold no VRAM, and would only be reloaded a moment later for nothing.
        """
        host = ollama_host()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(f"{host}/api/ps")
                response.raise_for_status()
                loaded = [e.get("model") or e.get("name") for e in response.json().get("models") or []
                          if not gpu_only or e.get("size_vram", 1)]
                for name in filter(None, loaded):
                    log.info("Asking Ollama to unload %s", name)
                    # keep_alive 0 unloads immediately. Ollama reports failures in the body with HTTP 200.
                    result = await client.post(f"{host}/api/generate", json={"model": name, "keep_alive": 0})
                    body = {}
                    if result.headers.get("content-type", "").startswith("application/json"):
                        body = result.json()
                    if result.status_code >= 400 or body.get("error"):
                        log.warning("Ollama would not unload %s: %s", name, body.get("error") or result.text[:200])

                for _ in range(15):
                    await asyncio.sleep(1)
                    still_loaded = [e for e in (await client.get(f"{host}/api/ps")).json().get("models") or []
                                    if not gpu_only or e.get("size_vram", 1)]
                    if not still_loaded:
                        log.info("Ollama released its models")
                        return
                log.warning("Ollama still holds %s after the unload request", [m.get("model") for m in still_loaded])
        except (httpx.HTTPError, ValueError) as e:
            log.warning("Could not reach Ollama to unload models: %s", e)

    async def _start_locked(self, profile):
        if not os.path.isfile(profile.model_path):
            return None, f"Model file not found: {profile.model_path}"

        binary = profile.binary or self.binary()
        if profile.binary and not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
            return None, f"llama-server not found at {binary} (profile {profile.name})"
        command = profile.command(binary)
        env = dict(os.environ)
        library_path = settings.get("engine.library.path", "ENGINE_LIBRARY_PATH")
        if library_path and not profile.binary:
            env["LD_LIBRARY_PATH"] = library_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        env.update({str(k): str(v) for k, v in (profile.env or {}).items()})

        log.info("Starting %s: %s", profile.name, " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        model = RunningModel(profile, process)
        # Read for as long as it runs: a pipe nobody reads fills, and the server then blocks on its
        # next log line in the middle of somebody's answer. Kept, so its errors can be read later.
        model.drain = asyncio.create_task(_drain(process, engine_log(profile.name)))
        ready, problem = await self._wait_until_ready(model)
        if not ready:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            return None, problem

        self._running[profile.name] = model
        return model, None

    async def _wait_until_ready(self, model):
        deadline = time.monotonic() + STARTUP_TIMEOUT
        async with httpx.AsyncClient(timeout=5) as client:
            while time.monotonic() < deadline:
                if model.process.returncode is not None:
                    return False, f"llama-server exited ({model.process.returncode}): {await self._stderr_tail(model)}"
                try:
                    response = await client.get(f"{model.base_url}/health")
                    if response.status_code == 200:
                        return True, None
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        return False, f"llama-server did not become ready within {STARTUP_TIMEOUT}s"

    @staticmethod
    async def _stderr_tail(model, limit=500):
        """llama-server prints pages of warnings before the fatal error, so keep the end."""
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(model.drain), timeout=3)   # the last lines, written
        try:
            with open(engine_log(model.profile.name), "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - 65536))
                text = f.read().decode(errors="replace").strip()
        except OSError:
            return "(no output)"
        interesting = [line for line in text.splitlines()
                       if any(word in line.lower() for word in ("error", "failed", "cannot", "out of memory", "unable"))]
        return (interesting[-1] if interesting else text.splitlines()[-1] if text.splitlines() else "(no output)")[-limit:]

    async def stop(self, name):
        async with self._lock:
            return await self._stop_locked(name)

    async def _stop_locked(self, name):
        model = self._running.pop(name, None)
        if model is None:
            return False
        process = model.process
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=20)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        return True

    # --- introspection ----------------------------------------------------

    def loaded_names(self):
        """The llama.cpp models running now, so a helper task can use one without loading another."""
        return [name for name, m in self._running.items() if m.process.returncode is None]

    async def ollama_loaded(self):
        """Ollama's models on the GPU now; empty when Ollama cannot be asked."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                models = (await client.get(f"{ollama_host()}/api/ps")).json().get("models") or []
        except (httpx.HTTPError, ValueError):
            return []
        return [m.get("name") or m.get("model") for m in models if m.get("size_vram")]

    def status(self):
        profiles = load_profiles()
        return {
            "binary": self.binary(),
            "available": self.is_available(),
            "profiles": sorted(profiles),
            "running": [model.status() for model in self._running.values()],
        }

    def track_request(self, model):
        """Context manager that keeps a model from being reaped mid-request."""
        supervisor = self

        class _Tracker:
            def __enter__(self):
                model.in_flight += 1
                model.last_used_at = time.monotonic()
                return model

            def __exit__(self, *exc):
                model.in_flight = max(0, model.in_flight - 1)
                model.last_used_at = time.monotonic()
                return False

        del supervisor
        return _Tracker()


supervisor = Supervisor()
