"""Subtitles for a YouTube video, decoded from its own sound rather than taken from YouTube.

The audio track alone is downloaded (yt-dlp), and Whisper large-v3 transcribes it on the GPU
(faster-whisper). YouTube's own captions are not used, on purpose: they are missing on many videos and
machine-made on most of the rest, and transcribing every video the same way gives the same quality
every time. Whisper can also translate as it goes, which is how "in English" is offered for a video
in another language.

Nothing here needs a system ffmpeg: yt-dlp is asked for the audio stream as it is published, with no
conversion, and faster-whisper decodes it with the libraries PyAV ships.
"""
import asyncio
import contextlib
import ctypes
import glob
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time

from app import settings

log = logging.getLogger("tools.subtitles")

MODEL = "large-v3"
def max_seconds():
    """The longest video transcribed: limits.subtitles.max_minutes (default 120). Every minute of
    sound holds the whole GPU, and one long video after another kept it from everybody else."""
    try:
        return int(float(settings.get("limits.subtitles.max_minutes", "LIMITS_SUBTITLES_MAX_MINUTES") or 120) * 60)
    except ValueError:
        return 120 * 60

YOUTUBE = re.compile(
    r"https?://(?:www\.|m\.|music\.)?(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=|shorts/|live/|embed/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})")


class SubtitleError(Exception):
    pass


def video_id(text):
    """The YouTube video a message links to, or None."""
    found = YOUTUBE.search(text or "")
    return found.group(1) if found else None


def is_available():
    try:
        import faster_whisper  # noqa: F401
        import yt_dlp  # noqa: F401
    except ImportError:
        return False
    return settings.get_bool("subtitles.enabled", "SUBTITLES_ENABLED", True)


def _model_dir():
    """models/whisper in the gateway's folder, beside the other models: about 3 GB, downloaded once."""
    import app
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(app.__file__))), "models", "whisper")


_cuda_ready = False


def _load_cuda_libraries():
    """cuBLAS 12 comes from the nvidia-cublas-cu12 wheel, which the loader does not search by itself.

    The host has cuDNN 9 installed system-wide but not cuBLAS 12, and CTranslate2 opens it by name at
    the first transcription; loaded here first, under its own name, it is found.
    """
    global _cuda_ready
    if _cuda_ready:
        return
    pattern = os.path.join(sys.prefix, "lib", "python3*", "site-packages", "nvidia", "cublas", "lib",
                           "libcublas*.so.12")
    for library in sorted(glob.glob(pattern)):
        with contextlib.suppress(OSError):
            ctypes.CDLL(library, mode=ctypes.RTLD_GLOBAL)
    _cuda_ready = True


def _download(video, work, report):
    """The audio stream only, as published: no conversion, so no ffmpeg."""
    import yt_dlp

    def hook(status):
        if status.get("status") == "downloading" and status.get("total_bytes"):
            report("Downloading the sound", status["downloaded_bytes"] / status["total_bytes"])

    options = {"format": "bestaudio/best", "outtmpl": os.path.join(work, "audio.%(ext)s"),
               "quiet": True, "noprogress": True, "noplaylist": True, "progress_hooks": [hook],
               "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {max_seconds()}")}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video}", download=True)
    except yt_dlp.utils.DownloadError as e:
        raise SubtitleError(f"YouTube would not give the video: {str(e).replace('ERROR: ', '')[:300]}") from e
    files = glob.glob(os.path.join(work, "audio.*"))
    if not files:
        if (info or {}).get("duration", 0) > max_seconds():
            raise SubtitleError(f"The video is longer than {max_seconds() // 60} minutes, which is the limit here")
        raise SubtitleError("YouTube gave no sound for this video")
    return files[0], {"title": info.get("title") or video, "channel": info.get("uploader") or "",
                      "duration": info.get("duration") or 0, "url": f"https://www.youtube.com/watch?v={video}"}


def _decode(path):
    """The sound as 16 kHz mono floats, which is what Whisper listens to.

    Done here rather than by faster-whisper: its own decoder passes PyAV an option that PyAV 19
    removed, and fails on every file. Decoding it ourselves keeps the two from having to agree.
    """
    import av
    import numpy

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    if not chunks:
        raise SubtitleError("The video's sound could not be read")
    return numpy.concatenate(chunks, axis=1).reshape(-1).astype(numpy.float32) / 32768.0


