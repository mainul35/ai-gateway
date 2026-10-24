"""The Quran recited by a qari, for a verse quoted in a video story.

A verse in a story is never read by the narration voice: it is matched to its sura and ayah in the
Tanzil Uthmani text, and the ayat are played as recited by a qari (video.quran.reciter, Mishary
Alafasy by default), from everyayah.com's verse-by-verse recordings. Both are fetched once, when first
needed, and kept in video.quran.dir: the text (1.4 MB) and each ayah's recording.

Matching is on the letters alone. The same ayah is written with different marks - إِنِّي and إِنِّى,
جَاعِلٞ and جَاعِلٌ, a small high sukun or a plain one - so the marks, the tatweel and the forms of
alef are set aside on both sides before the words are compared.
"""
import asyncio
import logging
import os
import re
import wave

import httpx

from app import settings

log = logging.getLogger("tools.quran")

TEXT_URL = ("https://tanzil.net/pub/download/index.php?marks=true&sajdah=true&rub=false&tatweel=true"
            "&quranType=uthmani&outType=txt-2&agree=true")
AUDIO_URL = "https://everyayah.com/data/{reciter}/{sura:03d}{ayah:03d}.mp3"
MIN_WORDS = 3             # fewer words than this is a phrase, not a verse to recite

_words = None             # every word of the Quran, in order: [(word, sura, ayah)]
_starts = None            # word -> the positions it is found at


def _folder():
    return os.path.expanduser(settings.get("video.quran.dir", "VIDEO_QURAN_DIR") or "~/quran")


def _reciter():
    return settings.get("video.quran.reciter", "VIDEO_QURAN_RECITER") or "Alafasy_128kbps"


def letters(text):
    """Arabic reduced to its letters, for comparing one way of writing an ayah with another."""
    text = re.sub("[ً-ٰٟۖ-ۭـ]", "", text)
    text = re.sub("[ٱآأإ]", "ا", text).replace("ى", "ي")
    return re.sub("[^ء-ي\\s]", "", text).split()


def _load():
    global _words, _starts
    if _words is not None:
        return _words
    path = os.path.join(_folder(), "quran-uthmani.txt")
    if not os.path.isfile(path):
        os.makedirs(_folder(), exist_ok=True)
        response = httpx.get(TEXT_URL, timeout=60, follow_redirects=True)
        response.raise_for_status()
        with open(path, "wb") as f:
            f.write(response.content)
    words, starts = [], {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("|")
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            for word in letters(parts[2]):
                starts.setdefault(word, []).append(len(words))
                words.append((word, int(parts[0]), int(parts[1])))
    _words, _starts = words, starts
    return words


def find(arabic):
    """[(sura, ayah)] the quoted words are from, in order, or [] when they are not a verse (or not
    found word for word)."""
    wanted = letters(arabic)
    if len(wanted) < MIN_WORDS:
        return []
    try:
        words = _load()
    except (OSError, httpx.HTTPError) as e:
        log.warning("The Quran text could not be loaded: %s", e)
        return []
    for at in _starts.get(wanted[0], []):
        if [w for w, _, _ in words[at:at + len(wanted)]] == wanted:
            ayat = []
            for _, sura, ayah in words[at:at + len(wanted)]:
                if (sura, ayah) not in ayat:
                    ayat.append((sura, ayah))
            return ayat
    return []


async def _recording(sura, ayah):
    """The ayah's recording, fetched once and kept."""
    folder = os.path.join(_folder(), "audio", _reciter())
    path = os.path.join(folder, f"{sura:03d}{ayah:03d}.mp3")
    if not os.path.isfile(path):
        os.makedirs(folder, exist_ok=True)
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            response = await client.get(AUDIO_URL.format(reciter=_reciter(), sura=sura, ayah=ayah))
            response.raise_for_status()
        with open(path + ".part", "wb") as f:
            f.write(response.content)
        os.replace(path + ".part", path)
    return path


async def recite(ayat, out, ffmpeg):
    """`out` (a WAV) with the ayat recited one after another; its length in seconds, or None when a
    recording could not be had - the verse is then shown without it."""
    try:
        paths = [await _recording(sura, ayah) for sura, ayah in ayat]
    except (OSError, httpx.HTTPError) as e:
        log.warning("Recitation of %s could not be fetched: %s", ayat, e)
        return None
    inputs = sum((["-i", p] for p in paths), [])
    # The ayat one after another, brought to the narration's loudness
    joined = ("".join(f"[{n}:a]" for n in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1,"
              "loudnorm=I=-18:TP=-2:LRA=11[a]")
    process = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", *inputs, "-filter_complex", joined, "-map", "[a]", "-ar", "24000", "-ac", "1", out,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await process.communicate()
    if process.returncode != 0:
        log.warning("Recitation of %s could not be joined: %s", ayat, err.decode(errors="replace")[-600:])
        return None
    with wave.open(out, "rb") as w:
        return w.getnframes() / w.getframerate()
