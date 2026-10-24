"""CPU, memory and every GPU as they are now, for the dashboard's live panel, with a short history.

Measured only when asked: the dashboard asks every few seconds while it is open and visible, and
nothing samples otherwise - a machine that sleeps when idle should not be kept busy measuring itself.
Each answer is one sample; the samples of the last few minutes are kept, so a dashboard opened now
starts with a history instead of an empty graph.
"""
import subprocess
import threading
import time
from collections import deque

import psutil

HISTORY = 240           # samples kept: twelve minutes at the dashboard's 3 s
MIN_GAP = 2.0           # seconds; a second tab polling as well does not take twice the samples

_history = deque(maxlen=HISTORY)
_lock = threading.Lock()
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


def current(with_history=False):
    """The newest sample - a fresh one unless one was taken under MIN_GAP ago - and, when asked,
    every sample kept. Blocking (nvidia-smi): call it from a thread."""
    with _lock:
        if not _history or time.time() - _history[-1]["at"] >= MIN_GAP:
            _history.append(_sample())
        latest = _history[-1]
        return {"now": latest, **({"history": list(_history)} if with_history else {})}
