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
    # Thinking models spend most of their output on reasoning; 0 turns that phase off entirely
    reasoning_budget: int | None = None      # --reasoning-budget (0 = no thinking, -1 = unlimited)
    cache_reuse: int | None = None           # --cache-reuse: reuse a matching prompt prefix between turns
    n_predict: int | None = None             # --n-predict: hard cap on generated tokens
    chat_template_kwargs: str | None = None  # --chat-template-kwargs: JSON passed to the chat template
    mmproj: str | None = None                # --mmproj: vision projector GGUF; lets the model read images
    extra_args: list[str] = field(default_factory=list)
    # A llama-server of its own, for a model the configured build cannot load yet (Ollama ships a
    # newer one: qwen3.6's architecture loads there and not in llama-b11025), and what it needs in
    # its environment - Ollama's finds its CUDA backend only through GGML_BACKEND_PATH
    binary: str | None = None
    env: dict = field(default_factory=dict)
    ttl_seconds: int = 900           # unload after this long idle; 0 = keep loaded
    exclusive: bool = True           # stop other exclusive models first (one big model fits in 24 GB)
    # "35B": what Auto knows the model's size by (app/tools/chooser.py). Without it the size is read
    # from the name, and "qwen3.6-1m" names its window, not a model of a million parameters
    parameter_size: str | None = None
    # Multi-token prediction: "auto" uses it when the model file carries the layers for it
    # (gguf_meta.mtp_layers) and drops it if the engine will not start with it; "on" / "off" decide.
    # Flags for it written into extra_args by hand are left alone and win.
    mtp: str = "auto"

    def has_mtp_flags(self):
        return "--spec-type" in self.extra_args or "-st" in self.extra_args

    def command(self, binary, mtp=False):
        """The llama-server command line this profile describes; `mtp` adds the multi-token
        prediction draft (unless extra_args already say how to draft)."""
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
        if self.reasoning_budget is not None:
            args += ["--reasoning-budget", str(self.reasoning_budget)]
        if self.cache_reuse is not None:
            # Long chats re-send the whole history; reusing the cached prefix avoids re-reading it
            args += ["--cache-reuse", str(self.cache_reuse)]
        if self.n_predict is not None:
            args += ["--n-predict", str(self.n_predict)]
        if self.chat_template_kwargs:
            args += ["--chat-template-kwargs", self.chat_template_kwargs]
        if self.mmproj:
            args += ["--mmproj", self.mmproj]
        if mtp and not self.has_mtp_flags():
            # Two tokens ahead: each further guess is less often right, and two measured fastest
            args += MTP_ARGS
        return args + list(self.extra_args)


MTP_ARGS = ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]


def engines_file():
    path = settings.get("engine.profiles.file", "ENGINE_PROFILES_FILE") or "config/engines.yaml"
    return path if os.path.isabs(path) else os.path.join(PROJECT_ROOT, path)


def local_engines_file():
    """Profiles this machine added for itself - models downloaded for llama.cpp from the Models page.
    Kept apart from engines.yaml, which comes from git and is replaced by every deploy."""
    return os.path.join(os.path.dirname(engines_file()), "engines.local.yaml")


def _read(path):
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}


def load_profiles():
    """Reads the profile files on each call, so edits apply without a restart: engines.yaml, then the
    local ones, which take ports after it and the same defaults."""
    data = _read(engines_file())
    if not data:
        return {}
    defaults = data.get("defaults") or {}
    profiles = {}
    next_port = int(data.get("start_port", 8090))
    known = {f for f in EngineProfile.__dataclass_fields__ if f not in ("name", "model_path", "port")}
    local = _read(local_engines_file())
    removed = set(local.get("removed") or ())
    for entry in (data.get("models") or []) + (local.get("models") or []):
        merged = {**defaults, **entry}
        name = merged.get("name")
        model_path = merged.get("model_path")
        if not name or not model_path or name in profiles:
            continue
        if name in removed:
            # Deleted from the Models page. Its place in the port order is kept, so the profiles after
            # it keep their ports
            next_port = max(next_port, int(merged.get("port") or next_port) + 1)
            continue
        port = int(merged.get("port") or next_port)
        next_port = max(next_port, port + 1)
        profiles[name] = EngineProfile(
            name=name, model_path=model_path, port=port,
            **{k: v for k, v in merged.items() if k in known},
        )
    return profiles


def local_profiles():
    """The entries of engines.local.yaml, as written there."""
    return [m for m in (_read(local_engines_file()).get("models") or []) if isinstance(m, dict) and m.get("name")]


def add_local_profile(entry):
    """Adds (or replaces, by name) a profile in engines.local.yaml - no longer removed, if it was."""
    _write_local([m for m in local_profiles() if m.get("name") != entry["name"]] + [entry],
                 removed=[n for n in _read(local_engines_file()).get("removed") or () if n != entry["name"]])


def remove_profile(name):
    """Deletes a profile: one of this machine's is taken out of engines.local.yaml, one of
    engines.yaml's - which every deploy writes back - is listed there as removed. True when there
    was such a profile."""
    models = local_profiles()
    if any(m.get("name") == name for m in models):
        _write_local([m for m in models if m.get("name") != name])
        return True
    if name in load_profiles():
        _write_local(models, removed=sorted(set(_read(local_engines_file()).get("removed") or ()) | {name}))
        return True
    return False


def _write_local(models, removed=None):
    path = local_engines_file()
    if removed is None:
        removed = list(_read(path).get("removed") or ())
    with open(path + ".new", "w", encoding="utf-8") as f:
        f.write("# Engine profiles this machine added for itself (Models page: Download for llama.cpp).\n"
                "# Read after config/engines.yaml, with its defaults; deploys leave this file alone.\n"
                "# removed: engines.yaml's profiles deleted from the Models page, skipped when read.\n")
        yaml.safe_dump({"models": models} | ({"removed": removed} if removed else {}), f,
                       sort_keys=False, allow_unicode=True, width=120)
    os.replace(path + ".new", path)
