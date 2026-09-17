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
from app.engine.profiles import load_profiles

log = logging.getLogger("engine")

STARTUP_TIMEOUT = 600  # big models take a while to load from disk
REAPER_INTERVAL = 30


class RunningModel:
    def __init__(self, profile, process):
        self.profile = profile
        self.process = process
        self.started_at = time.monotonic()
        self.last_used_at = time.monotonic()
        self.in_flight = 0

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
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_idle_loop())

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

        async with self._lock:
            existing = self._running.get(name)
            if existing and existing.process.returncode is None:
                existing.last_used_at = time.monotonic()
                return existing, None
            if existing:
                self._running.pop(name, None)

            if profile.exclusive:
                for other_name, other in list(self._running.items()):
                    if other.profile.exclusive and other_name != name:
                        log.info("Stopping %s to free VRAM for %s", other_name, name)
                        await self._stop_locked(other_name)

            model, problem = await self._start_locked(profile)
            return model, problem

    async def _start_locked(self, profile):
        if not os.path.isfile(profile.model_path):
            return None, f"Model file not found: {profile.model_path}"

        command = profile.command(self.binary())
        env = dict(os.environ)
        library_path = settings.get("engine.library.path", "ENGINE_LIBRARY_PATH")
        if library_path:
            env["LD_LIBRARY_PATH"] = library_path + os.pathsep + env.get("LD_LIBRARY_PATH", "")

        log.info("Starting %s: %s", profile.name, " ".join(command))
        process = await asyncio.create_subprocess_exec(
            *command, env=env,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        model = RunningModel(profile, process)
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
                    stderr = b""
                    if model.process.stderr:
                        with contextlib.suppress(Exception):
                            stderr = await asyncio.wait_for(model.process.stderr.read(2000), timeout=2)
                    return False, f"llama-server exited ({model.process.returncode}): {stderr.decode(errors='replace')[-400:]}"
                try:
                    response = await client.get(f"{model.base_url}/health")
                    if response.status_code == 200:
                        return True, None
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
        return False, f"llama-server did not become ready within {STARTUP_TIMEOUT}s"

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
