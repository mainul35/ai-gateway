"""Image generation and editing through ComfyUI on the homelab.

New images come from Flux Dev (text-to-image). Edits use Qwen-Image-Edit, which follows instructions
such as "make it night" or "put the hat from image 2 on the person in image 1" and keeps everything
else as it was. Without the Qwen files, edits fall back to Flux image-to-image: the picture is
re-rendered towards the prompt, with `strength` deciding how far it may move from the original.

The LLMs and Flux cannot share the 24 GB card, so every job first unloads the language models and
afterwards asks ComfyUI to release its VRAM again.
"""
import asyncio
import contextlib
import json
import logging
import random
import uuid

import httpx
import websockets

from app import settings
from app.engine.supervisor import supervisor
from app.tools import lettering, photo

log = logging.getLogger("tools.images")

JOB_TIMEOUT = 900
MAX_EDIT_IMAGES = 3  # Qwen-Image-Edit takes up to three reference pictures
# Tested against the alternatives: asking to "rewrite" or to "remove any other text" either changed the
# wording again or threw away the logo with it. Correcting gently keeps the picture and fixes the words.
FIX_TEXT = ('Correct the lettering so that it reads exactly "{text}", spelled correctly. '
            'Keep everything else in the picture exactly as it is.')
SIZES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "wide": (1344, 768)}

# One job at a time: two would fight over the GPU, and ComfyUI runs them one by one anyway
_job_lock = asyncio.Lock()


class ImageError(Exception):
    pass


def is_available():
    return settings.feature_enabled("image_generation") and bool(settings.comfyui_url())


async def edit_engine():
    """ "qwen" when ComfyUI has the Qwen-Image-Edit model files, otherwise "flux" (image-to-image)."""
    wanted = {"UNETLoader": ("unet_name", settings.edit_model()),
              "CLIPLoader": ("clip_name", settings.edit_text_encoder()),
              "VAELoader": ("vae_name", settings.edit_vae())}
    if not all(name for _, name in wanted.values()):
        return "flux"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            for node, (field, name) in wanted.items():
                info = (await client.get(f"{settings.comfyui_url()}/object_info/{node}")).json()
                if name not in info[node]["input"]["required"][field][0]:
                    return "flux"
    except (httpx.HTTPError, ValueError, KeyError, IndexError):
        return "flux"
    return "qwen"


def _qwen_edit_workflow(prompt, seed, source_names):
    """ComfyUI's own Qwen-Image-Edit-2511 template, flattened: the first image is the one edited."""
    lora = settings.edit_lora()
    nodes = {
        "unet": {"class_type": "UNETLoader", "inputs": {"unet_name": settings.edit_model(), "weight_dtype": "default"}},
        "shift": {"class_type": "ModelSamplingAuraFlow", "inputs": {"model": ["unet", 0], "shift": 3.1}},
        "norm": {"class_type": "CFGNorm", "inputs": {"model": ["shift", 0], "strength": 1.0}},
        "clip": {"class_type": "CLIPLoader", "inputs": {"clip_name": settings.edit_text_encoder(),
                                                         "type": "qwen_image", "device": "default"}},
        "vae": {"class_type": "VAELoader", "inputs": {"vae_name": settings.edit_vae()}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["vae", 0]}},
        "save": {"class_type": "PreviewImage", "inputs": {"images": ["decode", 0]}},
    }
    references = {}
    for index, name in enumerate(source_names[:MAX_EDIT_IMAGES], 1):
        nodes[f"load{index}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
        # The edited picture is scaled to a size the model handles well; references are used as they are
        if index == 1:
            nodes["scale"] = {"class_type": "FluxKontextImageScale", "inputs": {"image": ["load1", 0]}}
            references["image1"] = ["scale", 0]
        else:
            references[f"image{index}"] = [f"load{index}", 0]
    for key, text in (("positive", prompt), ("negative", "")):
        nodes[f"{key}_text"] = {"class_type": "TextEncodeQwenImageEditPlus",
                                "inputs": {"clip": ["clip", 0], "vae": ["vae", 0], "prompt": text, **references}}
        nodes[key] = {"class_type": "FluxKontextMultiReferenceLatentMethod",
                      "inputs": {"conditioning": [f"{key}_text", 0], "reference_latents_method": "index_timestep_zero"}}
    nodes["latent"] = {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["vae", 0]}}
    if lora:
        nodes["lora"] = {"class_type": "LoraLoaderModelOnly",
                         "inputs": {"model": ["norm", 0], "lora_name": lora, "strength_model": 1.0}}
    steps, cfg = (4, 1.0) if lora else (40, 4.0)
    nodes["sampler"] = {"class_type": "KSampler", "inputs": {
        "model": ["lora" if lora else "norm", 0], "positive": ["positive", 0], "negative": ["negative", 0],
        "latent_image": ["latent", 0], "seed": seed, "steps": steps, "cfg": cfg,
        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
    }}
    return nodes


