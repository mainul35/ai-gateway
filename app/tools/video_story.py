"""Video stories for the playground: a factual story, told in calm illustrated scenes, narrated.

A request (and the text and pictures given with it) becomes a storyboard written by a model from what
a web search found; every scene is drawn by the image model and then checked by a model that sees
pictures, against rules that are this feature's reason to exist:

- no faces, ever. People appear only from behind or far away, modestly dressed; women fully covered.
- a prophet is never shown in any form - the scene shows a place, light, nature or an object instead,
  and the narration tells it. Sahaba may appear only from behind or far away, modest and respectful.
- calm for children: each scene stays about ten seconds, the camera moves slowly, scenes cross-fade.
- facts only from the sources found or the material given, other scholarly or historical views told
  where they exist, and the sources listed with the video.

A picture that breaks a rule is drawn again (video.check.attempts), and failing that, the scene is
drawn as a place with nobody in it. The narration is spoken in English and Bangla
(scripts/tts_worker.py, on the CPU), and each language becomes its own video with its own words on
screen, drawn with libraqm so the Bangla script is shaped right.
"""
import asyncio
import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile

from app import settings

log = logging.getLogger("tools.video_story")

WIDTH, HEIGHT, FPS = 1280, 720, 25
SCENE_MIN, SCENE_MAX = 8.0, 14.0      # seconds a scene stays: slow enough for a child to take it in
FADE = 1.5                            # seconds two scenes overlap
LEAD_IN = 0.6                         # silence before a scene's narration starts
MIN_SCENES, MAX_SCENES = 5, 16
SIZE = "wide"                          # images.SIZES: 1344 x 768

STORY_PROMPT = """You write short, true, calm illustrated stories for children and families, told in scenes.

Use ONLY the facts in the SOURCES and the MATERIAL below. Never invent events, numbers, names or
quotations. When the sources disagree, or scholars and historians hold different views, say so
plainly in a scene near the end ("Some scholars say ..., others ..."). If something is not known, say
that it is not known. Keep a warm, gentle, interesting voice - no cliffhangers, no frightening detail.

Write 8 to 14 scenes. Each scene:
- "narration_en": one or two short sentences in English, at most 32 words, easy for a child.
- "narration_bn": the same in natural Bangla (Bengali script).
- "picture": what to draw, in English, as a calm wide illustration: places, nature, sky, light,
  buildings, objects, animals. Never a face. Never text.
- "people": "none" or "from_behind".
    "none" when the scene is about a prophet (any prophet, peace be upon them) - a prophet is NEVER
    drawn in any form; show the place, the light, the path, the sky instead - and whenever people
    are not needed.
    "from_behind" only for ordinary people or companions (sahaba): seen from behind or far away, small
    in the picture, modestly dressed; any woman fully covered; never a face.

Answer with JSON only, no other text:
{"title_en": "...", "title_bn": "...",
 "scenes": [{"narration_en": "...", "narration_bn": "...", "picture": "...", "people": "none"}],
 "perspectives": ["one line for each other view mentioned, in English"],
 "sources": [numbers of the SOURCES used]}"""

STYLE = ("soft painterly illustration for a children's storybook, warm gentle light, calm balanced "
         "composition, wide establishing shot, muted harmonious colours, no text, no lettering, "
         "no portraits, paintings, statues or silhouettes of people anywhere in the scene")
NO_PEOPLE = "no people, no human figures, no faces, an empty peaceful scene"
FROM_BEHIND = ("any people small in the distance and seen only from behind, modestly dressed in long "
               "loose clothing, women fully covered with long veils, faces never visible")

CHECK_PROMPT = """You check one picture for a children's video against strict rules. Look carefully.
Answer with JSON only:
{"faces": true if ANY human face is visible - front, side or partly, even small or stylised - and that
   includes faces or heads in a painting, portrait, framed picture, statue, carving or silhouette
   that is part of the scene,
 "people": the number of human figures in the picture, counting people shown in paintings, framed
   pictures, statues or silhouettes,
 "immodest": true if any person shows uncovered arms, legs, hair of a woman, or tight clothing,
 "text": true if there is any writing or lettering in the picture,
 "frightening": true if anything violent, bloody or scary is shown}"""


