"""Whether vLLM and SGLang can serve a model here, and the engine profile that does.

Unlike llama.cpp and Ollama, neither loads GGUF: they run a model's own files - safetensors and its
config.json - split across both GPUs. So a repository is judged by those: the one asked about when it
publishes them, else the model it was made from (a GGUF conversion names its base model). Then:
  - its architecture, as the installed engine itself lists them (vLLM's model registry, SGLang's
    model files) - read from the package on disk, so an upgrade is known without a change here;
  - its quantization: an FP4 one (NVFP4, MXFP4) is made for newer GPUs than these RTX 3090s;
  - its weights, which must fit in the GPUs with room left for the cache.
Not supported says why, and where a 4-bit version of it would fit, names some.
"""
import glob
import json
import os
import re
import threading

import requests

from app import settings
from utils import hf_client

ENGINES = {"vllm": "vLLM", "sglang": "SGLang"}
# Where each was installed (2026-10-26); settings can point elsewhere
DEFAULT_BINARY = {"vllm": "~/inference/vllm-venv/bin/vllm", "sglang": "~/inference/sglang-venv/bin/python"}
WEIGHTS_SHARE = 0.80     # of all VRAM the weights may take: the rest is the cache, the activations and CUDA's own
FP4 = ("nvfp4", "mxfp4", "fp4", "modelopt_fp4", "w4afp8")

_cache, _lock = {}, threading.Lock()


def binary(server):
    path = settings.get(f"engine.{server}.binary", f"ENGINE_{server.upper()}_BINARY") or DEFAULT_BINARY[server]
    return os.path.expanduser(path)


def _site_packages(server):
    venv = os.path.dirname(os.path.dirname(binary(server)))
    found = sorted(glob.glob(os.path.join(venv, "lib", "python3*", "site-packages")))
    return found[-1] if found else None


# Where each engine names what it can run and its parsers: (architectures, tool-call parsers, reasoning parsers)
_SOURCES = {
    "vllm": ("vllm/model_executor/models/registry.py", "vllm/tool_parsers/__init__.py", "vllm/reasoning/__init__.py"),
    "sglang": ("sglang/srt/models/*.py", "sglang/srt/function_call/function_call_parser.py",
               "sglang/srt/parser/reasoning_parser.py"),
}


def _read(paths):
    text = ""
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text += f.read() + "\n"
        except OSError:
            pass
    return text


def known(server):
    """{"installed", "architectures", "tools", "reasoning"} as the installed engine has them; kept until
    its package changes. Empty sets when it is not installed."""
    site = _site_packages(server)
    if not site or not os.path.isfile(binary(server)):
        return {"installed": False, "architectures": set(), "tools": set(), "reasoning": set()}
    files = [sorted(glob.glob(os.path.join(site, pattern))) for pattern in _SOURCES[server]]
    stamp = tuple(int(os.path.getmtime(f)) for group in files for f in group[:3])
    with _lock:
        if _cache.get(server, (None,))[0] == stamp:
            return _cache[server][1]
    arch_text, tool_text, reasoning_text = (_read(group) for group in files)
    if server == "vllm":
        architectures = set(re.findall(r'"([A-Z][A-Za-z0-9_]*For[A-Za-z]+)"', arch_text))
    else:
        architectures = set()
        for line in re.findall(r"^EntryClass\s*=\s*(.+)$", arch_text, flags=re.M):
            architectures |= set(re.findall(r"[A-Z][A-Za-z0-9_]+", line))
    names = re.compile(r'^\s*"([A-Za-z0-9_.-]+)"\s*:', re.M)
    answer = {"installed": True, "architectures": architectures,
              "tools": set(names.findall(tool_text)), "reasoning": set(names.findall(reasoning_text))}
    with _lock:
        _cache[server] = (stamp, answer)
    return answer


