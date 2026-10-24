"""CPU, memory and every GPU as they are now, for the dashboard's live panel, with a short history.

Measured while someone is watching: a dashboard asking starts a sampler that measures every
SAMPLE_SECONDS on its own, and goes on for KEEP_SAMPLING after the last ask. A page stops asking while
its tab is hidden - which the browser also says of a minimised or covered window - and the sampler
fills that gap, so the page comes back to whole charts. Nobody watching for half an hour, and it
stops: a machine that sleeps when idle should not be kept busy measuring itself. (It is no work the
idle check sees: well under 1 % of a core, and no requests.)
"""
import subprocess
import threading
import time
from collections import deque

import psutil

HISTORY = 240           # samples kept: twelve minutes at SAMPLE_SECONDS
SAMPLE_SECONDS = 3.0
MIN_GAP = 2.0           # seconds; an ask just after a sample is answered with that sample
KEEP_SAMPLING = 30 * 60  # seconds the sampler runs on after the last ask

_history = deque(maxlen=HISTORY)
_lock = threading.Lock()
_last_asked = 0.0
_sampler: threading.Thread | None = None
psutil.cpu_percent(interval=None)    # the first call only starts the count


def _gpus():
    """[{index, name, util, mem_used, mem_total, temp, power, power_limit, fan}] from nvidia-smi; [] without it."""
    fields = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,fan.speed"
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []

    def number(text):
        try:
            return float(text)
        except ValueError:      # "[N/A]" on a card that does not report it
            return None

    gpus = []
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 9:
            continue
        index, name, util, used, total, temp, power, limit, fan = parts
        gpus.append({"index": int(index), "name": name, "util": number(util),
                     # MiB as nvidia-smi gives them, in bytes like the rest of the page
                     "mem_used": int((number(used) or 0) * 1024**2), "mem_total": int((number(total) or 0) * 1024**2),
                     "temp": number(temp), "power": number(power), "power_limit": number(limit), "fan": number(fan)})
    return gpus


def _cpu_temperature():
    """The CPU package's temperature, where the kernel reports one (k10temp's Tctl on Ryzen)."""
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return None
    for chip in ("k10temp", "coretemp", "zenpower", "cpu_thermal"):
        for reading in sensors.get(chip) or ():
            if reading.label in ("Tctl", "Tdie", "Package id 0", "") and reading.current:
                return reading.current
    return None


def _sample():
    memory = psutil.virtual_memory()
    load1, _load5, _load15 = psutil.getloadavg()
    return {"at": time.time(),
            "cpu": psutil.cpu_percent(interval=None), "cpu_count": psutil.cpu_count(),
            "cpu_temp": _cpu_temperature(), "load1": load1,
            "ram_used": memory.total - memory.available, "ram_total": memory.total,
            "swap_used": psutil.swap_memory().used,
            "gpus": _gpus()}


def _take():
    """A sample, unless one was taken under MIN_GAP ago; the newest either way. Holds the lock."""
    if not _history or time.time() - _history[-1]["at"] >= MIN_GAP:
        _history.append(_sample())
    return _history[-1]


def _sample_while_watched():
    global _sampler
    while True:
        with _lock:
            if time.monotonic() - _last_asked > KEEP_SAMPLING:
                _sampler = None
                return
            _take()
        time.sleep(SAMPLE_SECONDS)


def current(with_history=False):
    """The newest sample and, when asked, every sample kept; (re)starts the sampler, which goes on
    for KEEP_SAMPLING. Blocking (nvidia-smi): call it from a thread."""
    global _last_asked, _sampler
    with _lock:
        _last_asked = time.monotonic()
        if _sampler is None:
            _sampler = threading.Thread(target=_sample_while_watched, name="live-metrics", daemon=True)
            _sampler.start()
        latest = _take()
        return {"now": latest, **({"history": list(_history)} if with_history else {})}