class VideoError(Exception):
    """Said back to the person as the reason no video was made."""


# --- the storyboard -------------------------------------------------------------------------------

def story_request(model, request, material, facts, pictures=()):
    """The chat request that writes the storyboard. `pictures` are (bytes, mime) given as context."""
    text = (f"REQUEST:\n{request}\n\nMATERIAL (given by the person):\n{material or '(none)'}\n\n"
            f"SOURCES (from a web search):\n{facts or '(none found)'}")
    content = [{"type": "text", "text": text}] + [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(data).decode()}"}}
        for data, mime in pictures]
    return {"model": model, "stream": False, "temperature": 0.4, "max_tokens": 6000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": STORY_PROMPT},
                         {"role": "user", "content": content if pictures else text}]}


def parse_story(text):
    """The storyboard out of the model's answer: checked, trimmed, every scene with what it needs."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", text or "", flags=re.S), re.S)
    if not match:
        raise VideoError("The story could not be written: the model did not answer with a storyboard")
    try:
        story = json.loads(match.group(0))
    except ValueError:
        raise VideoError("The story could not be written: the storyboard was not valid")
    scenes = []
    for scene in story.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        en, bn, picture = (str(scene.get(k) or "").strip() for k in ("narration_en", "narration_bn", "picture"))
        if en and picture:
            scenes.append({"narration_en": en, "narration_bn": bn or en, "picture": picture,
                           "people": "from_behind" if scene.get("people") == "from_behind" else "none"})
    if len(scenes) < MIN_SCENES:
        raise VideoError(f"The story came out too short ({len(scenes)} scenes); try asking about it differently")
    return {"title_en": str(story.get("title_en") or "A story").strip(),
            "title_bn": str(story.get("title_bn") or story.get("title_en") or "").strip(),
            "scenes": scenes[:MAX_SCENES],
            "perspectives": [str(p) for p in story.get("perspectives") or [] if p][:6],
            "sources": [int(s) for s in story.get("sources") or [] if str(s).isdigit()]}


# Said to the image model when it draws a scene again, so it does not make the same mistake: in the
# first real video (2026-10-24) a bookshelf scene came back with writing on the pages four times over
AVOID = {"a face is visible": "absolutely no faces, no portraits, paintings or statues of people",
         "a figure is not modestly dressed": "everyone fully covered in long loose robes",
         "something frightening is shown": "peaceful and safe, nothing dark or threatening",
         "there is writing in it": "no writing anywhere: books closed, pages blank, no signs, no inscriptions",
         "there are people in a scene meant to have none": NO_PEOPLE}


def picture_prompt(scene, plain=False, why=""):
    """What the image model is asked for. `plain` is the last resort: the place alone, nobody in it.
    `why` is what was wrong with the last picture of this scene."""
    who = NO_PEOPLE if plain or scene["people"] == "none" else FROM_BEHIND
    avoid = AVOID.get(why)
    return f"{scene['picture']}. {who}. {avoid + '. ' if avoid else ''}{STYLE}"


# --- the picture check ----------------------------------------------------------------------------

def check_request(model, png):
    return {"model": model, "stream": False, "temperature": 0, "max_tokens": 300,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": CHECK_PROMPT},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]}


def verdict(answer, scene):
    """(passes, why not): a picture with a face, an immodest figure, writing, anything frightening -
    or anyone at all in a scene meant to have nobody - does not pass. No answer is not a pass."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    if not match:
        return False, "the picture could not be checked"
    try:
        seen = json.loads(match.group(0))
    except ValueError:
        return False, "the picture could not be checked"
    if seen.get("faces"):
        return False, "a face is visible"
    if seen.get("immodest"):
        return False, "a figure is not modestly dressed"
    if seen.get("frightening"):
        return False, "something frightening is shown"
    if seen.get("text"):
        return False, "there is writing in it"
    if scene["people"] == "none" and int(seen.get("people") or 0) > 0:
        return False, "there are people in a scene meant to have none"
    return True, ""


# --- narration ------------------------------------------------------------------------------------

def _python():
    return os.path.expanduser(settings.get("video.tts.python", "VIDEO_TTS_PYTHON") or "")


def _worker():
    return os.path.join(settings.PROJECT_ROOT, "scripts", "tts_worker.py")


