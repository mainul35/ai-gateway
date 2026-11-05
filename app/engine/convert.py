"""Making a GGUF of a model that publishes none, for llama.cpp: its own weights (safetensors) through
llama.cpp's converter (convert_hf_to_gguf.py), then llama-quantize down to the quantization chosen.

The converter is llama.cpp's source at the version of the newest build here (~/llama.cpp/source,
engine.convert.source), run with a Python that has torch, transformers and safetensors - vLLM's
(engine.convert.python) - and the gguf package from that same source. Nothing of it is installed.

    survey(model_id, info)                  what the model is and whether the converter knows it
    check(found, quantization, system)      whether that quantization can be made and run here
    plan(model_id, info, quantization, system)  the two together
    rows(model_id, quantization, folder, name, cancellation)  the work, as progress events

The weights are downloaded first (resumable) when the disk holds them and the converted file
together; otherwise the converter reads them from Hugging Face as it goes (--remote). The weights
and the full-precision file are deleted once the quantized one is made.
"""
import glob
import logging
import os
import re
import shutil
import subprocess
import threading

from app import fitting, settings
from app.engine import builds as engine_builds, gguf_meta, serving
from utils import hf_client, hf_download

log = logging.getLogger("convert")

DEFAULT_SOURCE = "~/llama.cpp/source"
DEFAULT_PYTHON = "~/inference/vllm-venv/bin/python"
DISK_SHARE = 0.95            # of the free disk a conversion may plan to fill
# Written straight by the converter; anything else goes through a full-precision file first
DIRECT = {"q8_0": "q8_0"}

_supported = {}
_supported_lock = threading.Lock()


def source():
    return os.path.expanduser(settings.get("engine.convert.source", "ENGINE_CONVERT_SOURCE") or DEFAULT_SOURCE)


def python():
    return os.path.expanduser(settings.get("engine.convert.python", "ENGINE_CONVERT_PYTHON") or DEFAULT_PYTHON)


def _script():
    return os.path.join(source(), "convert_hf_to_gguf.py")


def _env():
    return {**os.environ, "PYTHONPATH": os.path.join(source(), "gguf-py"), "PYTHONUNBUFFERED": "1"}


def installed():
    return os.path.isfile(_script()) and os.path.isfile(python())


def supported():
    """{"text": {architecture}, "mmproj": {architecture}}: what the converter says it knows, asked once
    per version of it. Blocking."""
    if not installed():
        return {"text": set(), "mmproj": set()}
    stamp = os.path.getmtime(_script())
    with _supported_lock:
        if _supported.get("stamp") == stamp:
            return _supported["answer"]
        try:
            # Printed through logging, so on stderr
            out = subprocess.run([python(), _script(), "--print-supported-models"], stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, timeout=120, env=_env()).stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("could not ask the converter what it supports: %s", e)
            return {"text": set(), "mmproj": set()}
        answer, section = {"text": set(), "mmproj": set()}, "text"
        for line in out.splitlines():
            if line.strip().upper().startswith("MMPROJ"):
                section = "mmproj"
            elif line.strip().upper().startswith("TEXT"):
                section = "text"
            elif line.strip().startswith("- "):
                answer[section].add(line.strip()[2:].strip())
        _supported.update(stamp=stamp, answer=answer)
        return answer


def _quantize_binary():
    """llama-quantize of the newest llama.cpp release here: the one most likely to know a new model."""
    for build in engine_builds.builds():
        candidate = os.path.join(os.path.dirname(build["binary"]), "llama-quantize")
        if "/ollama/" not in build["binary"] and os.path.isfile(candidate):
            yield build, candidate


