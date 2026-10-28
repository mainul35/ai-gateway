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
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
import wave

from app import settings
from app.tools import child, quran, voices

log = logging.getLogger("tools.video_story")

WIDTH, HEIGHT, FPS = 1280, 720, 25
# A video uploaded to Facebook, YouTube, Instagram, TikTok or X as it is: H.264 High in 4:2:0, a
# keyframe every 2 s and at most 4 Mbit/s - the longest story stays well under X's 512 MB - with
# AAC at 48 kHz (adding the narration) and the index at the front
SHAREABLE = ["-pix_fmt", "yuv420p", "-profile:v", "high", "-level:v", "4.0", "-r", str(FPS), "-g", str(FPS * 2),
             "-maxrate", "4M", "-bufsize", "8M"]
SCENE_MIN = 8.0                       # seconds a scene stays at least: slow enough for a child to take it in
FADE = 1.5                            # seconds two scenes overlap
LEAD_IN = 0.6                         # silence before a scene's narration starts
PART_GAP = 0.3                        # a breath between the speakers of one scene
MIN_SCENES, MAX_SCENES = 5, 16
# A story the person gives may be long - a 4,000-character limit, meant for a topic, turned one
# away (2026-10-24) - and is cut into more scenes than a looked-up one, so no caption is a page long
STORY_CHARS = 20000
GIVEN_MAX_SCENES = 30
SIZE = "wide"                          # images.SIZES: 1344 x 768

SCENE_RULES = """Each scene:
- "narration_en": one or two short sentences in English, at most 32 words, easy for a child.
- "narration_bn": the same in natural Bangla (Bengali script).
- "picture": what to draw, in English: exactly what the scene tells - who is in it and how many,
  what is happening, where, the objects that matter, and the shot that shows it best - or a place,
  nature, light or an object that belongs to it. Never a face. Never text.
- "people": "none" or "from_behind".
    "none" when the scene is about a prophet (any prophet, peace be upon them) - a prophet is NEVER
    drawn in any form; show the place, the light, the path, the sky instead - and whenever people
    are not needed.
    "from_behind" only for ordinary people or companions (sahaba): seen from behind or far away, small
    in the picture, modestly dressed; any woman fully covered; never a face.
  Angels, jinn and Iblis are never drawn as figures either, and Allah is never depicted in any form:
  show light, the sky or the place instead.

Answer with JSON only, no other text:
{"title_en": "...", "title_bn": "...",
 "scenes": [{"narration_en": "...", "narration_bn": "...", "picture": "...", "people": "none"}],
 "perspectives": ["one line for each other view mentioned, in English"],
 "sources": [numbers of the SOURCES used]}"""

STORY_PROMPT = """You write short, true, calm illustrated stories for children and families, told in scenes.

Use ONLY the facts in the SOURCES and the MATERIAL below. Never invent events, numbers, names or
quotations. When the sources disagree, or scholars and historians hold different views, say so
plainly in a scene near the end ("Some scholars say ..., others ..."). If something is not known, say
that it is not known. Keep a warm, gentle, interesting voice - no cliffhangers, no frightening detail.

Write 8 to 14 scenes. """ + SCENE_RULES

# A story the person wrote is read word for word: the scenes are cut from it here, at its sentences,
# and no model writes its narration. Asked to keep the wording, the story model still rewrote it
# ("the Ummah" became "the community"); the person counts any change as a breach of the text.
ILLUSTRATE_PROMPT = """You illustrate a story that is read aloud word for word in a calm video for children
and families. Its SCENES are fixed: do not change, shorten or reorder them. For each scene give:
- "picture": what to draw, in English: exactly what that scene tells, so a viewer recognises the
  moment. Say who is in it and how many (one man, not "people"), what each is doing - the action
  itself, as the scene tells it - where, and the objects that matter, and the shot that shows it best
  (a medium shot from behind, a wide view of the place, a close view of an object). Describe every
  person in full, the same way, in every scene they are in - first decide each one's look (age, build,
  the colour and kind of clothing, a head covering) and repeat it word for word every time, never just
  their role - so the same person appears throughout. A spoken line shows the speaker and the
  listener in their place, from behind. A lesson or advice shows a calm object or place that belongs
  to it. One moment in one picture - never a split screen or several moments side by side; a scene
  that tells of several, shows the one that sums it up. Never a face. Never text.
- "people": "none" or "from_behind".
    "none" when the scene is about a prophet (any prophet, peace be upon them) - a prophet is NEVER
    drawn in any form; show the place, the light, the path, the sky instead - and whenever people
    are not needed (a lesson or advice is shown as a calm place, not as people).
    "from_behind" only for ordinary people or companions (sahaba): seen from behind or far away, small
    in the picture, modestly dressed; any woman fully covered; never a face.
  Angels, jinn and Iblis are never drawn as figures either, and Allah is never depicted in any form:
  show light, the sky or the place instead.
- "translation": the scene translated into {other}, faithfully, sentence by sentence: every sentence,
  nothing added, nothing left out, nothing explained or softened. Keep Islamic words as they are said
  in {other} (Allah, the Ummah: উম্মাহ, ইসলাম, নবী). Text in Arabic script (a verse of the Quran,
  a blessing) is copied unchanged. A transliteration of Arabic in Latin letters ("Wa iz qaala
  rabbuka ...") is written in Bangla letters when translating into Bangla, as Bangla Islamic books
  do, and kept as it is when translating into English.
Also the title in English and in Bangla ("title_en", "title_bn"): the story's own title translated,
or a short plain one if it has none.

Answer with JSON only, one entry for each scene, in order:
{"title_en": "...", "title_bn": "...", "scenes": [{"picture": "...", "people": "none", "translation": "..."}]}"""

# The languages a video story can be told in. For each: its name, the caption fonts (the first found
# in video.fonts.dir), and its machine voice - Kokoro (a language code and a voice; a male narrator
# where it has one) or one of Meta's MMS voices, from app/tools/voices.py, which owns the voices.
# "spaces": False for a script written without spaces between words, wrapped at any letter instead;
# "arabic": its own script is Arabic, kept and spoken.
LANGUAGES = {
    "en": {"name": "English", "fonts": ["DejaVuSans.ttf"], "speed": 0.92},
    "bn": {"name": "Bangla", "fonts": ["NotoSansBengali.ttf"], "speed": 0.85},
    "es": {"name": "Spanish", "fonts": ["DejaVuSans.ttf"], "speed": 0.92},
    "fr": {"name": "French", "fonts": ["DejaVuSans.ttf"], "speed": 0.92},
    "zh": {"name": "Chinese", "fonts": ["NotoSansSC[wght].ttf"], "speed": 0.92, "spaces": False},
    "ja": {"name": "Japanese", "fonts": ["NotoSansJP[wght].ttf"], "speed": 0.92, "spaces": False},
    "ko": {"name": "Korean", "fonts": ["NotoSansKR[wght].ttf"], "speed": 0.9},
    "th": {"name": "Thai", "fonts": ["NotoSansThai[wdth,wght].ttf"], "speed": 0.9, "spaces": False},
    "ar": {"name": "Arabic", "fonts": ["Amiri-Regular.ttf", "NotoNaskhArabic[wght].ttf"],
           "speed": 0.9, "arabic": True},
    "ur": {"name": "Urdu", "fonts": ["NotoNastaliqUrdu[wght].ttf", "Amiri-Regular.ttf"],
           "speed": 0.9, "arabic": True, "line_height": 2.4},
    "hi": {"name": "Hindi", "fonts": ["NotoSansDevanagari[wdth,wght].ttf"],
           "speed": 0.92},
}
for _lang in LANGUAGES:
    LANGUAGES[_lang]["voice"] = voices.narrator(_lang)


def language_name(lang):
    return LANGUAGES.get(lang, {}).get("name", lang)


LANGUAGE_NAMES = {code: ("Bangla (Bengali script)" if code == "bn" else info["name"]) for code, info in LANGUAGES.items()}

