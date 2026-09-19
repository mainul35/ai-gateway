"""Image generation and editing through ComfyUI (Flux Dev on the homelab).

A new image is text-to-image; an edit is image-to-image: the source picture is encoded and re-rendered
towards the prompt, with `strength` deciding how far it may move from the original.

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

log = logging.getLogger("tools.images")

JOB_TIMEOUT = 900
SIZES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "wide": (1344, 768)}

# One job at a time: two would fight over the GPU, and ComfyUI runs them one by one anyway
_job_lock = asyncio.Lock()


class ImageError(Exception):
    pass


def is_available():
    return settings.feature_enabled("image_generation") and bool(settings.comfyui_url())


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


async def generate(prompt, size="square", source=None, strength=0.75, on_progress=None):
    """Runs one job and returns (png_bytes, seed).

    source is (bytes, mime_type) for an edit. on_progress(stage, fraction) is awaited as the job runs.
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
            source_name = None
            if source:
                data, mime = source
                extension = {"image/jpeg": "jpg", "image/webp": "webp"}.get(mime, "png")
                try:
                    uploaded = await client.post(f"{base}/upload/image",
                                                 files={"image": (f"gateway-{client_id}.{extension}", data, mime)},
                                                 data={"overwrite": "true"})
                    uploaded.raise_for_status()
                    source_name = uploaded.json()["name"]
                except (httpx.HTTPError, ValueError, KeyError) as e:
                    raise ImageError(f"Could not reach the image server ({e.__class__.__name__})") from e

            await report("Freeing GPU memory from the language models")
            await _free_vram_for_images()

            workflow = _workflow(prompt, width, height, seed, source_name, strength)
            ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + f"/ws?clientId={client_id}"
            try:
                async with websockets.connect(ws_url, max_size=None, open_timeout=10) as ws:
                    queued = await client.post(f"{base}/prompt", json={"prompt": workflow, "client_id": client_id})
                    if queued.status_code >= 400:
                        raise ImageError(f"The image server rejected the job: {queued.text[:300]}")
                    prompt_id = queued.json()["prompt_id"]
                    await report("Loading the image model")
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
                view = await client.get(f"{base}/view", params={
                    "filename": image["filename"], "subfolder": image.get("subfolder", ""), "type": image.get("type", "output")})
                view.raise_for_status()
                return view.content, seed
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
