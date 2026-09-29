"""Hardware facts used by both the checker and the gateway dashboard.

When the app does not run on the Ollama server itself, the server's hardware can be set in the
properties file instead of detected here.
"""
import subprocess
import sys

import psutil

from utils import config


def detect_gpus():
    gpu_info = []
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                free, total = torch.cuda.mem_get_info(i)
                gpu_info.append({"name": torch.cuda.get_device_name(i), "vram_total": total,
                                 "vram_free": free, "index": i})
    except Exception:
        # torch is optional (and may be CPU-only); fall back to nvidia-smi
        gpu_info = []

    if not gpu_info:
        try:
            nvidia_smi = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if nvidia_smi.returncode == 0:
                for line in nvidia_smi.stdout.strip().splitlines():
                    parts = [p.strip() for p in line.rsplit(",", 2)]
                    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
                        gpu_info.append({"name": parts[0], "vram_total": int(parts[1]) * 1024 * 1024,
                                         "vram_free": int(parts[2]) * 1024 * 1024, "index": len(gpu_info)})
        except (subprocess.SubprocessError, OSError):
            pass
    return gpu_info


def get_system_info():
    configured_vram_gb = config.get_float("ollama.server.vram.gb", "OLLAMA_SERVER_VRAM_GB")
    configured_ram_gb = config.get_float("ollama.server.ram.gb", "OLLAMA_SERVER_RAM_GB")

    if configured_vram_gb is not None:
        vram = int(configured_vram_gb * 1024**3)
        gpu_info = [{"name": "Ollama server GPU", "vram_total": vram, "vram_free": vram, "index": 0}] if vram > 0 else []
    else:
        gpu_info = detect_gpus()

    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    configured_ram = int(configured_ram_gb * 1024**3) if configured_ram_gb is not None else None

    return {
        "gpu_info": gpu_info,
        "gpu_source": "config" if configured_vram_gb is not None else "detected",
        "total_vram": sum(gpu["vram_total"] for gpu in gpu_info),
        "total_ram": configured_ram if configured_ram is not None else mem.total,
        "available_ram": configured_ram if configured_ram is not None else mem.available,
        "ram_source": "config" if configured_ram is not None else "detected",
        "used_ram_percent": mem.percent,
        "swap_total": swap.total,
        "swap_used": swap.used,
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "cpu_count": psutil.cpu_count(),
        "platform": sys.platform,
    }