TRANSLATE_PROMPT = """You translate one line of a story for a children's video into {language}, faithfully,
sentence by sentence: every sentence, nothing added, nothing left out, nothing explained or softened.
Keep Islamic words as they are said in {language}. Text in Arabic script (a verse of the Quran, a
blessing) is copied unchanged. Answer with the translation only."""


def translate_request(model, text, lang):
    """The chat request that translates one edited line into another of the video's languages."""
    language = LANGUAGE_NAMES.get(lang, lang)
    return {"model": model, "stream": False, "temperature": 0.2, "max_tokens": 2000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": TRANSLATE_PROMPT.replace("{language}", language)},
                         {"role": "user", "content": text}]}


TRANSLATE_STORY_PROMPT = """You translate a story for a children's video into {language}, faithfully: each
numbered line on its own, sentence by sentence - every sentence, nothing added, nothing left out,
nothing explained or softened. Keep Islamic words as they are said in {language}. Text in Arabic script
(a verse of the Quran, a blessing) is copied unchanged. Translate the TITLE too.

Answer with JSON only, a line for each numbered line, in order:
{"title": "...", "lines": ["...", "..."]}"""


def translate_story_request(model, title, lines, lang):
    """The chat request that tells a whole story again in another language, for a language added."""
    text = f"TITLE: {title}\n\nLINES:\n" + "\n".join(f"{n}. {line}" for n, line in enumerate(lines, 1))
    room = min(32000, 2000 + 3 * sum(len(line) for line in lines))
    return {"model": model, "stream": False, "temperature": 0.2, "max_tokens": room,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": TRANSLATE_STORY_PROMPT.replace("{language}", language_name(lang))},
                         {"role": "user", "content": text}]}


def parse_translated_story(answer, count):
    """(title, lines) of a story told again, with a line for every scene, or a VideoError."""
    if answer is None:
        raise VideoError(MODEL_DOWN)
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer, flags=re.S), re.S)
    try:
        told = json.loads(match.group(0)) if match else {}
    except ValueError:
        told = {}
    lines = [" ".join(str(line).split()) for line in told.get("lines") or []]
    if len(lines) != count or not all(lines):
        raise VideoError("The story could not be translated; try again")
    return " ".join(str(told.get("title") or "").split()), lines


# --- who speaks: the cast and each scene's parts -------------------------------------------------

# The kinds of speaker, and how many distinct voices each has, so that two men in a story never sound
# alike; app/tools/voices.py counts them and makes the voices. The narrator is one voice, and the words
# of Allah, of a prophet or of an angel are always given to the narrator, never to a character.
KINDS = voices.KINDS

CAST_PROMPT = """You prepare a story for a narrated children's video. For each numbered SCENE, say who speaks
which part of it: the narrator, or a character whose own words are quoted.

Rules:
- Every part's "text" is copied exactly from the scene, and the parts, in order, make up the whole
  scene - nothing added, nothing left out, nothing changed.
- A character speaks only their own quoted words; everything else is the narrator's.
- The words of Allah, of a prophet (any prophet, peace be upon them) or of an angel are NEVER given to
  a character: their speaker is "narrator".
- The cast lists each character who speaks: an id ("c1", "c2", ...), the name the story gives them,
  and their kind - one of: man, older man, woman, older woman, boy, girl.

Answer with JSON only:
{"cast": [{"id": "c1", "name": "...", "kind": "man"}],
 "scenes": [[{"speaker": "narrator", "text": "..."}, {"speaker": "c1", "text": "..."}]]}"""


def cast_request(model, texts):
    """The chat request that finds the story's speakers and splits each scene into its spoken parts."""
    body = "\n".join(f"{n}. {text}" for n, text in enumerate(texts, 1))
    return {"model": model, "stream": False, "temperature": 0.1,
            "max_tokens": min(32000, 3000 + 3 * sum(len(t) for t in texts)),
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": CAST_PROMPT}, {"role": "user", "content": f"SCENES:\n{body}"}]}


def _same(a, b):
    return " ".join((a or "").split()) == " ".join((b or "").split())


def parse_cast(answer, texts):
    """(cast, [[{speaker, text}]] for each scene): a scene whose parts do not make up its words exactly
    is the narrator's alone, and a cast it does not fit is left out."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        found = json.loads(match.group(0)) if match else {}
    except ValueError:
        found = {}
    cast = [{"id": "narrator", "name": "Narrator", "kind": "narrator"}]
    for member in found.get("cast") or []:
        if isinstance(member, dict) and str(member.get("id") or "").startswith("c") and member.get("kind") in KINDS:
            cast.append({"id": str(member["id"]), "name": str(member.get("name") or member["id"])[:80],
                         "kind": member["kind"]})
    ids = {member["id"] for member in cast}
    scenes = found.get("scenes") or []
    parts = []
    for i, text in enumerate(texts):
        mine = scenes[i] if i < len(scenes) and isinstance(scenes[i], list) else []
        mine = [{"speaker": p.get("speaker") if p.get("speaker") in ids else "narrator",
                 "text": " ".join(str(p.get("text") or "").split())}
                for p in mine if isinstance(p, dict) and str(p.get("text") or "").strip()]
        if not mine or not _same(" ".join(p["text"] for p in mine), text):
            mine = [{"speaker": "narrator", "text": " ".join(text.split())}]
        merged = []                      # neighbours with one speaker are one part
        for part in mine:
            if merged and merged[-1]["speaker"] == part["speaker"]:
                merged[-1]["text"] += " " + part["text"]
            else:
                merged.append(dict(part))
        parts.append(merged)
    used = {p["speaker"] for scene in parts for p in scene}
    return assign_voices([m for m in cast if m["id"] in used or m["id"] == "narrator"]), parts


def assign_voices(cast):
    """The cast with a voice for each member: a different one for each of a kind, as far as the kind
    has voices; one already chosen is kept."""
    taken = {member.get("voice") for member in cast if member.get("voice")}
    for member in cast:
        if member.get("voice"):
            continue
        kind = member["kind"] if member["kind"] in KINDS else "man"
        slots = [f"{kind.replace(' ', '-')}-{n}" for n in range(1, KINDS[kind] + 1)] if kind != "narrator" else ["narrator"]
        member["voice"] = next((s for s in slots if s not in taken), slots[0])
        taken.add(member["voice"])
    return cast


TRANSLATE_PARTS_PROMPT = """You translate the spoken parts of a story for a children's video into {language},
faithfully: each numbered part on its own, sentence by sentence - nothing added, nothing left out,
nothing explained or softened. Keep Islamic words as they are said in {language}. Text in Arabic script
(a verse of the Quran, a blessing) is copied unchanged.

Answer with JSON only, one entry for each numbered part, in order: {"lines": ["...", "..."]}"""


def translate_parts_request(model, texts, lang):
    """The chat request that translates the parts of scenes spoken by more than one voice."""
    body = "\n".join(f"{n}. {text}" for n, text in enumerate(texts, 1))
    return {"model": model, "stream": False, "temperature": 0.2,
            "max_tokens": min(32000, 2000 + 3 * sum(len(t) for t in texts)),
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": TRANSLATE_PARTS_PROMPT.replace("{language}", language_name(lang))},
                         {"role": "user", "content": body}]}


def parse_translated_parts(answer, count):
    if answer is None:
        raise VideoError(MODEL_DOWN)
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer, flags=re.S), re.S)
    try:
        lines = [" ".join(str(x).split()) for x in (json.loads(match.group(0)).get("lines") or [])] if match else []
    except (ValueError, AttributeError):
        lines = []
    if len(lines) != count or not all(lines):
        raise VideoError("The parts could not be translated; try again")
    return lines


def scene_text(parts, lang):
    """A scene's whole line in a language, from its parts."""
    return " ".join((p.get("text") or {}).get(lang, "") for p in parts).strip()


def translation(answer):
    """The translated line out of the model's answer, or None."""
    text = re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S).strip().strip('"').strip()
    return text or None


SCENE_WORDS = 45          # a scene's narration is whole sentences, up to about this many words


