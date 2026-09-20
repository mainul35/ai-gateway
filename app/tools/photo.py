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

def denoise_workflow(image_name, upscale=False, denoise=True, sharpen=False):
    """Noise off first, then enlarge: enlarging noise only makes the noise bigger.

    Sharpening is done by putting the picture through the upscaler and bringing it back down. That
    recovers edges that were softened, where an unsharp mask on its own only draws bright lines
    along them; a light mask afterwards finishes it.
    """
    nodes = {"load": {"class_type": "LoadImage", "inputs": {"image": image_name}}}
    last = "load"
    if denoise:
        nodes["denoiser"] = {"class_type": "UpscaleModelLoader",
                             "inputs": {"model_name": settings.denoise_model()}}
        nodes["denoised"] = {"class_type": "ImageUpscaleWithModel",
                             "inputs": {"upscale_model": ["denoiser", 0], "image": [last, 0]}}
        last = "denoised"
    if upscale or sharpen:
        nodes["upscaler"] = {"class_type": "UpscaleModelLoader",
                             "inputs": {"model_name": settings.upscale_model()}}
        nodes["upscaled"] = {"class_type": "ImageUpscaleWithModel",
                             "inputs": {"upscale_model": ["upscaler", 0], "image": [last, 0]}}
        last = "upscaled"
        if not upscale:
            # Back to the size it came in at: the detail stays, the file does not double
            nodes["back"] = {"class_type": "ImageScaleBy",
                             "inputs": {"image": ["upscaled", 0], "upscale_method": "lanczos",
                                        "scale_by": 0.5}}
            last = "back"
    if sharpen:
        nodes["sharpened"] = {"class_type": "ImageSharpen",
                              "inputs": {"image": [last, 0], "sharpen_radius": 2, "sigma": 0.8,
                                         "alpha": 0.35}}
        last = "sharpened"
    nodes["save"] = {"class_type": "PreviewImage", "inputs": {"images": [last, 0]}}
    return nodes


def dehaze(png, amount=1.0):
    """Takes the veil off a hazy picture, by the dark channel the haze leaves behind.

    Haze adds the same pale light everywhere, which shows up as a floor under the darkest colour in
    each neighbourhood. Estimating that floor and removing it brings back contrast and colour, which
    is what "sharper" usually means for a picture taken through air rather than out of focus.
    """
    image = Image.open(io.BytesIO(png)).convert("RGB")
    pixels = np.asarray(image, dtype=np.float32) / 255.0
    # The darkest channel in a neighbourhood: near zero in a clear picture, lifted by haze
    darkest = pixels.min(axis=2)
    patch = max(7, int(min(image.size) / 60) | 1)
    small = Image.fromarray((darkest * 255).astype(np.uint8), mode="L")
    dark = np.asarray(small.filter(ImageFilter.MinFilter(patch if patch % 2 else patch + 1)),
                      dtype=np.float32) / 255.0
    sky = float(np.percentile(dark, 99.5)) or 1.0
    atmosphere = np.percentile(pixels.reshape(-1, 3)[dark.reshape(-1) >= sky * 0.999], 90, axis=0)
    atmosphere = np.clip(atmosphere, 0.35, 1.0)
    transmission = 1.0 - 0.95 * float(amount) * (dark / max(atmosphere.max(), 1e-3))
    # Smoothed, so the correction follows the scene rather than the patches it was measured in
    transmission = np.asarray(Image.fromarray((np.clip(transmission, 0.1, 1.0) * 255).astype(np.uint8),
                                              mode="L").filter(ImageFilter.GaussianBlur(patch)),
                              dtype=np.float32) / 255.0
    transmission = np.clip(transmission, 0.25, 1.0)[..., None]
    cleared = (pixels - atmosphere) / transmission + atmosphere
    out = io.BytesIO()
    Image.fromarray((np.clip(cleared, 0, 1) * 255).round().astype(np.uint8)).save(out, format="PNG")
    return out.getvalue()


async def dehaze_in_background(png, amount):
    return await asyncio.to_thread(dehaze, png, amount)


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
