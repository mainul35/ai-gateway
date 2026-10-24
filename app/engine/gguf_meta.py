"""What a GGUF file says about itself, read from its header on disk: the keys, not the weights.

Used to decide things about a model before it is started rather than by trying it: whether it was
trained with multi-token prediction layers (MTP, `<arch>.nextn_predict_layers`), which llama.cpp can
use to draft tokens ahead and check them in one step - 1.7-3x faster writing on code and tool calls,
the same output (measured 2026-10-24). Most conversions strip those layers, so it is per file.
"""
import logging
import os
import struct
import threading

log = logging.getLogger("engine")

_FIXED = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
READ_LIMIT = 64 * 1024**2      # keys come first; a vocabulary can be tens of MB, the weights are not read
_cache = {}
_lock = threading.Lock()


def _parse(data):
    """{key: value} of a GGUF header; arrays longer than 64 items are counted, not kept."""
    pos = 0

    def take(n):
        nonlocal pos
        if pos + n > len(data):
            raise ValueError("header longer than what was read")
        pos += n
        return data[pos - n:pos]

    def u32():
        return struct.unpack("<I", take(4))[0]

    def u64():
        return struct.unpack("<Q", take(8))[0]

    def value(kind):
        if kind == 8:
            return take(u64()).decode("utf-8", errors="replace")
        if kind == 9:
            inner, count = u32(), u64()
            if inner in _FIXED and count > 64:
                take(struct.calcsize(_FIXED[inner]) * count)
                return count
            items = [value(inner) for _ in range(count)]
            return items if count <= 64 else count
        if kind not in _FIXED:
            raise ValueError(f"unknown GGUF value type {kind}")
        return struct.unpack(_FIXED[kind], take(struct.calcsize(_FIXED[kind])))[0]

    if take(4) != b"GGUF":
        raise ValueError("not a GGUF file")
    u32()                       # version
    u64()                       # tensor count
    keys = {}
    for _ in range(u64()):
        name = take(u64()).decode("utf-8", errors="replace")
        keys[name] = value(u32())
    return keys


def keys(path):
    """The header's keys, kept per file until it changes; {} when it cannot be read."""
    try:
        stamp = (path, os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return {}
    with _lock:
        if stamp in _cache:
            return _cache[stamp]
    try:
        with open(path, "rb") as f:
            found = _parse(f.read(READ_LIMIT))
    except (OSError, ValueError, struct.error) as e:
        log.warning("could not read the GGUF header of %s: %s", path, e)
        found = {}
    with _lock:
        _cache[stamp] = found
    return found


def mtp_layers(path):
    """How many multi-token prediction layers the model carries (0 for none)."""
    for key, value in keys(path).items():
        if key.endswith(".nextn_predict_layers") and isinstance(value, int):
            return value
    return 0