def _workflow(prompt, width, height, seed, source_name=None, strength=0.75):
    steps, guidance = settings.image_steps(), settings.image_guidance()
    nodes = {
        "ckpt": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": settings.image_checkpoint()}},
        "positive": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["ckpt", 1]}},
        "guided": {"class_type": "FluxGuidance", "inputs": {"conditioning": ["positive", 0], "guidance": guidance}},
        # Flux Dev is guidance-distilled: no real negative prompt, and CFG stays at 1
        "negative": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["positive", 0]}},
        "decode": {"class_type": "VAEDecode", "inputs": {"samples": ["sampler", 0], "vae": ["ckpt", 2]}},
        # A preview lands in ComfyUI's temp folder, which it clears itself; the gateway keeps its own copy
        "save": {"class_type": "PreviewImage", "inputs": {"images": ["decode", 0]}},
    }
    if source_name:
        nodes.update({
            "load": {"class_type": "LoadImage", "inputs": {"image": source_name}},
            # About one megapixel, which is what Flux is trained on, whatever the upload size; sides in
            # multiples of 16 as the latent needs (older ComfyUI ignores the extra input)
            "scale": {"class_type": "ImageScaleToTotalPixels",
                      "inputs": {"image": ["load", 0], "upscale_method": "lanczos", "megapixels": 1.0,
                                 "resolution_steps": 16}},
            "latent": {"class_type": "VAEEncode", "inputs": {"pixels": ["scale", 0], "vae": ["ckpt", 2]}},
        })
        denoise = max(0.05, min(1.0, float(strength)))
    else:
        nodes["latent"] = {"class_type": "EmptySD3LatentImage",
                           "inputs": {"width": width, "height": height, "batch_size": 1}}
        denoise = 1.0
    nodes["sampler"] = {"class_type": "KSampler", "inputs": {
        "model": ["ckpt", 0], "positive": ["guided", 0], "negative": ["negative", 0], "latent_image": ["latent", 0],
        "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler", "scheduler": "simple", "denoise": denoise,
    }}
    return nodes


async def _free_vram_for_images():
    """Stops our llama.cpp engines and has Ollama drop its models, so Flux gets the whole card."""
    for model in supervisor.status()["running"]:
        log.info("Stopping %s to make room for image generation", model["name"])
        await supervisor.stop(model["name"])
    await supervisor._unload_ollama_models()


async def _release_comfy(client):
    with contextlib.suppress(httpx.HTTPError):
        await client.post(f"{settings.comfyui_url()}/free", json={"unload_models": True, "free_memory": True})


