"""Per-model llama-server launch profiles, loaded from config/engines.yaml.

Each profile is one llama-server process: which GGUF to load and exactly how to split it between
GPU and CPU. These are the knobs Ollama does not expose.
"""
import os
from dataclasses import dataclass, field

import yaml

from app import settings

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@dataclass
class EngineProfile:
    name: str                       # model name clients request
    model_path: str                 # GGUF file on disk
    port: int
    n_gpu_layers: int | None = None  # -ngl: layers on GPU ("all" = 999)
    n_cpu_ffn: int | None = None     # -ncffn: dense FFN weights of the first N layers kept on CPU
    n_cpu_moe: int | None = None     # --n-cpu-moe: MoE expert weights of the first N layers on CPU
    ctx_size: int = 8192             # -c
    cache_type_k: str = "q8_0"       # -ctk
    cache_type_v: str = "q8_0"       # -ctv
    flash_attn: bool = True
    batch_size: int | None = None    # -b
    ubatch_size: int | None = None   # -ub
    parallel: int | None = None      # -np: server slots
    threads: int | None = None       # -t
    override_tensor: str | None = None  # -ot: per-tensor placement regex
    extra_args: list[str] = field(default_factory=list)
    ttl_seconds: int = 900           # unload after this long idle; 0 = keep loaded
    exclusive: bool = True           # stop other exclusive models first (one big model fits in 24 GB)

    def command(self, binary):
        """The llama-server command line this profile describes."""
        args = [binary, "--model", self.model_path, "--host", "127.0.0.1", "--port", str(self.port),
                "--ctx-size", str(self.ctx_size), "--alias", self.name]
        if self.n_gpu_layers is not None:
            args += ["--n-gpu-layers", str(self.n_gpu_layers)]
        if self.n_cpu_ffn is not None:
            args += ["--n-cpu-ffn", str(self.n_cpu_ffn)]
        if self.n_cpu_moe is not None:
            args += ["--n-cpu-moe", str(self.n_cpu_moe)]
        if self.flash_attn:
            args += ["--flash-attn", "on"]
        # KV cache quantization needs flash attention; it is what buys back context VRAM
        args += ["--cache-type-k", self.cache_type_k, "--cache-type-v", self.cache_type_v]
        if self.batch_size:
            args += ["--batch-size", str(self.batch_size)]
        if self.ubatch_size:
            args += ["--ubatch-size", str(self.ubatch_size)]
        if self.parallel:
            args += ["--parallel", str(self.parallel)]
        if self.threads:
            args += ["--threads", str(self.threads)]
        if self.override_tensor:
            args += ["--override-tensor", self.override_tensor]
        return args + list(self.extra_args)


def engines_file():
    path = settings.get("engine.profiles.file", "ENGINE_PROFILES_FILE") or "config/engines.yaml"
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def load_profiles():
    """Reads the profile file on each call, so edits apply without a restart."""
    try:
        with open(engines_file(), encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}

    defaults = data.get("defaults") or {}
    profiles = {}
    next_port = int(data.get("start_port", 8090))
    for entry in data.get("models") or []:
        merged = {**defaults, **entry}
        name = merged.get("name")
        model_path = merged.get("model_path")
        if not name or not model_path:
            continue
        port = int(merged.get("port") or next_port)
        next_port = max(next_port, port + 1)
        known = {f for f in EngineProfile.__dataclass_fields__ if f not in ("name", "model_path", "port")}
        profiles[name] = EngineProfile(
            name=name, model_path=model_path, port=port,
            **{k: v for k, v in merged.items() if k in known},
        )
    return profiles