def survey(model_id, info):
    """{"ok", "why", "source", "architecture", "weights_bytes", "params", "kv", "mmproj"}: the model's own
    files (here, or in the repository it was made from) and whether the converter knows it. Blocking."""
    if not installed():
        return {"ok": False, "why": "llama.cpp's converter is not set up here "
                                    f"({_script()} and {python()})"}
    repo, own = serving.source_of(model_id, info)
    if repo is None:
        return {"ok": False, "why": "neither it nor the model it was made from publishes safetensors, "
                                    "which the converter reads"}
    config = hf_client.get_model_config(repo)
    config = {} if "error" in config else config
    architecture = ((config.get("architectures") or (own.get("config") or {}).get("architectures") or [""])[0])
    known = supported()
    if not architecture:
        return {"ok": False, "why": "its config.json names no architecture", "source": repo}
    if architecture not in known["text"]:
        return {"ok": False, "why": f"llama.cpp's converter here does not know {architecture}", "source": repo,
                "architecture": architecture}
    weights = sum(s.get("size") or 0 for s in own.get("siblings") or []
                  if (s.get("rfilename") or "").endswith(".safetensors"))
    params = hf_client.get_parameter_count(own, hf_client.get_model_sizes(own))[0] or weights // 2
    kv = fitting.kv_cache(hf_client.get_model_architecture(repo, own))["kv_cache_recommended_bytes"]
    return {"ok": True, "why": "", "source": repo, "architecture": architecture, "weights_bytes": weights,
            "params": params, "kv": kv, "context": int(config.get("max_position_embeddings") or 0),
            "mmproj": architecture in known["mmproj"] and bool(config.get("vision_config"))}


def check(found, quantization, system):
    """survey()'s answer for one quantization: room on the disk for the work, and a result that fits
    this machine's memory. Adds "output_bytes" and "remote" (the weights read as the converter goes)."""
    level = fitting.QUANTIZATION_LEVELS.get(quantization)
    if not found["ok"] or not level:
        return found | ({} if level else {"ok": False, "why": f"unknown quantization {quantization}"})
    params, weights = found["params"], found["weights_bytes"]
    output = int(params * level["bpw"] / 8)
    middle = 0 if quantization in DIRECT else params * 2      # the BF16 file quantized from
    free = shutil.disk_usage(os.path.expanduser("~")).free * DISK_SHARE
    local_peak = max(weights + (middle or output), middle + output)
    remote_peak = middle + output
    answer = found | {"output_bytes": output}
    if remote_peak > free:
        return answer | {"ok": False, "why": f"it needs {remote_peak / 1e9:.0f} GB of disk to convert, "
                                             f"and {free / 1e9:.0f} GB is free"}
    if not any(r["quantization"] == quantization for r in fitting.recommend(system, params, found["kv"], {}, {})):
        return answer | {"ok": False, "why": f"its {level['name']} ({output / 1e9:.0f} GB) does not fit in "
                                             f"this machine's memory"}
    return answer | {"remote": local_peak > free}


def plan(model_id, info, quantization, system):
    return check(survey(model_id, info), quantization, system)


_SIZE = re.compile(r"([\d.]+)([kMGT]?)/([\d.]+)([kMGT]?)")
_UNITS = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}
_TENSOR = re.compile(r"\[\s*(\d+)/\s*(\d+)\]")


def _run(command, cancellation, stage, progress, cwd=None, env=None):
    """Runs a step, its output read as progress events; ends it when cancelled. Yields events, and
    finally {"exit": code, "tail": last lines}."""
    process = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding="utf-8", errors="replace", bufsize=1)
    stopper = threading.Thread(target=_stop_when_cancelled, args=(process, cancellation), daemon=True)
    stopper.start()
    tail, line = [], ""
    while True:
        char = process.stdout.read(1)
        if not char:
            break
        if char not in "\r\n":
            line += char
            continue
        if line.strip():
            tail = (tail + [line.strip()])[-20:]
            event = progress(line)
            if event:
                yield {"status": stage, **event}
        line = ""
    process.wait()
    if cancellation.cancelled:
        yield {"cancelled": True}
        return
    yield {"exit": process.returncode, "tail": "\n".join(tail[-6:])}


def _stop_when_cancelled(process, cancellation):
    while process.poll() is None:
        if cancellation.cancelled:
            process.terminate()
            return
        threading.Event().wait(1)


def _written(line):
    """tqdm's "Writing:  45%|####  | 3.20G/7.10G" from the converter: bytes written of all."""
    if "Writing" not in line:
        return None
    match = _SIZE.search(line.split("|")[-1])
    if not match:
        return None
    done, total = (float(match[1]) * _UNITS[match[2]], float(match[3]) * _UNITS[match[4]])
    return {"completed": int(done), "total": int(total)} if total else None


def _quantized(line, size):
    """llama-quantize's "[ 12/ 399] blk.0.attn_q.weight ...": tensors done, as a share of the file."""
    match = _TENSOR.search(line)
    if not match or not int(match[2]):
        return None
    return {"completed": int(size * int(match[1]) / int(match[2])), "total": size}


