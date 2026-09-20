"""Two things done to a photograph, each on its own.

Cleaning up: a restoration model removes sensor noise without inventing detail, and an upscaler is
run after it when the picture is going to be cropped into. What is out of focus stays out of focus -
that is the point. A generative editor would redraw the blur as detail, which is why one is not used.

Background blur: a depth model says how far away each pixel is, and the picture is blurred by that
distance. This is the opposite job, and is never done in the same pass as cleaning up: one asks for
noise to go away, the other asks for blur to appear.
"""
import asyncio
import io
import logging

import numpy as np
from PIL import Image, ImageFilter

from app import settings

log = logging.getLogger("tools.photo")

DEPTH_SIZE = 518          # what Depth Anything V2 was trained at
BLUR_LAYERS = 5           # how many blur strengths are blended by distance
FOCUS_BAND = 0.18         # how much of the depth around the focus point stays sharp
MAX_SIDE = 4096           # a photograph, not a poster

_depth = None
_depth_missing = False


# --- cleaning up: noise out, nothing invented ------------------------------------------

def denoise_workflow(image_name, upscale):
    """Noise off first, then enlarge: enlarging noise only makes the noise bigger."""
    nodes = {
        "load": {"class_type": "LoadImage", "inputs": {"image": image_name}},
        "denoiser": {"class_type": "UpscaleModelLoader",
                     "inputs": {"model_name": settings.denoise_model()}},
        "denoised": {"class_type": "ImageUpscaleWithModel",
                     "inputs": {"upscale_model": ["denoiser", 0], "image": ["load", 0]}},
    }
    last = "denoised"
    if upscale:
        nodes["upscaler"] = {"class_type": "UpscaleModelLoader",
                             "inputs": {"model_name": settings.upscale_model()}}
        nodes["upscaled"] = {"class_type": "ImageUpscaleWithModel",
                             "inputs": {"upscale_model": ["upscaler", 0], "image": ["denoised", 0]}}
        last = "upscaled"
    nodes["save"] = {"class_type": "PreviewImage", "inputs": {"images": [last, 0]}}
    return nodes


# --- background blur: shallower depth of field than the sensor gives --------------------

def is_depth_available():
    global _depth, _depth_missing
    if _depth is not None:
        return True
    if _depth_missing:
        return False
    try:
        import onnxruntime
        _depth = onnxruntime.InferenceSession(settings.depth_model_path(),
                                              providers=["CPUExecutionProvider"])
    except Exception as e:
        log.info("Depth model unavailable (%s); background blur is off", e)
        _depth_missing = True
        return False
    return True


def _depth_map(image):
    """Distance per pixel, 0 near to 1 far, at the size of the picture."""
    size = (DEPTH_SIZE, DEPTH_SIZE)
    small = np.asarray(image.convert("RGB").resize(size, Image.BILINEAR), dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    deviation = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    batch = ((small - mean) / deviation).transpose(2, 0, 1)[None]
    predicted = _depth.run(None, {_depth.get_inputs()[0].name: batch})[0][0]
    # The model returns inverse depth: large is near. Flip it, so 0 is near and 1 is far.
    low, high = float(predicted.min()), float(predicted.max())
    normalised = (predicted - low) / (high - low) if high > low else np.zeros_like(predicted)
    far = 1.0 - normalised
    return np.asarray(Image.fromarray((far * 255).astype(np.uint8)).resize(image.size, Image.BILINEAR),
                      dtype=np.float32) / 255.0


def _focus_distance(image, depth):
    """Where the camera focused, as a distance.

    The nearest thing is not the subject: in a photograph of a bird over water, the water in the
    foreground is nearer. What marks the subject is that it is the part that came out sharp, so the
    sharpest region of the frame is found and its distance is taken as the plane of focus.
    """
    grey = np.asarray(image.convert("L"), dtype=np.float32)
    laplacian = np.abs(-4 * grey[1:-1, 1:-1] + grey[:-2, 1:-1] + grey[2:, 1:-1]
                       + grey[1:-1, :-2] + grey[1:-1, 2:])
    # Averaged over a neighbourhood, so one bright edge does not decide it. As 8-bit, because that
    # is what a box blur takes, and only the ordering of the values matters here.
    magnitude = Image.fromarray(np.clip(laplacian, 0, 255).astype(np.uint8), mode="L")
    spread = np.asarray(magnitude.filter(ImageFilter.BoxBlur(12)), dtype=np.float32)
    sharpest = spread >= np.percentile(spread, 96)
    if not sharpest.any():
        return 0.0
    return float(np.median(depth[1:-1, 1:-1][sharpest]))


def background_blur(png, strength=1.0, focus=None):
    """Blurs by distance: whatever is at the focus distance stays sharp, the rest softens.

    strength scales the widest blur. focus (0 near, 1 far) says which distance is in focus; left
    unset, it is taken from wherever the photograph is already sharp, which is what the camera
    focused on.
    """
    if not is_depth_available():
        raise PhotoError("Background blur needs the depth model; it is not installed")
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    depth = _depth_map(image)
    if focus is None:
        focus = _focus_distance(image, depth)
        log.info("Focus distance taken from the sharpest part of the picture: %.2f", focus)
    distance = np.abs(depth - float(focus))
    if distance.max() > 0:
        distance = distance / distance.max()
    # A lens has a plane of focus with depth on either side of it, not a single sharp distance.
    # Without this band even the subject picked up a little blur, which is what softened the bird.
    distance = np.clip((distance - FOCUS_BAND) / (1 - FOCUS_BAND), 0.0, 1.0)

    # A real lens blurs more the further a thing is from the plane of focus. Several blurred copies
    # blended by distance approximate that, and blend smoothly where one layer meets the next.
    widest = max(1.0, (max(image.size) / 500) * 6 * float(strength))
    result = np.asarray(image, dtype=np.float32)
    for layer in range(1, BLUR_LAYERS + 1):
        near, far = (layer - 1) / BLUR_LAYERS, layer / BLUR_LAYERS
        blurred = np.asarray(image.filter(ImageFilter.GaussianBlur(widest * far)), dtype=np.float32)
        weight = np.clip((distance - near) / (far - near), 0.0, 1.0)[..., None]
        result = result * (1 - weight) + blurred * weight
    out = io.BytesIO()
    Image.fromarray(result.round().astype(np.uint8)).save(out, format="PNG")
    return out.getvalue()


class PhotoError(Exception):
    pass


async def blur_in_background(png, strength, focus):
    """The blur is numpy work, so it runs off the event loop."""
    return await asyncio.to_thread(background_blur, png, strength, focus)