async def _upload(client, base, client_id, index, data, mime):
    """Into ComfyUI's temp folder, which it clears itself, rather than its input library."""
    extension = {"image/jpeg": "jpg", "image/webp": "webp", "image/gif": "gif"}.get(mime, "png")
    uploaded = await client.post(f"{base}/upload/image", data={"overwrite": "true", "type": "temp"},
                                 files={"image": (f"gateway-{client_id}-{index}.{extension}", data, mime)})
    uploaded.raise_for_status()
    return f"{uploaded.json()['name']} [temp]"


async def _run_job(client, base, client_id, workflow, report, loading_stage):
    """Queues one ComfyUI job, follows it, and returns the picture it produced."""
    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
    async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
        queued = await client.post(f"{base}/prompt", json={"prompt": workflow, "client_id": client_id})
        if queued.status_code >= 400:
            raise ImageError(f"The image server rejected the job: {queued.text[:300]}")
        prompt_id = queued.json()["prompt_id"]
        await report(loading_stage)
        await asyncio.wait_for(_follow_progress(ws, prompt_id, report), JOB_TIMEOUT)

    history = (await client.get(f"{base}/history/{prompt_id}")).json().get(prompt_id) or {}
    status = history.get("status") or {}
    if status.get("status_str") == "error":
        messages = [m[1].get("exception_message") for m in status.get("messages", [])
                    if m[0] == "execution_error"]
        raise ImageError("Image generation failed: " + (messages[0] if messages else "unknown error"))
    images = [img for output in (history.get("outputs") or {}).values() for img in output.get("images", [])]
    if not images:
        raise ImageError("The image server finished without producing an image")
    image = images[0]
    view = await client.get(f"{base}/view", params={"filename": image["filename"],
                                                    "subfolder": image.get("subfolder", ""),
                                                    "type": image.get("type", "output")})
    view.raise_for_status()
    return view.content


async def generate(prompt, size="square", sources=(), strength=0.75, on_progress=None, text=None):
    """Runs the job and returns (png_bytes, seed).

    sources is a list of (bytes, mime_type) for an edit: the picture to change first, then up to two
    references. text is wording that must appear in the picture; Flux spells it wrong far more often
    than not, so the result is passed to the editing model to have the lettering put right.
    on_progress(stage, fraction) is awaited as the job runs.
    """
    if not is_available():
        raise ImageError("Image generation is turned off on this server")
    width, height = SIZES.get(size, SIZES["square"])
    seed = random.randint(1, 2**31 - 1)
    base = settings.comfyui_url()
    client_id = uuid.uuid4().hex

    async def report(stage, fraction=None):
        if on_progress:
            await on_progress(stage, fraction)

    if _job_lock.locked():
        await report("Waiting for another image to finish")
    async with _job_lock:
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                source_names = [await _upload(client, base, client_id, i, data, mime)
                                for i, (data, mime) in enumerate(list(sources)[:MAX_EDIT_IMAGES])]
            except (httpx.HTTPError, ValueError, KeyError) as e:
                raise ImageError(f"Could not reach the image server ({e.__class__.__name__})") from e

            engine = await edit_engine()
            await report("Freeing GPU memory from the language models")
            await _free_vram_for_images()

            try:
                if source_names and engine == "qwen":
                    workflow = _qwen_edit_workflow(prompt, seed, source_names)
                    loading = "Loading the image editing model"
                else:
                    workflow = _workflow(prompt, width, height, seed,
                                         source_names[0] if source_names else None, strength)
                    loading = "Loading the image model"
                png = await _run_job(client, base, client_id, workflow, report, loading)

                if text and not source_names and settings.fix_image_text():
                    async def draw_again():
                        return await _run_job(
                            client, base, client_id,
                            _workflow(prompt, width, height, random.randint(1, 2**31 - 1)),
                            report, "Drawing it again")

                    png = await _with_correct_lettering(client, base, client_id, png, text, report,
                                                        draw_again, engine == "qwen")
                return png, seed
            except asyncio.CancelledError:
                # Nobody is waiting for the picture any more; stop rendering it
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{base}/interrupt")
                raise
            except asyncio.TimeoutError as e:  # before OSError, which it subclasses
                raise ImageError("Image generation took too long") from e
            except (OSError, websockets.WebSocketException, httpx.HTTPError, ValueError, KeyError) as e:
                raise ImageError(f"Could not reach the image server ({e.__class__.__name__})") from e
            finally:
                # Hand the GPU back to the language models
                await _release_comfy(client)