def split_story(text, most=None):
    """(title, [scene texts]) of a story as written: its first line is its title when that line is
    short and does not end like a sentence; the rest is cut into scenes of whole sentences, a new
    scene at each paragraph, none much over SCENE_WORDS words unless one sentence is longer."""
    most = most or SCENE_WORDS
    # Left behind by copying from a web page, not part of the story: a line with no letter or digit
    # in it (a lone quotation mark) and the label "Paragraph Separator" - read word for word, the
    # voice would say them
    lines = ["" if not re.search(r"\w", line) or line.strip().lower() == "paragraph separator" else line
             for line in (text or "").replace("\r", "").strip().split("\n")]
    title = ""
    if lines and len(lines[0].split()) <= 12 and not re.search(r"[.!?।,;:]\s*$", lines[0]):
        title, lines = lines[0].strip(), lines[1:]
    paragraphs = [" ".join(p.split()) for p in re.split(r"\n\s*\n", "\n".join(lines)) if p.strip()]
    scenes, current = [], []

    def words(parts):
        return sum(len(p.split()) for p in parts)

    for paragraph in paragraphs:
        for sentence in re.split(r"(?<=[.!?।])\s+|(?<=[.!?।][\"'”’)])\s+", paragraph):
            if current and words(current + [sentence]) > most:
                scenes.append(" ".join(current))
                current = []
            current.append(sentence)
        if words(current) >= 8:                      # a paragraph's end is a scene's end
            scenes.append(" ".join(current))
            current = []
    if current:
        if scenes and words(current) < 8:
            scenes[-1] += " " + " ".join(current)
        else:
            scenes.append(" ".join(current))
    if len(scenes) > GIVEN_MAX_SCENES:               # a long story: longer scenes, never fewer words
        return split_story(text, int(most * 1.3) + 1)
    if len(scenes) < MIN_SCENES and most > 8:        # a short one: shorter scenes, more pictures
        return split_story(text, max(8, int(most * 0.6)))
    return title, scenes


NOTES_PROMPT = """You prepare a story someone wrote for a video in which every word of the story is read
aloud. Some of what they wrote is not part of the story to be read: a reference or citation (a hadith's
collection and number, a verse's surah and ayah number, a book and its page, a link), a footnote, a
source note, a "References" or "Sources" list, a footnote marker such as [1], * or a superscript number.
These are shown silently on a dark screen instead of being read.
You get the text in numbered paragraphs. List every such piece, copied exactly as it is written,
character for character, with the number of its paragraph. A piece is a whole paragraph, a whole line,
or the exact words inside a sentence (a citation in brackets at the end of a sentence).
Everything else is the story and is not listed: the narration, what people said, and the words of a
verse or a hadith that are part of the telling. Only where something comes from is a note, never
what it says. When unsure, it is the story.
"show" is false for a bare marker ([1], *, a superscript number) that only points to a note, and true
for anything with content.
Answer with JSON only: {"notes": [{"paragraph": 1, "text": "...", "show": true}]}, an empty list when
there are none."""


def story_paragraphs(text):
    """A given story's paragraphs as split_story finds them: apart at blank lines."""
    return [p for p in re.split(r"\n\s*\n", (text or "").replace("\r", "").strip()) if p.strip()]