def rows(model_id, quantization, folder, name, cancellation, system):
    """The conversion, as progress events like a download's, ending {"status": "success", "model_path",
    "mmproj", "architecture", "build"} or {"error"} / {"cancelled"}. Blocking."""
    yield {"status": "reading the repository"}
    info = hf_client.get_model_info(model_id)
    if "error" in info:
        yield {"error": info["error"]}
        return
    answer = plan(model_id, info, quantization, system)
    if not answer["ok"]:
        yield {"error": f"Cannot convert {model_id} here: {answer['why']}"}
        return
    repo, level = answer["source"], fitting.QUANTIZATION_LEVELS[quantization]
    os.makedirs(folder, exist_ok=True)
    weights_folder = os.path.join(folder, "weights")
    if answer["remote"]:
        yield {"status": "the disk cannot hold the weights and the conversion together: "
                         "the converter reads the weights from Hugging Face as it goes"}
        model_arg = [repo, "--remote"]
    else:
        own = info if repo == model_id else hf_client.get_model_info(repo)
        sizes = {s.get("rfilename"): s.get("size") or 0 for s in own.get("siblings", [])}
        for event in hf_download.download(repo, serving.download_files(own), weights_folder, cancellation, sizes):
            if event.get("status") == "success":
                break
            yield event
            if "error" in event or event.get("cancelled"):
                return
        model_arg = [weights_folder]
    token = hf_client._headers().get("Authorization", "")[len("Bearer "):]
    env = _env() | ({"HF_TOKEN": token} if answer["remote"] and token else {})
    outtype = DIRECT.get(quantization, "bf16")
    middle = os.path.join(folder, f"{name}-{outtype.upper()}.gguf")
    final = os.path.join(folder, f"{name}.gguf") if quantization not in DIRECT else middle

    steps = [("converting to GGUF (" + outtype.upper() + ")",
              [python(), _script(), *model_arg, "--outfile", middle, "--outtype", outtype])]
    reader = None
    if answer["mmproj"]:
        reader = os.path.join(folder, f"mmproj-{name}-F16.gguf")
        steps.append(("converting its image reader",
                      [python(), _script(), *model_arg, "--outfile", reader, "--outtype", "f16", "--mmproj"]))
    for stage, command in steps:
        yield {"status": stage}
        result = None
        for event in _run(command, cancellation, stage, _written, cwd=source(), env=env):
            if "exit" in event:
                result = event
            else:
                yield event
                if event.get("cancelled"):
                    return
        if result["exit"]:
            yield {"error": f"The converter failed ({stage}): {result['tail'][-1500:]}"}
            return
    if not answer["remote"]:
        shutil.rmtree(weights_folder, ignore_errors=True)     # the GGUF has everything it had

    gguf_arch = gguf_meta.keys(middle).get("general.architecture") or ""
    able = [b for b in engine_builds.supports(gguf_arch) if b["supported"]] if gguf_arch else []
    if not able:
        yield {"error": f"Converted, but no llama.cpp build here loads {gguf_arch or 'its architecture'}: "
                        "a newer llama.cpp is needed. The file is kept: " + middle}
        return
    build = next((b for b in able if b["default"]), able[0])
    if final != middle:
        quantizer = next((q for b, q in _quantize_binary() if b["binary"] in {a["binary"] for a in able}), None)
        if quantizer is None:
            yield {"error": "No llama-quantize beside a llama.cpp build that knows this model. The full-precision "
                            "file is kept: " + middle}
            return
        stage = f"quantizing to {level['name']}"
        yield {"status": stage}
        result = None
        expected = answer["output_bytes"]
        for event in _run([quantizer, middle, final, level["name"]], cancellation, stage,
                          lambda line: _quantized(line, expected), cwd=folder,
                          env={**os.environ, **next((b["env"] for b in engine_builds.builds()
                                                    if b["binary"] == build["binary"]), {})}):
            if "exit" in event:
                result = event
            else:
                yield event
                if event.get("cancelled"):
                    return
        if result["exit"]:
            yield {"error": f"llama-quantize failed: {result['tail'][-1500:]}"}
            return
        os.remove(middle)
    for leftover in glob.glob(os.path.join(folder, "*.gguf.tmp*")):
        os.remove(leftover)
    log.info("converted %s to %s (%s)", repo, final, level["name"])
    yield {"status": "success", "model_path": final, "mmproj": reader, "build": build,
           "context": answer["context"], "params": answer["params"]}