def _transcribe(audio, duration, translate, report, cancelled):
    from faster_whisper import WhisperModel

    report("Reading the sound", None)
    sound = _decode(audio)
    _load_cuda_libraries()
    report("Loading the speech model", None)
    model = WhisperModel(MODEL, device="cuda", compute_type="float16", download_root=_model_dir())
    try:
        report("Listening" if not translate else "Listening and translating to English", 0.0)
        pieces, details = model.transcribe(sound, task="translate" if translate else "transcribe",
                                           vad_filter=True, beam_size=5)
        segments = []
        for piece in pieces:
            if cancelled.is_set():
                raise SubtitleError("Stopped")
            text = piece.text.strip()
            if text:
                segments.append({"start": round(piece.start, 2), "end": round(piece.end, 2), "text": text})
            if duration:
                report(None, min(piece.end / duration, 1.0))
        return segments, details.language, round(details.language_probability, 2)
    finally:
        del model       # the GPU memory goes back as soon as the job is done


# One video at a time: two would share a GPU that each wants a fair part of
_lock = asyncio.Lock()


async def subtitles(video, translate=False, on_progress=None):
    """Downloads the video's sound and transcribes it. Returns what the result needs, segments included.

    on_progress(stage, fraction) is awaited; stage None means only the fraction moved.
    """
    from app.tools import images      # for freeing the GPU, as a picture job does

    loop = asyncio.get_running_loop()
    stage = {"name": "Starting"}
    cancelled = threading.Event()

    def report(name, fraction):
        if name:
            stage["name"] = name
        if on_progress:
            asyncio.run_coroutine_threadsafe(on_progress(stage["name"], fraction), loop)

    if _lock.locked() and on_progress:
        await on_progress("Waiting for another video to finish", None)
    async with _lock:
        work = tempfile.mkdtemp(prefix="gateway-subtitles-")
        started = time.monotonic()
        try:
            audio, about = await asyncio.to_thread(_download, video, work, report)
            if on_progress:
                await on_progress("Freeing GPU memory from the language models", None)
            await images._free_vram_for_images()
            # And whatever ComfyUI still holds from the last picture: Flux alone fills most of the card
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                await images._release_comfy(client)
            try:
                segments, language, sure = await asyncio.to_thread(
                    _transcribe, audio, about["duration"], translate, report, cancelled)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            except SubtitleError:
                raise
            except Exception as e:      # a CUDA or decoding failure, said in words
                log.exception("Transcribing %s failed", video)
                raise SubtitleError(f"Transcribing failed: {e.__class__.__name__}: {str(e)[:200]}") from e
        finally:
            shutil.rmtree(work, ignore_errors=True)
    if not segments:
        raise SubtitleError("Nothing was said in this video that could be written down")
    return {**about, "segments": segments, "language": language, "language_sure": sure,
            "translated": translate, "model": f"Whisper {MODEL}", "seconds": round(time.monotonic() - started, 1)}


# --- files --------------------------------------------------------------------------------------

def _stamp(seconds, separator):
    milliseconds = int(round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def srt(segments):
    return "\n".join(f"{i}\n{_stamp(s['start'], ',')} --> {_stamp(s['end'], ',')}\n{s['text']}\n"
                     for i, s in enumerate(segments, 1))


def vtt(segments):
    return "WEBVTT\n\n" + "\n".join(f"{_stamp(s['start'], '.')} --> {_stamp(s['end'], '.')}\n{s['text']}\n"
                                    for s in segments)


def clock(seconds):
    """[m:ss] or [h:mm:ss], as the transcript shows a time."""
    seconds = int(seconds)
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def transcript(result):
    """The text a chat model is given afterwards, so it can summarise or answer questions about it."""
    head = (f"Subtitles of the YouTube video \"{result['title']}\""
            + (f" by {result['channel']}" if result.get("channel") else "")
            + f" ({result['url']}), {'translated to English' if result['translated'] else 'in ' + result['language']}"
            + f", transcribed from its sound by {result['model']} on this gateway:\n\n")
    return head + "\n".join(f"[{clock(s['start'])}] {s['text']}" for s in result["segments"])
