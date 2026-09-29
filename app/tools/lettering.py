"""Reads the words in a generated picture, so the gateway can tell whether they came out right.

Flux spells badly and the editing model spells well but not always ("JDK 27" came back once as
"JDC 27"). Neither can be trusted blindly, so the picture is read back: a few hundred milliseconds on
the CPU decides whether the lettering needs correcting, and whether the correction worked.
"""
import asyncio
import difflib
import io
import logging
import re

log = logging.getLogger("tools.lettering")

_reader = None
_unavailable = False


def is_available():
    global _reader, _unavailable
    if _reader is not None:
        return True
    if _unavailable:
        return False
    try:
        from rapidocr_onnxruntime import RapidOCR
        _reader = RapidOCR()
    except Exception as e:  # the feature is optional; without it the lettering is simply not checked
        log.info("Lettering cannot be checked (%s); install rapidocr-onnxruntime to enable it", e)
        _unavailable = True
        return False
    return True


def read(png):
    """Every run of text the picture contains, in reading order."""
    if not is_available():
        return []
    try:
        result, _ = _reader(io.BytesIO(png).getvalue())
    except Exception as e:
        log.info("Could not read the picture: %s", e)
        return []
    return [str(line[1]) for line in (result or []) if len(line) > 1]


def _simplify(text):
    """Case, spacing and punctuation are the picture's business; the letters are what must match."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(text):
    return [_simplify(word) for word in re.split(r"\s+", text or "") if _simplify(word)]


def score(png, wanted, found=None):
    """How well the picture's lettering matches, from 0 to 1. Exactly right is 1.

    Right means every word there, in order, spelled correctly and written once. A poster that says
    "Happy New Year 2027" with "NEW YEAR" repeated underneath is not what was asked for either.
    """
    target = _simplify(wanted)
    if not target:
        return 1.0
    found = read(png) if found is None else found
    if not found:
        return 0.0
    whole = _simplify(" ".join(found))
    wanted_words, found_words = _words(wanted), _words(" ".join(found))
    # Every word present and in order counts as spelled right: OCR splits lines unpredictably
    spelled = target in whole or _in_order(wanted_words, found_words)
    if not spelled:
        # Not right, so report the closest run: it tells a near miss from a wholly wrong picture
        return max(difflib.SequenceMatcher(None, target, _simplify(line)).ratio() for line in found)
    if any(found_words.count(word) > wanted_words.count(word) for word in set(wanted_words)):
        return 0.9   # said more than once; readable, but not what was asked for
    return 1.0


def _in_order(wanted_words, found_words):
    remaining = list(found_words)
    for word in wanted_words:
        while remaining and remaining[0] != word:
            remaining.pop(0)
        if not remaining:
            return False
        remaining.pop(0)
    return True


def is_correct(png, wanted):
    return score(png, wanted) >= 1.0


async def score_in_background(png, wanted):
    """Reading a picture takes a fifth of a second of CPU, which the event loop should not spend."""
    return await asyncio.to_thread(score, png, wanted)