def is_available():
    return os.path.isfile(_python()) and bool(ffmpeg())


async def narrate(lang, texts, folder):
    """[(wav path, seconds)] for each text, spoken in English ("en") or Bangla ("bn")."""
    items = [{"text": t, "out": os.path.join(folder, f"{lang}-{i:02d}.wav")} for i, t in enumerate(texts)]
    process = await asyncio.create_subprocess_exec(
        _python(), _worker(), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    out, err = await process.communicate(json.dumps({"lang": lang, "items": items, "speed": 0.92}).encode())
    if process.returncode != 0:
        log.warning("narration in %s failed: %s", lang, err.decode(errors="replace")[-800:])
        raise VideoError(f"The {'Bangla' if lang == 'bn' else 'English'} narration could not be made")
    durations = json.loads(out.decode().strip().splitlines()[-1])["durations"]
    return [(item["out"], seconds) for item, seconds in zip(items, durations)]


# --- the video ------------------------------------------------------------------------------------

_ffmpeg = None


def ffmpeg():
    """The ffmpeg binary: the system's, or the one imageio-ffmpeg brings into the narration Python."""
    global _ffmpeg
    if _ffmpeg is None:
        found = shutil.which("ffmpeg") or ""
        if not found and os.path.isfile(_python()):
            try:
                found = subprocess.run([_python(), "-c", "import imageio_ffmpeg; print(imageio_ffmpeg.get_ffmpeg_exe())"],
                                       capture_output=True, text=True, timeout=30).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                found = ""
        _ffmpeg = found if found and os.path.isfile(found) else ""
    return _ffmpeg


def scene_lengths(narration_seconds):
    """How long each scene stays: its narration and a breath either side, between SCENE_MIN and MAX."""
    return [min(SCENE_MAX, max(SCENE_MIN, LEAD_IN + seconds + 1.4 + FADE)) for seconds in narration_seconds]


FONTS = {"bn": "NotoSansBengali.ttf", "en": "DejaVuSans.ttf"}


def _font(lang, size):
    """The subtitle font, laid out by libraqm: Bangla's vowel signs go before their letter and its
    conjuncts join only with it. ffmpeg's subtitle filter here has no shaping, and wrote "অনকে দনি"
    for "অনেক দিন" (2026-10-24)."""
    from PIL import ImageFont
    folder = os.path.expanduser(settings.get("video.fonts.dir", "VIDEO_FONTS_DIR") or "")
    path = os.path.join(folder, FONTS.get(lang, FONTS["en"]))
    if not os.path.isfile(path):
        path = os.path.join(folder, FONTS["en"])
    engine = ImageFont.Layout.RAQM if hasattr(ImageFont, "Layout") else None
    return ImageFont.truetype(path, size, layout_engine=engine)


def caption(text, path, lang, title=False):
    """A transparent frame-sized PNG with the words on it: the title in the middle, a line of
    narration at the bottom - white, outlined in dark, wrapped to fit."""
    from PIL import Image, ImageDraw
    font = _font(lang, 54 if title else 34)
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and draw.textlength(trial, font=font) > WIDTH - 200:
            lines.append(line)
            line = word
        else:
            line = trial
    lines += [line] if line else []
    height = int((font.size * 1.45) * len(lines))
    y = (HEIGHT - height) // 2 if title else HEIGHT - 40 - height
    for one in lines:
        width = draw.textlength(one, font=font)
        draw.text(((WIDTH - width) / 2, y), one, font=font, fill=(255, 255, 255, 255),
                  stroke_width=3 if title else 2, stroke_fill=(20, 20, 20, 230))
        y += int(font.size * 1.45)
    image.save(path)
    return path


async def _run(args, what):
    process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.DEVNULL,
                                                   stderr=asyncio.subprocess.PIPE)
    _, err = await process.communicate()
    if process.returncode != 0:
        log.warning("ffmpeg %s failed: %s", what, err.decode(errors="replace")[-1200:])
        raise VideoError(f"The video could not be put together ({what})")