def notes_request(model, paragraphs):
    """The chat request that finds a given story's references and footnotes (NOTES_PROMPT)."""
    text = "\n\n".join(f"[{n}] {p}" for n, p in enumerate(paragraphs, 1))
    return {"model": model, "stream": False, "temperature": 0, "max_tokens": 4000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": NOTES_PROMPT}, {"role": "user", "content": text}]}


def take_notes(answer, paragraphs):
    """(the story without its notes, [(paragraph index, note)], each paragraph without its notes).
    Only a piece found in its paragraph exactly as the model copied it (spacing aside) is taken out,
    so no word of the story is lost to a model's mistake; a bare marker is taken out and not shown.
    No answer, or one that would leave no story, takes nothing out."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        listed = (json.loads(match.group(0)).get("notes") or []) if match else []
    except (ValueError, AttributeError):
        listed = []
    kept, notes = list(paragraphs), []
    for entry in listed if isinstance(listed, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            k = int(entry.get("paragraph")) - 1
        except (TypeError, ValueError):
            continue
        piece = " ".join(str(entry.get("text") or "").split())
        if not piece or not 0 <= k < len(kept):
            continue
        found = re.search(r"\s+".join(map(re.escape, piece.split())), kept[k])
        if not found:
            continue
        kept[k] = kept[k][:found.start()] + kept[k][found.end():]
        if entry.get("show", True) is not False and re.search(r"\w", piece):
            notes.append((k, piece))
    # What a citation leaves behind: empty brackets, a space before a full stop, lines with no word
    tidy = []
    for paragraph in kept:
        paragraph = re.sub(r"[(\[]\s*[)\]]", "", paragraph)
        paragraph = re.sub(r"[ \t]+([.,;:!?।])", r"\1", paragraph)
        tidy.append("\n".join(line.rstrip() for line in paragraph.split("\n") if re.search(r"\w", line)))
    told = "\n\n".join(p for p in tidy if p.strip())
    if not re.search(r"\w", told):
        return "\n\n".join(paragraphs), [], list(paragraphs)
    return told, notes, tidy


def notes_by_scene(notes, tidy, scenes):
    """[[note, ...] for each scene]: each note shown after the scene where the story before it ends -
    the scene holding the end of the nearest paragraph at or before the note's that still has story
    in it (`tidy`, take_notes's paragraphs); the first scene when there is none."""
    out = [[] for _ in scenes]
    flat = [" ".join(s.split()) for s in scenes]
    for k, note in notes:
        at = None
        for paragraph in reversed(tidy[:k + 1]):
            tail = " ".join(paragraph.split())[-40:]
            at = next((i for i in range(len(flat) - 1, -1, -1) if tail and tail in flat[i]), None)
            if at is not None:
                break
        if scenes:
            out[at or 0].append(note)
    return out


def note_seconds(text):
    """How long a note stays on the dark screen: long enough to read it, slowly."""
    return min(14.0, max(5.0, 3.0 + 0.4 * len(text.split())))


def illustrate_request(model, title, scenes, lang):
    """The chat request for each fixed scene's picture and its translation into the other language."""
    other = "English" if lang == "bn" else "Bangla (Bengali script)"
    text = (f"TITLE: {title or '(none)'}\n\nSCENES:\n"
            + "\n".join(f"{n}. {scene}" for n, scene in enumerate(scenes, 1)))
    # Room for the translation, as long as the story, and a picture for every scene
    room = min(32000, 4000 + 2 * sum(len(scene) for scene in scenes))
    return {"model": model, "stream": False, "temperature": 0.2, "max_tokens": room,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": ILLUSTRATE_PROMPT.replace("{other}", other)},
                         {"role": "user", "content": text}]}


REDRAW_PROMPT = """You rewrite what one scene's picture shows, for a calm story video for children and
families. You are given the scene's words (what is read aloud while it shows), the description its
picture was drawn from, and the person's request. The request is about this picture only. It may be
a whole new description, or only what should be different - then keep everything else from the
current description and change what it asks. Follow what it asks closely and literally: the pose,
the action, the place, the light, the number of people, the shot.
Write the new description in English, as one paragraph: who is in it and how many, what each is doing
and how they are placed, where, the objects that matter, the light and the time of day, and the shot.
Describe each person in full, as the current description does, so they look the same as in the other
scenes. The picture is drawn only from your words, so describe a posture by where each part of the
body is - the head, the hands, the knees, the feet, what touches the ground - and say which way every
person faces; people doing the same thing together all face the same way. One moment in one picture.
Never a face, never text.
These rules come before the request, and the description must keep to them even when the request
asks otherwise: a prophet (peace be upon them) is never drawn in any form - show the place, the light
or the path instead; angels, jinn and Iblis are never figures, and Allah is never depicted in any
form; people are seen from behind or far away, the face completely hidden, modestly dressed in long
loose clothing; any woman fully covered in a long loose robe and a headscarf.
Answer with JSON only: {"picture": "...", "people": "none" or "from_behind", "note": "..."} -
"people" is "none" when nobody may or need be in it; "note" says in one short sentence, in the
language of the request, what of the request could not be followed and why, or is "" when all of it was."""


def redraw_request(model, words, current, asked):
    """The chat request for one scene's picture description, rewritten as the person asks."""
    text = (f"SCENE WORDS: {words or '(none)'}\n\nCURRENT DESCRIPTION: {current or '(none)'}\n\n"
            f"REQUEST: {asked}")
    return {"model": model, "stream": False, "temperature": 0.2, "max_tokens": 1500,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": REDRAW_PROMPT}, {"role": "user", "content": text}]}


def parse_redraw(answer):
    """(picture, people, note) from the rewritten description; VideoError when there is none."""
    if answer is None:
        raise VideoError(MODEL_DOWN)
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        found = json.loads(match.group(0)) if match else {}
    except ValueError:
        found = {}
    picture = " ".join(str(found.get("picture") or "").split())
    if not picture:
        raise VideoError("The new picture could not be described; try again")
    people = "none" if found.get("people") == "none" else "from_behind"
    return picture, people, " ".join(str(found.get("note") or "").split())


def parse_illustrated(answer, title, scenes, lang):
    """The storyboard of a given story: its own words for its language, the translation for the other,
    a picture for every scene. Every scene must be there, or nothing is made."""
    if answer is None:
        raise VideoError(MODEL_DOWN)
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        drawn = json.loads(match.group(0)) if match else {}
    except ValueError:
        drawn = {}
    entries = [e for e in drawn.get("scenes") or [] if isinstance(e, dict)]
    if len(entries) != len(scenes) or not all(str(e.get("picture") or "").strip() for e in entries):
        raise VideoError("The story's pictures could not be planned; try again")
    other = "en" if lang == "bn" else "bn"
    told = []
    for scene, entry in zip(scenes, entries):
        translation = str(entry.get("translation") or "").strip()
        if not translation:
            raise VideoError("The story could not be translated; try again")
        told.append({f"narration_{lang}": scene, f"narration_{other}": translation,
                     "picture": str(entry["picture"]).strip(),
                     "people": "from_behind" if entry.get("people") == "from_behind" else "none"})
    title_en = str(drawn.get("title_en") or "").strip()
    title_bn = str(drawn.get("title_bn") or "").strip()
    if title:                                        # the story's own title, as written, in its language
        title_en, title_bn = (title, title_bn or title) if lang == "en" else (title_en or title, title)
    return {"title_en": title_en or "A story", "title_bn": title_bn or title_en or "", "scenes": told,
            "perspectives": [], "sources": []}

GIVEN_LENGTH = 400        # characters: a request this long is a story to tell, not a topic to look up

CHOOSE_PROMPT = """Someone asked for an illustrated story video. Decide what it should tell, and answer
with JSON only:
{"tell": "given" when the LATEST request asks to tell a story the person wrote themselves - in an
         EARLIER message or in the attached DOCUMENTS ("make a video of the story above", "use exactly
         what I gave", "this story") - or "research" when it asks for a story about a topic,
 "earlier": the number of the EARLIER message that holds that story, or 0 when it is in the DOCUMENTS
            or there is none}"""

# The scene's own description decides the shot and who is in it. "Small in the distance, women fully
# covered with long veils, wide establishing shot" was added to every scene with people, and the
# woodcutter's story (2026-10-25) came out as groups of veiled figures standing about in a forest,
# never a man cutting a tree.
STYLE = ("soft painterly illustration for a children's storybook, warm gentle light, calm balanced "
         "composition, the action clearly visible, muted harmonious colours, no text, no lettering, "
         "no portraits, paintings, statues or silhouettes of people anywhere in the scene")
NO_PEOPLE = "no people, no human figures, no faces, an empty peaceful scene"
# "Turned away" let Qwen-Image draw faces in profile, and naming a woman, even "only if the scene has
# one", made the woodcutter's employer a veiled woman: a woman is named only when the scene has one
FROM_BEHIND = ("only the people the scene describes, each in a back view - the back of the head towards "
               "the viewer, the face completely hidden, no profile - modestly dressed in long loose "
               "clothing with long sleeves")
COVERED = "any woman fully covered in a long loose robe and a headscarf"
# For the editing model (Qwen-Image-Edit), on a picture whose only fault is a face: on the woodcutter's
# scenes it turned every head away and kept the moment, and all six passed the check after it
TURN_AWAY = ("Turn every person so that they face completely away from the viewer, their back and the back "
             "of the head towards us: only the back of the head and hair or cap can be seen, no face, no "
             "cheek, no beard, no profile. Keep their clothes, what they are doing, the objects and "
             "everything else in the picture exactly as it is.")
# What TURN_AWAY puts right, so a picture with only that fault keeps its moment rather than being drawn again
TURNABLE = ("a face is visible", "a person faces the viewer")
WOMAN = re.compile(r"\b(wom[ae]n|girls?|mother|wife|daughter|sister|lady|ladies|queen|grandmother)\b", re.I)

CHECK_PROMPT = """You check one picture for a children's video against strict rules. Look carefully.
The picture was drawn for this scene: {scene}
Answer with JSON only:
{"faces": true if ANY human face is visible - front, side or partly, even small or stylised - and that
   includes faces or heads in a painting, portrait, framed picture, statue, carving or silhouette
   that is part of the scene,
 "people": the number of human figures in the picture, counting people shown in paintings, framed
   pictures, statues or silhouettes,
 "towards": true if ANY person's body faces the viewer - their chest, front or the top of a bowed head
   towards the viewer - even when the face itself cannot be seen,
 "immodest": true if any person shows uncovered arms, legs, hair of a woman, or tight clothing,
 "text": true if there is any writing or lettering in the picture,
 "frightening": true if anything violent, bloody or scary is shown,
 "matches": true if the picture shows what the scene describes - its main subject, what is happening,
   how each person is placed (their posture, and which way they face) and where - false if it shows
   something else (other people, another action or posture, another place)}"""


class VideoError(Exception):
    """Said back to the person as the reason no video was made."""


# No answer at all is the model not running, not the story: said as such
MODEL_DOWN = "The model that writes the story could not be started just now; try again in a few minutes"


# --- the storyboard -------------------------------------------------------------------------------

def choose_request(model, latest, earlier, documents):
    """The chat request that decides whether a short request points at a story given before."""
    lines = "\n\n".join(f"EARLIER {n}:\n{text[:3000]}" for n, text in enumerate(earlier, 1))
    text = (f"{lines or 'EARLIER: (none)'}\n\nDOCUMENTS:\n{documents[:3000] if documents else '(none)'}"
            f"\n\nLATEST REQUEST:\n{latest}")
    return {"model": model, "stream": False, "temperature": 0, "max_tokens": 200,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": CHOOSE_PROMPT}, {"role": "user", "content": text}]}


def given_story(answer, earlier, documents):
    """The story the person gave, as chosen; None when the request is a topic to look up."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        choice = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None
    if choice.get("tell") != "given":
        return None
    n = int(choice.get("earlier") or 0) if str(choice.get("earlier") or "0").isdigit() else 0
    if 0 < n <= len(earlier):
        return earlier[n - 1]
    return documents or None


def is_bangla(text):
    letters = [c for c in text or "" if c.isalpha()]
    return bool(letters) and sum("ঀ" <= c <= "৿" for c in letters) / len(letters) > 0.3


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
    if text is None:
        raise VideoError(MODEL_DOWN)
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
         "a person faces the viewer": "every person turned completely away from the viewer, their back towards us",
         "a figure is not modestly dressed": "everyone fully covered in long loose robes",
         "something frightening is shown": "peaceful and safe, nothing dark or threatening",
         "there is writing in it": "no writing anywhere: books closed, pages blank, no signs, no inscriptions",
         "there are people in a scene meant to have none": NO_PEOPLE,
         "the picture does not show the scene": "show exactly the subject and the action described, "
                                                "clearly, in the foreground"}


# The storyboard's Bangla read like a translation for adults - উপত্যকা, সংগ্রহ করত, প্রতিষ্ঠান - and
# its long sentences ran together when spoken. A second pass rewrites it for children, on its own.
BANGLA_PROMPT = """You tell true stories to young children in Bangladesh (6 to 10 years old), the way a
parent tells a story at bedtime. For each scene you get the English narration, and a first Bangla
draft that is too formal. Tell each scene again in Bangla, from the English, for children; use the
draft only to see how names are spelled in Bangla. It is read aloud by a computer voice.

How to write it:
- Everyday spoken Bangla as families in Bangladesh speak it (cholito), warm and simple.
- Words a young child knows. Where the draft uses a bookish word, use the plain one; for example
  নামক -> নামের, পণ্ডিতগণ -> জ্ঞানী মানুষেরা, গ্রন্থ -> বই, সংগ্রহ করা -> জোগাড় করা,
  প্রতিষ্ঠান -> জায়গা, পরিণত হলো -> হয়ে উঠল, ইতিহাসবিদ -> ইতিহাস জানা মানুষেরা, জল -> পানি.
  These are only swaps for words that are there: never bring in a word or a place from this list.
- Two or three sentences a scene, each about 6 to 12 words, flowing the way people talk (আর, তখন,
  তাই) - not one long sentence, and not a row of tiny choppy ones.
- A name or a word a child may not know (a place, a person, an Islamic term, a word like অনুবাদ)
  gets a few easy words of explanation the first time it comes.
- Islamic words as Bangladeshi Muslim families say them: আল্লাহ, নবী, সাহাবি, নামাজ, মসজিদ. After a
  prophet's name write the blessing out in full (আলাইহিস সালাম; for our Prophet: সাল্লাল্লাহু
  আলাইহি ওয়া সাল্লাম), never as (আ.) or (সা.).
- For the voice: numbers and years in words, no English words, no abbreviations, no brackets or
  quotation marks. A comma where a reader would take a breath.

What not to do:
- Say what the English says and nothing more: no new places, reasons, results, feelings or
  conclusions. Explaining a word is the only thing you may add.

The title: short and easy; a foreign name in it is followed by what it means.

Answer with JSON only: {"title_bn": "...", "narration_bn": ["one string for each scene, in order"]}"""


def bangla_request(model, story):
    """The chat request that rewrites the storyboard's Bangla for children."""
    lines = "\n".join(f"{i}. EN: {s['narration_en']}\n   BN: {s['narration_bn']}"
                       for i, s in enumerate(story["scenes"], 1))
    text = f"TITLE: {story['title_en']} / {story['title_bn']}\n\nSCENES:\n{lines}"
    return {"model": model, "stream": False, "temperature": 0.3, "max_tokens": 5000,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": BANGLA_PROMPT}, {"role": "user", "content": text}]}


def apply_bangla(answer, story):
    """The story with its Bangla rewritten, when the answer has a line for every scene; else unchanged."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    try:
        rewritten = json.loads(match.group(0)) if match else {}
    except ValueError:
        rewritten = {}
    lines = [str(line).strip() for line in rewritten.get("narration_bn") or []]
    if len(lines) != len(story["scenes"]) or not all(lines):
        log.info("the Bangla rewrite was not usable; keeping the first draft")
        return story
    for scene, line in zip(story["scenes"], lines):
        scene["narration_bn"] = line
    story["title_bn"] = str(rewritten.get("title_bn") or "").strip() or story["title_bn"]
    return story


def picture_prompt(scene, plain=False, why=""):
    """What the image model is asked for. `plain` is the last resort: the place alone, nobody in it.
    `why` is what was wrong with the last picture of this scene."""
    who = NO_PEOPLE if plain or scene["people"] == "none" else FROM_BEHIND
    if who == FROM_BEHIND and WOMAN.search(scene["picture"]):
        who += "; " + COVERED
    avoid = AVOID.get(why)
    return f"{scene['picture']}. {who}. {avoid + '. ' if avoid else ''}{STYLE}"


# --- the picture check ----------------------------------------------------------------------------

def check_request(model, png, scene=None):
    described = scene["picture"] if scene else "(no description)"
    return {"model": model, "stream": False, "temperature": 0, "max_tokens": 300,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": CHECK_PROMPT.replace("{scene}", described)},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}]}]}


def verdict(answer, scene, must_match=True):
    """(passes, why not): a picture with a face, an immodest figure, writing, anything frightening -
    or anyone at all in a scene meant to have nobody - does not pass. No answer is not a pass. One
    that does not show its scene is drawn again while there are tries left (`must_match`); the last
    try is kept rather than leave the scene without a picture."""
    match = re.search(r"\{.*\}", re.sub(r"<think>.*?</think>", "", answer or "", flags=re.S), re.S)
    if not match:
        return False, "the picture could not be checked"
    try:
        seen = json.loads(match.group(0))
    except ValueError:
        return False, "the picture could not be checked"
    if seen.get("faces"):
        return False, "a face is visible"
    if scene["people"] != "none" and seen.get("towards"):
        return False, "a person faces the viewer"
    if seen.get("immodest"):
        return False, "a figure is not modestly dressed"
    if seen.get("frightening"):
        return False, "something frightening is shown"
    if seen.get("text"):
        return False, "there is writing in it"
    if scene["people"] == "none" and int(seen.get("people") or 0) > 0:
        return False, "there are people in a scene meant to have none"
    if must_match and seen.get("matches") is False:
        return False, "the picture does not show the scene"
    return True, ""


# --- narration ------------------------------------------------------------------------------------

def _python():
    return os.path.expanduser(settings.get("video.tts.python", "VIDEO_TTS_PYTHON") or "")


def _worker():
    return os.path.join(settings.PROJECT_ROOT, "scripts", "tts_worker.py")


def is_available():
    return os.path.isfile(_python()) and bool(ffmpeg())


# Arabic in a story: shown on screen as written, never read by a voice that cannot say it. The
# blessings after a name are said in full - "(AS)" read out as letters, and Arabic script skipped,
# lost them - and anything else in Arabic, a verse of the Quran above all, is not recited by a
# machine: the scene is held long enough to read it.
ARABIC = "؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿"
ARABIC_RUN = re.compile(f"[{ARABIC}]+(?:\\s+[{ARABIC}]+)*")
BLESSINGS = [  # (abbreviations, the Arabic without its vowel marks, said in English, said in Bangla)
    (r"\((?:AS|A\.S\.?|as|a\.s\.?)\)|\(আ\.?\)", {"عليه السلام"}, "alayhis salam", "আলাইহিস সালাম"),
    (r"\((?:SAW|SAWS|S\.A\.W\.?|PBUH|pbuh|saw)\)|\(সা\.?\)", {"صلى الله عليه وسلم", "ﷺ"},
     "sallallahu alayhi wa sallam", "সাল্লাল্লাহু আলাইহি ওয়া সাল্লাম"),
    (r"\((?:SWT|S\.W\.T\.?|swt)\)", {"سبحانه وتعالى", "سبحنه وتعلى"}, "subhanahu wa ta'ala", "সুবহানাহু ওয়া তায়ালা"),
    (r"\((?:RA|R\.A\.?)\)|\(রা\.?\)", {"رضي الله عنه"}, "radiyallahu anhu", "রাদিয়াল্লাহু আনহু"),
]
READ_SECONDS = 0.6        # a word of Arabic shown and not spoken is held this long, for reading


def _bare_arabic(text):
    """Arabic without its vowel and Quranic marks, for recognising a blessing however it is written."""
    text = re.sub("[ً-ٰٟۖ-ۭـ]", "", text).replace("ٱ", "ا")
    return " ".join(text.split())


def spoken(text, lang):
    """(what the voice says, the words of Arabic it leaves for the eye) for one line of a story. In a
    language written in Arabic script (Arabic, Urdu) the line is the voice's to say - all but a verse
    of the Quran, which is the qari's."""
    arabic_script = LANGUAGES.get(lang, {}).get("arabic")
    if not arabic_script:
        for abbreviation, _, en, bn in BLESSINGS:
            text = re.sub(abbreviation, f" {bn if lang == 'bn' else en} ", text)
    unspoken = 0

    def say(match):
        nonlocal unspoken
        words = match.group(0).split()
        if arabic_script:
            if len(words) >= quran.MIN_WORDS and quran.find(match.group(0)):
                unspoken += len(words)
                return " "
            return match.group(0)
        bare = _bare_arabic(match.group(0))
        for _, arabic, en, bn in BLESSINGS:
            if bare in arabic:
                return f" {bn if lang == 'bn' else en} "
        unspoken += len(words)
        return " "

    text = ARABIC_RUN.sub(say, text)
    text = re.sub(r"\(\s*\)", " ", text)
    text = re.sub(r"\s+([,.;:!?।)])", r"\1", re.sub(r"\(\s+", "(", " ".join(text.split())))
    return text.strip(), unspoken


async def narrate_parts(lang, scenes, cast_map, folder):
    """[(wav path, seconds)] for each scene, its parts spoken by their speakers: the narrator, or a
    character in the voice their slot is given (app/tools/voices.py), turned a little where the
    language's model has no voice spare for them. `scenes` is a list of scenes, each a list of
    {speaker, text} in this language, in order; `cast_map` maps a speaker id to their voice slot. A
    part that is a verse of the Quran is the qari's, spliced into that part only; a part all in
    Arabic is silence, held for reading; the parts of a scene are one line of audio, a breath apart."""
    language = LANGUAGES.get(lang) or LANGUAGES["en"]
    narrator = language["voice"]
    plan = voices.plan(lang, narrator)
    records = []
    for i, scene in enumerate(scenes):
        for k, part in enumerate(scene or []):
            text = " ".join(str(part.get("text") or "").split())
            if not text:
                continue
            slot = cast_map.get(part.get("speaker") or "narrator", "narrator")
            pitch, tempo = (plan.get(slot) or plan["narrator"])[-2:]
            words, unspoken = spoken(text, lang)
            records.append({"i": i, "k": k, "text": text, "slot": slot, "pitch": pitch,
                            "tempo": tempo, "words": words, "unspoken": unspoken})
    # The words of every part, spoken in the speaker's voice, in one worker job (a model loads once)
    items = []
    for n, r in enumerate(records):
        if re.search(r"\w", r["words"]):
            r["out"] = os.path.join(folder, f"{lang}-{r['i']:02d}-{r['k']:02d}.wav")
            items.append((n, {"text": r["words"], "out": r["out"],
                              "voice": voices.job_voice(lang, r["slot"], narrator)}))
    if items:
        job = {"lang": lang, "voice": narrator, "speed": language["speed"],
               "items": [it for _, it in items]}
        out, err, code = await child.run(_python(), _worker(), stdin=json.dumps(job).encode(),
                                         stdout=asyncio.subprocess.PIPE)
        if code != 0:
            log.warning("narration in %s failed: %s", lang, err.decode(errors="replace")[-800:])
            raise VideoError(f"The {language_name(lang)} narration could not be made")
        durations = json.loads(out.decode().strip().splitlines()[-1])["durations"]
        for (n, _), seconds in zip(items, durations):
            records[n]["dur"] = seconds + READ_SECONDS * records[n]["unspoken"]
    # Each part's audio: a shifted voice is turned first, so a qari spliced in after is not turned too
    for r in records:
        has_words = re.search(r"\w", r["words"]) is not None
        name = f"{lang}-{r['i']:02d}-{r['k']:02d}"
        if has_words:
            path, seconds = r["out"], r["dur"]
            if (r["pitch"], r["tempo"]) != (1.0, 1.0):
                path, seconds = await _retune(path, r["pitch"], r["tempo"], folder, name)
        else:
            path, seconds = held_silence(r["text"], lang, os.path.join(folder, f"{name}.wav"))
        r["audio"] = await with_recitation(r["text"], lang, path if has_words else None, seconds,
                                           folder, name) or (path, seconds)
    # One scene's parts, in order, into its one line of audio
    by_scene = {}
    for r in records:
        by_scene.setdefault(r["i"], []).append(r["audio"])
    results = []
    for i in range(len(scenes)):
        audio = by_scene.get(i)
        if not audio:
            results.append(held_silence("", lang, os.path.join(folder, f"{lang}-{i:02d}.wav")))
        elif len(audio) == 1:
            results.append(audio[0])
        else:
            results.append(await _concat_parts(audio, folder, f"{lang}-{i:02d}"))
    return results


async def _retune(source, pitch, tempo, folder, name):
    """(wav, seconds) of a line spoken in a voice the model does not have: turned a little in pitch
    and pace (app/tools/voices.py), so two characters of a kind are told apart by ear."""
    out = os.path.join(folder, f"{name}-turned.wav")
    await _run([ffmpeg(), "-y", "-i", source, "-af", voices.retune(pitch, tempo), "-ar", "24000", out],
               "turning a voice")
    with wave.open(out, "rb") as w:
        return out, w.getnframes() / w.getframerate()


async def _concat_parts(audio, folder, name):
    """(wav, seconds) of one scene's parts, in order, a breath between the speakers and none after
    the last: the scene is held as long as all of them, told one after the other."""
    out = os.path.join(folder, f"{name}.wav")
    inputs, layers = [], []
    for k, (path, _) in enumerate(audio):
        inputs += ["-i", path]
        pad = f",apad=pad_dur={PART_GAP}" if k < len(audio) - 1 else ""
        layers.append(f"[{k}:a]aformat=sample_rates=24000:channel_layouts=mono{pad}[p{k}]")
    layers.append("".join(f"[p{k}]" for k in range(len(audio))) +
                  f"concat=n={len(audio)}:v=0:a=1[out]")
    await _run([ffmpeg(), "-y", *inputs, "-filter_complex", ";".join(layers), "-map", "[out]", out],
               "joining a scene's parts")
    with wave.open(out, "rb") as w:
        return out, w.getnframes() / w.getframerate()


def held_silence(text, lang, out):
    """(a breath of silence, seconds the scene is held) for a line all in Arabic with no recitation:
    long enough to read it."""
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        w.writeframes(b"\0\0" * 2400)
    return out, max(3.0, READ_SECONDS * spoken(text, lang)[1])


def needs_reading(text, lang="en"):
    """Whether a line has anything for a person to read aloud: a line that is a verse is recited by
    the qari, and in a language not written in Arabic script a line all in Arabic is held in silence,
    never read by the person (the page asks the same)."""
    return re.search(r"\w", spoken(text, lang)[0]) is not None


async def with_recitation(text, lang, voice, seconds, folder, name):
    """(audio, seconds) for a line with a verse of the Quran in it: the verse recited by a qari
    (app/tools/quran.py) after `voice` - the line's words, spoken or read by the person, or None when
    there are none - or before it when the line opens with the verse; the scene lasts as long as both.
    None when the line has no verse, or its recitation could not be had."""
    runs = [m for m in ARABIC_RUN.finditer(text) if len(m.group(0).split()) >= quran.MIN_WORDS]
    ayat, recited_words = [], 0
    for run in runs:
        found = await asyncio.to_thread(quran.find, run.group(0))
        recited_words += len(run.group(0).split()) if found else 0
        ayat += [a for a in found if a not in ayat]
    if not ayat:
        return None
    recited = os.path.join(folder, f"{name}-quran.wav")
    recitation = await quran.recite(ayat, recited, ffmpeg())
    if recitation is None:
        return None
    hold = max(0.0, READ_SECONDS * (spoken(text, lang)[1] - recited_words))   # Arabic shown, not recited
    if voice is None:
        return recited, recitation + hold
    first, second = (recited, voice) if runs[0].start() < len(text) // 4 else (voice, recited)
    joined = os.path.join(folder, f"{name}-with-quran.wav")
    same = "aformat=sample_rates=24000:channel_layouts=mono"
    await _run([ffmpeg(), "-y", "-i", first, "-i", second, "-filter_complex",
                f"[0:a]{same},apad=pad_dur=0.6[a0];[1:a]{same}[a1];[a0][a1]concat=n=2:v=0:a=1[a]",
                "-map", "[a]", joined], "adding the recitation")
    with wave.open(joined, "rb") as w:
        return joined, w.getnframes() / w.getframerate() + hold


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
    """How long each scene stays: its narration and a breath either side, at least SCENE_MIN. No upper
    limit: a cap of 14 s let a long Bangla line run on over the next scene's narration."""
    return [max(SCENE_MIN, LEAD_IN + seconds + 1.4 + FADE) for seconds in narration_seconds]


# The first font found in video.fonts.dir is used. Arabic within another language's line needs one
# with the Quran's own letters and marks (alef wasla, the small high signs, ﷺ), which DejaVu Sans
# does not have
QURAN_FONTS = ["Amiri-Regular.ttf", "NotoNaskhArabic[wght].ttf", "DejaVuSans.ttf"]


def _font(lang, size):
    """The subtitle font, laid out by libraqm: Bangla's vowel signs go before their letter and its
    conjuncts join only with it. ffmpeg's subtitle filter here has no shaping, and wrote "অনকে দনি"
    for "অনেক দিন" (2026-10-24). `lang` "quran" is the font for Arabic in another language's line."""
    from PIL import ImageFont
    folder = os.path.expanduser(settings.get("video.fonts.dir", "VIDEO_FONTS_DIR") or "")
    names = QURAN_FONTS if lang == "quran" else (LANGUAGES.get(lang) or LANGUAGES["en"])["fonts"]
    path = next((os.path.join(folder, name) for name in names + ["DejaVuSans.ttf"]
                 if os.path.isfile(os.path.join(folder, name))), os.path.join(folder, "DejaVuSans.ttf"))
    engine = ImageFont.Layout.RAQM if hasattr(ImageFont, "Layout") else None
    font = ImageFont.truetype(path, size, layout_engine=engine)
    # A variable font comes at its default weight, which for Noto Sans JP, KR and SC is Thin: the
    # captions were faint hairlines. Medium reads well with the dark outline.
    try:
        font.set_variation_by_name("Medium")
    except (OSError, ValueError, AttributeError):
        pass                              # not a variable font, or no weight by that name
    return font


def _pieces(text):
    """[(piece, is Arabic)]: a line cut where it changes between Arabic and the rest, each piece to be
    set in its own font and direction."""
    out, at = [], 0
    for match in ARABIC_RUN.finditer(text):
        if match.start() > at:
            out.append((text[at:match.start()], False))
        out.append((match.group(0), True))
        at = match.end()
    if at < len(text):
        out.append((text[at:], False))
    return out


def caption(text, path, lang, title=False, note=False):
    """A transparent frame-sized PNG with the words on it: the title in the middle, a line of
    narration at the bottom - white, outlined in dark, wrapped to fit. A note (a reference shown on the
    dark screen) is set in the middle, smaller and in a soft grey. Arabic in it is set in an Arabic
    font, right to left."""
    from PIL import Image, ImageDraw
    # A long line of a story read word for word is set a little smaller, to cover less of the picture
    size = 54 if title else 30 if note else 34 if len(text) <= 160 else 29
    language = LANGUAGES.get(lang) or LANGUAGES["en"]
    # Arabic in the line: in the language's own font when it is written in Arabic script (Urdu's
    # Nastaliq), otherwise in one with the Quran's letters
    fonts = {False: _font(lang, size),
             True: _font(lang, size) if language.get("arabic") else _font("quran", int(size * 1.15))}
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def pieces(line):
        # A line of Arabic or Urdu is laid out right to left as a whole, its stops and numbers with
        # it: cut into pieces, the full stop was drawn after the Arabic, at its start
        return [(line, True)] if language.get("arabic") else _pieces(line)

    def measure(line):
        return sum(draw.textlength(piece, font=fonts[ar], direction="rtl" if ar else None)
                   for piece, ar in pieces(line))

    # Wrapped at the spaces - or, in Chinese, Japanese and Thai, written without them, at any letter
    # (a mark that sits on a letter stays with it)
    if language.get("spaces", True):
        units, joiner = text.split(), " "
    else:
        units, joiner = [], ""
        for char in text.strip():
            if units and (unicodedata.category(char) in ("Mn", "Mc") or char in "，。、！？」』）,.!?)"):
                units[-1] += char
            else:
                units.append(char)
    lines, line = [], ""
    for unit in units:
        trial = f"{line}{joiner}{unit}".strip() if joiner else line + unit
        if line and measure(trial) > WIDTH - 200:
            lines.append(line.strip())
            line = unit
        else:
            line = trial
    lines += [line.strip()] if line.strip() else []
    step = int(size * language.get("line_height", 1.7 if ARABIC_RUN.search(text) else 1.45))
    height = step * len(lines)
    y = (HEIGHT - height) // 2 if title or note else HEIGHT - 40 - height
    for one in lines:
        x = (WIDTH - measure(one)) / 2
        for piece, ar in pieces(one):
            # On a shared baseline, so the two scripts sit on one line
            draw.text((x, y + size), piece, font=fonts[ar], anchor="ls", direction="rtl" if ar else None,
                      fill=(214, 214, 206, 255) if note else (255, 255, 255, 255),
                      stroke_width=0 if note else 3 if title else 2, stroke_fill=(20, 20, 20, 230))
            x += draw.textlength(piece, font=fonts[ar], direction="rtl" if ar else None)
        y += step
    image.save(path)
    return path


async def _run(args, what):
    _, err, code = await child.run(*args)
    if code != 0:
        log.warning("ffmpeg %s failed: %s", what, err.decode(errors="replace")[-1200:])
        raise VideoError(f"The video could not be put together ({what})")


def as_jpeg(path):
    """A scene's picture, small enough to keep with the conversation."""
    from PIL import Image
    out = io.BytesIO()
    with Image.open(path) as image:
        image.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


async def clean_recording(raw, folder, n):
    """(m4a bytes, seconds) of one scene as the person read it: the silence before and after cut,
    low rumble taken out, and the loudness brought to the same level for every scene."""
    binary = ffmpeg()
    if not binary:
        raise VideoError("No ffmpeg on this server to put the video together")
    source, wav, m4a = (os.path.join(folder, f"read-{n:02d}.{ext}") for ext in ("in", "wav", "m4a"))
    with open(source, "wb") as f:
        f.write(raw)
    # A quarter of a second of the quiet kept at each end: cut at -45 dB with nothing kept, the soft
    # last sound of a line went with the silence
    trim = "silenceremove=start_periods=1:start_duration=0.05:start_threshold=-55dB:start_silence=0.25"
    await _run([binary, "-y", "-i", source, "-af",
                f"highpass=f=70,{trim},areverse,{trim},areverse,loudnorm=I=-18:TP=-2:LRA=9",
                "-ar", "24000", "-ac", "1", wav], f"cleaning up the recording of scene {n + 1}")
    with wave.open(wav, "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    if seconds < 0.5:
        raise VideoError(f"The recording of scene {n + 1} is silent; read it again")
    await _run([binary, "-y", "-i", wav, "-c:a", "aac", "-b:a", "96k", m4a], f"saving the recording of scene {n + 1}")
    with open(m4a, "rb") as f:
        return f.read(), round(seconds, 2)


async def audio_to_m4a(raw, out):
    """Convert raw audio bytes to m4a and return the duration in seconds."""
    source = out + ".in"
    with open(source, "wb") as f:
        f.write(raw)
    await _run([ffmpeg(), "-y", "-i", source, "-c:a", "aac", "-b:a", "96k", "-ar", "24000", "-ac", "1", out], "converting audio")
    with wave.open(out, "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    return round(seconds, 2)


CLIP_VERSION = 1          # raised when the way a clip is made changes, so cached clips are made again
NOTE_BACKGROUND = "0x101216"   # the dark screen a note is shown on


def clip_cache(message_id):
    """Where a video story's finished scene clips are kept between builds, or None."""
    if message_id is None:
        return None
    return os.path.join(settings.PROJECT_ROOT, "state", "video-clips", str(int(message_id)))


async def to_m4a(source, out):
    """A line's narration as it is kept: AAC, a tenth of the WAV's size."""
    await _run([ffmpeg(), "-y", "-i", source, "-c:a", "aac", "-b:a", "96k", out], "keeping the narration")
    with open(out, "rb") as f:
        return f.read()


async def _note_clip(text, length, lang, folder, n, cache, used):
    """One note's clip: its words fading in on the dark screen and out again, in silence. Set in the
    note's own script (a Bangla note in a Bangla font, any other in the Latin one), whichever language's
    video it is in: a reference is shown as it was written."""
    script = "bn" if is_bangla(text) else "en"
    kept = None
    if cache:
        key = hashlib.sha1(json.dumps([CLIP_VERSION, "note", text, round(length, 3), script]).encode()).hexdigest()[:20]
        kept = os.path.join(cache, f"{lang}-note-{key}.mp4")
        used.add(kept)
        if os.path.isfile(kept):
            return kept
    png = caption(text, os.path.join(folder, f"note-{lang}-{n:02d}.png"), script, note=True)
    clip = os.path.join(folder, f"clip-note-{n:02d}.mp4")
    until = length - FADE
    await _run([ffmpeg(), "-y", "-f", "lavfi", "-i", f"color=c={NOTE_BACKGROUND}:s={WIDTH}x{HEIGHT}:r={FPS}:d={length:.3f}",
                "-loop", "1", "-t", f"{length:.3f}", "-i", png, "-filter_complex",
                f"[1:v]format=rgba,fade=t=in:st=0.6:d=0.6:alpha=1,fade=t=out:st={max(0.6, until - 0.6):.2f}:d=0.6:alpha=1[w];"
                f"[0:v][w]overlay=0:0,format=yuv420p[v]", "-map", "[v]", "-t", f"{length:.3f}", "-r", str(FPS),
                "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", clip], "a note")
    if kept:
        shutil.copyfile(clip, kept)
    return clip


async def assemble(pictures, narration, texts, title, lang, folder, recorded=False, keys=None, cache=None,
                   notes=None):
    """The video for one language (`texts` its narration, `title` shown over the first scene): each picture held for its scene with a slow zoom or pan, scenes
    cross-faded, the narration placed at each scene's start, the words faded in over each line as it is spoken. MP4 bytes.

    `recorded` - one flag, or one per scene - marks narration the person read. With `keys` (each
    scene's picture, as kept) and `cache` (clip_cache), a scene whose picture, words and length are
    unchanged reuses the clip made last time: an edited sentence remakes its own scene's clip, not
    every scene's.

    `notes` - for each scene, the notes shown after it (references, footnotes): each on a dark screen
    of its own, in silence, as long as it takes to read; the story's narration is not read over them."""
    binary = ffmpeg()
    if not binary:
        raise VideoError("No ffmpeg on this server to put the video together")
    # What is shown, in order: each scene, then its notes. ("scene", i) or ("note", text)
    items = []
    for i in range(len(pictures)):
        items.append(("scene", i))
        items += [("note", note) for note in ((notes or [])[i] if i < len(notes or []) else []) if note]
    scene_length = scene_lengths([seconds for _, seconds in narration])
    lengths = [scene_length[what] if kind == "scene" else note_seconds(what) + FADE for kind, what in items]
    starts, at = [], 0.0
    for length in lengths:
        starts.append(at)
        at += length - FADE
    total = starts[-1] + lengths[-1]
    scene_starts = [start for (kind, _), start in zip(items, starts) if kind == "scene"]
    if cache:
        os.makedirs(cache, exist_ok=True)
    used = set()

    clips = []
    for n, ((kind, what), length) in enumerate(zip(items, lengths)):
        if kind == "note":
            clips.append(await _note_clip(what, length, lang, folder, n, cache, used))
            continue
        i = what
        png_path, text = pictures[i], texts[i]
        if cache and keys:
            key = hashlib.sha1(json.dumps([CLIP_VERSION, keys[i], text, round(length, 3), i,
                                           title if i == 0 else ""]).encode()).hexdigest()[:20]
            kept = os.path.join(cache, f"{lang}-{i:02d}-{key}.mp4")
            used.add(kept)
            if os.path.isfile(kept):
                clips.append(kept)
                continue
        else:
            kept = None
        frames = int(round(length * FPS))
        # Slow and gentle: in for one scene, out for the next, drifting a little sideways - never more
        # than 8 % over the whole scene, so nothing rushes past a child's eye
        zoom = ("min(1.0+0.08*on/{f},1.08)" if i % 2 == 0 else "max(1.08-0.08*on/{f},1.0)").format(f=frames)
        x = "(iw-iw/zoom)*" + ("on/{f}" if i % 4 < 2 else "(1-on/{f})").format(f=frames)
        # The words go into the scene's own clip: its line while it is spoken, and the title over the
        # first. Laid over the whole video at the end - every caption an input as long as the video -
        # ffmpeg held frames for all of them at once and used 89 GB on a 28-scene story, until the
        # kernel killed it (2026-10-25)
        words = [(caption(text, os.path.join(folder, f"line-{lang}-{i:02d}.png"), lang),
                  LEAD_IN - 0.2, length - FADE)]
        if i == 0:
            words.append((caption(title, os.path.join(folder, f"title-{lang}.png"), lang, title=True),
                          0.4, min(4.5, length - 1.0)))
        inputs = ["-loop", "1", "-t", f"{length:.3f}", "-i", png_path]
        layers = [f"[0:v]scale={WIDTH * 2}:{HEIGHT * 2}:force_original_aspect_ratio=increase,crop={WIDTH * 2}:{HEIGHT * 2},"
                  f"zoompan=z='{zoom}':x='{x}':y='(ih-ih/zoom)/2':d={frames}:s={WIDTH}x{HEIGHT}:fps={FPS},"
                  f"format=yuv420p[s0]"]
        for k, (png, begin, until) in enumerate(words, 1):
            inputs += ["-loop", "1", "-t", f"{length:.3f}", "-i", png]
            layers.append(f"[{k}:v]format=rgba,fade=t=in:st={begin:.2f}:d=0.4:alpha=1,"
                          f"fade=t=out:st={max(begin, until - 0.4):.2f}:d=0.4:alpha=1[w{k}]")
            layers.append(f"[s{k - 1}][w{k}]overlay=0:0:enable='between(t,{begin:.2f},{until:.2f})'[s{k}]")
        clip = os.path.join(folder, f"clip-{i:02d}.mp4")
        await _run([binary, "-y", *inputs, "-filter_complex", ";".join(layers), "-map", f"[s{len(words)}]",
                    "-t", f"{length:.3f}", "-r", str(FPS), "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", clip],
                   f"scene {i + 1}")
        if kept:
            shutil.copyfile(clip, kept)
        clips.append(clip)
    if cache:
        # Only the clips of this build are kept for the next
        for name in os.listdir(cache):
            path = os.path.join(cache, name)
            if name.startswith(f"{lang}-") and path not in used:
                os.remove(path)

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
                "-map", last if chain else "[v0]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *SHAREABLE,
                joined], "joining the scenes")

    # The narration, each line where its scene begins
    audio_inputs, delays = [], []
    # The Bangla voice is bright and a little harsh: its highs softened
    read = recorded if isinstance(recorded, list) else [recorded] * len(narration)
    for i, ((wav, _), start) in enumerate(zip(narration, scene_starts)):
        tone = "highshelf=f=3200:g=-5," if lang == "bn" and not read[i] else ""
        audio_inputs += ["-i", wav]
        ms = int((start + LEAD_IN) * 1000)
        delays.append(f"[{i + 1}:a]{tone}adelay={ms}|{ms},aformat=channel_layouts=stereo[a{i}]")
    mix = ";".join(delays) + ";" + "".join(f"[a{i}]" for i in range(len(narration))) + \
        f"amix=inputs={len(narration)}:normalize=0,apad=whole_dur={total:.3f}[aout]"

    # The words are already in the pictures: the video is copied as it is, with the narration added
    final = os.path.join(folder, f"story-{lang}.mp4")
    await _run([binary, "-y", "-i", joined, *audio_inputs, "-filter_complex", mix, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-t", f"{total:.3f}",
                "-movflags", "+faststart", final],
               "adding the narration")
    with open(final, "rb") as f:
        return f.read(), round(total, 1)


def workspace():
    return tempfile.mkdtemp(prefix="video-story-")