# The parsers a model family speaks, in each engine's names: (tool calls, reasoning). The first of a
# family's names the installed engine knows is used; none, and the model answers without tool calls.
PARSERS = [
    (r"^Qwen3|^Qwen3_5|^Qwen3Next", {"vllm": (["qwen3_coder"], ["qwen3"]), "sglang": (["qwen3_coder"], ["qwen3"])}),
    (r"^DeepseekV41", {"vllm": (["deepseek_v41", "deepseek_v4"], ["deepseek_v41", "deepseek_r1"]),
                       "sglang": (["deepseekv41", "deepseekv4"], ["deepseek-v41", "deepseek-r1"])}),
    (r"^DeepseekV4", {"vllm": (["deepseek_v4"], ["deepseek_v4", "deepseek_r1"]),
                      "sglang": (["deepseekv4"], ["deepseek-v4", "deepseek-r1"])}),
    (r"^DeepseekV3", {"vllm": (["deepseek_v3"], ["deepseek_r1"]), "sglang": (["deepseekv3"], ["deepseek-r1"])}),
    (r"^Glm4Moe|^Glm4", {"vllm": (["glm45"], ["glm45"]), "sglang": (["glm45"], ["glm45"])}),
    (r"^Gemma4", {"vllm": (["gemma4"], ["gemma4"]), "sglang": (["gemma4"], ["gemma4"])}),
    (r"^Kimi", {"vllm": (["kimi_k2"], ["kimi_k2"]), "sglang": (["kimi_k2"], ["kimi_k2"])}),
    (r"^Mistral|^Ministral", {"vllm": (["mistral"], ["mistral"]), "sglang": (["mistral"], ["mistral"])}),
    (r"^Llama", {"vllm": (["llama3_json"], []), "sglang": (["llama3"], [])}),
]


def parser_args(server, architecture):
    """The command-line flags for a model's tool calls and reasoning in this engine, as far as it
    knows them: [] when it knows no parser for the family."""
    have = known(server)
    for pattern, by_engine in PARSERS:
        if re.search(pattern, architecture or ""):
            tools, reasoning = by_engine.get(server, ([], []))
            tool = next((t for t in tools if t in have["tools"]), None)
            think = next((r for r in reasoning if r in have["reasoning"]), None)
            args = []
            if tool:
                args += (["--enable-auto-tool-choice"] if server == "vllm" else []) + ["--tool-call-parser", tool]
            if think:
                args += ["--reasoning-parser", think]
            return args
    return []


def _weights(info):
    return sum(s.get("size") or 0 for s in info.get("siblings") or []
               if (s.get("rfilename") or "").endswith((".safetensors", ".bin")) and "/" not in (s.get("rfilename") or ""))


def has_weights(info):
    names = [s.get("rfilename") or "" for s in info.get("siblings") or []]
    return "config.json" in names and any(n.endswith(".safetensors") for n in names)


def source_of(model_id, info):
    """(repository, its info) whose own files vLLM and SGLang would load: this one when it publishes
    safetensors, else the model it was made from; (None, None) when neither does."""
    if has_weights(info):
        return model_id, info
    base = hf_client._base_model_id(info)
    if base and base != model_id:
        other = hf_client.get_model_info(base)
        if "error" not in other and has_weights(other):
            return base, other
    return None, None


def search_quantized(model_id, limit=4):
    """4-bit safetensors versions of a model (AWQ, GPTQ): the ones that fit where the original does not."""
    name = model_id.split("/")[-1]
    try:
        response = requests.get(f"{hf_client.HF_API_BASE}/models", headers=hf_client._headers(),
                                timeout=hf_client.TIMEOUT,
                                params={"search": name, "filter": "safetensors", "sort": "downloads",
                                        "direction": -1, "limit": 30})
        response.raise_for_status()
        found = response.json()
    except (requests.RequestException, ValueError):
        return []
    four_bit = re.compile(r"awq|gptq|int4|w4a16|4bit|4-bit", re.I)
    return [{"id": m["id"], "downloads": m.get("downloads") or 0} for m in found
            if isinstance(m, dict) and m.get("id") and m["id"] != model_id and four_bit.search(m["id"])
            and not re.search(r"gguf|mlx|nvfp4|mxfp4", m["id"], re.I)][:limit]