async def assemble(pictures, narration, texts, title, lang, folder):
    """The video for one language (`texts` its narration, `title` shown over the first scene): each picture held for its scene with a slow zoom or pan, scenes
    cross-faded, the narration placed at each scene's start, the words faded in over each line as it is spoken. MP4 bytes."""
    binary = ffmpeg()
    if not binary:
        raise VideoError("No ffmpeg on this server to put the video together")
    lengths = scene_lengths([seconds for _, seconds in narration])
    starts, at = [], 0.0
    for length in lengths:
        starts.append(at)
        at += length - FADE
    total = starts[-1] + lengths[-1]

    clips = []
    for i, (png_path, length) in enumerate(zip(pictures, lengths)):
        frames = int(round(length * FPS))
        # Slow and gentle: in for one scene, out for the next, drifting a little sideways - never more
        # than 8 % over the whole scene, so nothing rushes past a child's eye
        zoom = ("min(1.0+0.08*on/{f},1.08)" if i % 2 == 0 else "max(1.08-0.08*on/{f},1.0)").format(f=frames)
        x = "(iw-iw/zoom)*" + ("on/{f}" if i % 4 < 2 else "(1-on/{f})").format(f=frames)
        clip = os.path.join(folder, f"clip-{i:02d}.mp4")
        await _run([binary, "-y", "-loop", "1", "-i", png_path, "-t", f"{length:.3f}", "-vf",
                    f"scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,crop={WIDTH * 2}:{HEIGHT * 2},"
                    f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
                    f"format=yuv420p", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", clip],
                   f"scene {i + 1}")
        clips.append(clip)

    # The scenes joined, each fading into the next
    inputs, chain, last = [], [], "[0:v]"
    for clip in clips:
        inputs += ["-i", clip]
    for i in range(1, len(clips)):
        out = f"[v{i}]"
        chain.append(f"{last}[{i}:v]xfade=transition=fade:duration={FADE}:offset={starts[i]:.3f}{out}")
        last = out
    joined = os.path.join(folder, f"joined-{lang}.mp4")
    await _run([binary, "-y", *inputs, "-filter_complex", ";".join(chain) or "[0:v]null[v0]",
                "-map", last if chain else "[v0]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", joined],
               "joining the scenes")

    # The narration, each line where its scene begins
    audio_inputs, delays = [], []
    for i, ((wav, _), start) in enumerate(zip(narration, starts)):
        audio_inputs += ["-i", wav]
        ms = int((start + LEAD_IN) * 1000)
        delays.append(f"[{i + 1}:a]adelay={ms}|{ms},aformat=channel_layouts=stereo[a{i}]")
    mix = ";".join(delays) + ";" + "".join(f"[a{i}]" for i in range(len(narration))) + \
        f"amix=inputs={len(narration)}:normalize=0,apad=whole_dur={total:.3f}[aout]"

    # The words: the title over the first scene, each line of narration while it is spoken
    shown = [(caption(title, os.path.join(folder, f"title-{lang}.png"), lang, title=True),
              0.4, min(4.5, lengths[0] - 1.0))]
    for i, (text, start, length) in enumerate(zip(texts, starts, lengths)):
        shown.append((caption(text, os.path.join(folder, f"line-{lang}-{i:02d}.png"), lang),
                      start + LEAD_IN - 0.2, start + length - FADE))
    caption_inputs, layers, base = [], [], "[0:v]"
    first = 1 + len(narration)                       # inputs: the video, the narration, then the words
    for k, (png, begin, until) in enumerate(shown):
        caption_inputs += ["-loop", "1", "-t", f"{total:.3f}", "-i", png]
        layers.append(f"[{first + k}:v]format=rgba,fade=t=in:st={begin:.2f}:d=0.4:alpha=1,"
                      f"fade=t=out:st={max(begin, until - 0.4):.2f}:d=0.4:alpha=1[c{k}]")
        layers.append(f"{base}[c{k}]overlay=0:0:enable='between(t,{begin:.2f},{until:.2f})'[o{k}]")
        base = f"[o{k}]"
    final = os.path.join(folder, f"story-{lang}.mp4")
    await _run([binary, "-y", "-i", joined, *audio_inputs, *caption_inputs, "-filter_complex",
                ";".join(layers) + ";" + mix, "-map", base, "-map", "[aout]",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", "-movflags", "+faststart", final],
               "adding narration and words")
    with open(final, "rb") as f:
        return f.read(), round(total, 1)


def workspace():
    return tempfile.mkdtemp(prefix="video-story-")
