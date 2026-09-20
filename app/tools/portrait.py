"""Putting a passport photograph on the background colour the form asks for.

The person is cut out with a segmentation model and placed on a flat colour. Nothing about the person
is changed - a passport photograph that has been retouched is a rejected passport photograph - so the
work is all in the edge: hair against a plain wall is where a cut-out either convinces or does not.
"""
import asyncio
import io
import logging
import re

import numpy as np
from PIL import Image, ImageFilter

from app import settings

log = logging.getLogger("tools.portrait")

MODEL_SIDE = 1024        # what BiRefNet was trained at; u2net runs at 320 and is resized here
MAX_SIDE = 4000

# The colours passport and visa forms actually ask for, and a few obvious ones besides
COLOURS = {
    "white": (255, 255, 255), "off-white": (247, 247, 242), "cream": (252, 248, 238),
    "light blue": (197, 222, 242), "blue": (110, 160, 215), "sky blue": (160, 205, 240),
    "dark blue": (40, 72, 130), "navy": (28, 48, 92), "red": (200, 42, 42), "grey": (200, 200, 200),
    "gray": (200, 200, 200), "light grey": (223, 223, 223), "light gray": (223, 223, 223),
    "dark grey": (110, 110, 110), "dark gray": (110, 110, 110), "green": (120, 190, 130),
    "black": (18, 18, 18), "beige": (238, 228, 208), "pink": (244, 200, 208),
}
NAMED = re.compile("|".join(sorted((re.escape(name) for name in COLOURS), key=len, reverse=True)), re.I)
HEX = re.compile(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", re.I)

_session = None
_unavailable = False


class PortraitError(Exception):
    pass


def colour_from(text, fallback=(255, 255, 255)):
    """The colour asked for, as RGB. Words first, then a hex code, then plain white."""
    found = HEX.search(text or "")
    if found:
        digits = found.group(1)
        if len(digits) == 3:
            digits = "".join(c * 2 for c in digits)
        return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))
    named = NAMED.search(text or "")
    return COLOURS[named.group(0).lower()] if named else fallback


def colour_name(rgb):
    for name, value in COLOURS.items():
        if value == tuple(rgb):
            return name
    return "#%02x%02x%02x" % tuple(rgb)


def is_available():
    global _session, _unavailable
    if _session is not None:
        return True
    if _unavailable:
        return False
    try:
        import onnxruntime
        _session = onnxruntime.InferenceSession(settings.cutout_model_path(),
                                                providers=["CPUExecutionProvider"])
    except Exception as e:
        log.info("Cut-out model unavailable (%s); background colour is off", e)
        _unavailable = True
        return False
    return True


def _matte(image):
    """How much of each pixel is the person, from 0 to 1, at the size of the picture."""
    shape = _session.get_inputs()[0].shape
    side = shape[2] if isinstance(shape[2], int) else MODEL_SIDE
    small = np.asarray(image.convert("RGB").resize((side, side), Image.BILINEAR), dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    deviation = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    batch = ((small - mean) / deviation).transpose(2, 0, 1)[None]
    predicted = _session.run(None, {_session.get_inputs()[0].name: batch})[0]
    mask = np.asarray(predicted).squeeze()
    if mask.ndim == 3:          # some of these models answer with several maps; the first is the subject
        mask = mask[0]
    # Some exports end in a sigmoid and answer between 0 and 1; others hand back raw scores. Stretching
    # raw scores from their own smallest to their own largest is what a single bright outlier needs to
    # turn a clean cut-out into fog, so scores get the sigmoid they were missing instead.
    if mask.min() < -0.001 or mask.max() > 1.001:
        mask = 1.0 / (1.0 + np.exp(-np.clip(mask, -30, 30)))
    mask = np.clip(mask, 0.0, 1.0)
    return Image.fromarray((mask * 255).astype(np.uint8), mode="L").resize(image.size, Image.BILINEAR)


def replace_background(png, rgb=(255, 255, 255), soften=1.0):
    """Returns the picture with everything but the person replaced by one flat colour."""
    if not is_available():
        raise PortraitError("Changing the background needs the cut-out model; it is not installed")
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if max(image.size) > MAX_SIDE:
        image.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    matte = _matte(image)
    # A hair's width of softening, so the edge is not a cut line. More than that and the person
    # starts to glow, which is exactly what a passport office looks for.
    if soften > 0:
        matte = matte.filter(ImageFilter.GaussianBlur(max(0.4, soften * min(image.size) / 900)))

    alpha = (np.asarray(matte, dtype=np.float32) / 255.0)[..., None]
    pixels = np.asarray(image, dtype=np.float32)
    background = np.array(rgb, dtype=np.float32)
    # Where the edge is half the person and half the old background, the old background's colour is
    # still in the pixel. Pulling it towards the new colour keeps a white cut-out from wearing a
    # green rim in front of a garden.
    edge = ((alpha > 0.02) & (alpha < 0.98)).squeeze()
    blended = pixels * alpha + background * (1 - alpha)
    if edge.any():
        correction = (pixels[edge] - background) * alpha[edge] + background
        blended[edge] = correction
    out = io.BytesIO()
    Image.fromarray(np.clip(blended, 0, 255).round().astype(np.uint8)).save(out, format="PNG")
    return out.getvalue()


async def replace_background_in_background(png, rgb, soften=1.0):
    """Segmentation is CPU work, so it runs off the event loop."""
    return await asyncio.to_thread(replace_background, png, rgb, soften)