def assess(model_id, info, system):
    """{"source", "weights_bytes", "architecture", "quantization", "engines": {server: {...}},
    "alternatives"} - whether each of vLLM and SGLang can serve the model here, and why not."""
    repo, source = source_of(model_id, info)
    engines = {}
    if repo is None:
        why = ("this repository and the model it was made from publish no safetensors weights, the only files "
               "vLLM and SGLang load")
        return {"source": None, "engines": {s: {"supported": False, "why": why, "label": ENGINES[s],
                                                "installed": known(s)["installed"]} for s in ENGINES},
                "alternatives": []}
    config = hf_client.get_model_config(repo)
    config = {} if "error" in config else config
    architecture = (config.get("architectures") or (source.get("config") or {}).get("architectures") or [""])[0]
    quantization = config.get("quantization_config") or {}
    quant = (quantization.get("quant_method") or "").lower()
    # FP4 can be named anywhere in it: NVIDIA's ModelOpt says "modelopt" and puts NVFP4 in quant_algo
    fp4 = any(q in json.dumps(quantization).lower() for q in FP4) or bool(re.search(r"nvfp4|mxfp4", repo, re.I))
    weights = _weights(source)
    vram = system.get("total_vram") or 0
    gpus = max(1, len(system.get("gpu_info") or []))
    alternatives = []
    for server, label in ENGINES.items():
        have = known(server)
        if not have["installed"]:
            supported, why = False, f"{label} is not installed on this machine"
        elif not architecture:
            supported, why = False, "its config.json names no architecture"
        elif architecture not in have["architectures"]:
            supported, why = False, f"the installed {label} does not know the {architecture} architecture"
        elif fp4:
            supported, why = False, "its FP4 weights are made for newer GPUs than these RTX 3090s"
        elif vram and weights > vram * WEIGHTS_SHARE:
            # In the units the GPUs give their memory in, as the rest of the page says it
            supported, why = False, (f"its weights ({weights / 1024**3:.0f} GB) do not fit in {vram / 1024**3:.0f} GB "
                                     f"of VRAM with room for the cache")
        else:
            supported, why = True, ""
        engines[server] = {"supported": supported, "why": why, "label": label, "installed": have["installed"],
                           "parsers": parser_args(server, architecture) if supported else []}
    if not any(e["supported"] for e in engines.values()) and weights > vram * WEIGHTS_SHARE:
        alternatives = search_quantized(repo)
    return {"source": repo, "weights_bytes": weights, "architecture": architecture, "quantization": quant,
            "gpus": gpus, "engines": engines, "alternatives": alternatives}


def download_files(info):
    """What a model needs from its repository: its weights, config and tokenizer files at the top of
    the repository (not README, pictures, other formats or subfolders of originals)."""
    out = []
    for s in info.get("siblings") or []:
        name = s.get("rfilename") or ""
        if "/" in name or name.startswith(".") or name.lower().endswith((".md", ".png", ".jpg", ".jpeg", ".gif",
                                                                          ".gguf", ".pdf", ".pt", ".pth", ".bin.index.json")):
            continue
        if name.endswith(".bin") and any((x.get("rfilename") or "").endswith(".safetensors") for x in info["siblings"]):
            continue                                         # the same weights twice, in the older format
        out.append(name)
    return out


def profile(server, name, folder, config, system):
    """The engine profile for a model downloaded for vLLM or SGLang: both GPUs, a window as long as the
    model's own up to 256K, and what made each engine start here (config/engines.yaml, qwen3.6-vllm)."""
    text = config.get("text_config") or {}
    window = int(text.get("max_position_embeddings") or config.get("max_position_embeddings") or 32768)
    architecture = (config.get("architectures") or [""])[0]
    gpus = max(1, len(system.get("gpu_info") or []))
    entry = {"name": name, "server": server, "binary": binary(server), "model_path": folder,
             "ctx_size": min(window, 262144), "tensor_parallel": gpus, "parallel": 4,
             "vision": bool(config.get("vision_config")), "extra_args": parser_args(server, architecture)}
    if server == "vllm":
        entry["gpu_memory"] = 0.90
        entry["env"] = {"VLLM_USE_FLASHINFER_SAMPLER": "0"}
        entry["extra_args"] = entry["extra_args"] + ["--enable-prefix-caching"]
    else:
        venv = os.path.dirname(os.path.dirname(binary(server)))
        site = _site_packages(server) or venv
        entry["gpu_memory"] = 0.88
        entry["env"] = {"CUDA_HOME": os.path.join(site, "nvidia", "cu13"),
                        "PATH": os.path.join(venv, "bin") + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        entry["extra_args"] = entry["extra_args"] + ["--attention-backend", "triton", "--sampling-backend", "pytorch",
                                                     "--dtype", "bfloat16"]
    return entry