async def clean_up(png, mime, upscale, on_progress=None):
    """Removes noise from a photograph, and enlarges it afterwards when it is to be cropped into."""
    if not is_available():
        raise ImageError("Image work is turned off on this server")
    base = settings.comfyui_url()
    client_id = uuid.uuid4().hex

    async def report(stage, fraction=None):
        if on_progress:
            await on_progress(stage, fraction)

    if _job_lock.locked():
        await report("Waiting for another picture to finish")
    async with _job_lock:
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                name = await _upload(client, base, client_id, "photo", png, mime)
                await report("Freeing GPU memory from the language models")
                await _free_vram_for_images()
                return await _run_job(client, base, client_id,
                                      photo.denoise_workflow(name, upscale), report,
                                      "Removing noise" + (" and enlarging" if upscale else ""))
            except asyncio.CancelledError:
                with contextlib.suppress(httpx.HTTPError):
                    await client.post(f"{base}/interrupt")
                raise
            except asyncio.TimeoutError as e:
                raise ImageError("The picture took too long") from e
            except (OSError, websockets.WebSocketException, httpx.HTTPError, ValueError, KeyError) as e:
                raise ImageError(f"Could not reach the image server ({e.__class__.__name__})") from e
            finally:
                await _release_comfy(client)


async def _with_correct_lettering(client, base, client_id, png, text, report, draw_again, can_correct):
    """Returns the picture whose words came out best.

    Each attempt is read back, because neither model can be trusted with words: Flux misspells them,
    and the editing model, asked to correct a few words, sometimes drops one instead. So the two are
    alternated - correct what is there, then draw the whole thing afresh - and the best is kept. A
    picture that came out right first time is returned untouched.
    """
    await report("Checking the lettering")
    best, best_score = png, lettering.score(png, text)
    log.info("Lettering %r scored %.2f as drawn", text, best_score)
    if best_score >= 1.0:
        return png

    for attempt in range(settings.text_attempts()):
        correcting = can_correct and attempt % 2 == 0
        if correcting:
            await report("Correcting the lettering")
            name = await _upload(client, base, client_id, f"text{attempt}", best, "image/png")
            candidate = await _run_job(
                client, base, client_id,
                _qwen_edit_workflow(FIX_TEXT.format(text=text), random.randint(1, 2**31 - 1), [name]),
                report, "Correcting the lettering")
        else:
            candidate = await draw_again()
        if not candidate:
            continue
        candidate_score = lettering.score(candidate, text)
        log.info("Lettering %r scored %.2f after %s", text, candidate_score,
                 "a correction" if correcting else "drawing again")
        if candidate_score > best_score:
            best, best_score = candidate, candidate_score
        if best_score >= 1.0:
            return best
    return best


async def _follow_progress(ws, prompt_id, report):
    """Relays ComfyUI's websocket events until our prompt has finished."""
    async for raw in ws:
        if isinstance(raw, bytes):
            continue  # live preview frames
        event = json.loads(raw)
        kind, data = event.get("type"), event.get("data") or {}
        if data.get("prompt_id") not in (None, prompt_id):
            continue
        if kind == "progress":
            await report("Generating", data["value"] / max(1, data["max"]))
        elif kind == "executing" and data.get("node") == "decode":
            await report("Decoding", 1.0)
        elif kind == "executing" and data.get("node") is None:
            return  # the whole prompt is done
        elif kind == "execution_error":
            return  # the history lookup reports the message
        elif kind == "execution_success":
            return
