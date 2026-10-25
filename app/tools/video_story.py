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
from app.tools import child, quran

log = logging.getLogger("tools.video_story")

WIDTH, HEIGHT, FPS = 1280, 720, 25
SCENE_MIN = 8.0                       # seconds a scene stays at least: slow enough for a child to take it in
FADE = 1.5                            # seconds two scenes overlap
LEAD_IN = 0.6                         # silence before a scene's narration starts
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
  moment. Say who is in it and how many (one man, not "people"), what they are doing (swinging an
  axe at a tree trunk, carrying logs, talking with his employer), where, and the objects that matter,
  and the shot that shows it best (a medium shot from behind as he swings the axe; a close view of a
  blunt axe on a stump). Describe every person in full, the same way, in every scene they are in -
  first decide each one's look (the woodcutter: a strong young man in a brown tunic and a white cap;
  his employer: an older bearded man in a grey robe) and repeat it word for word every time, never
  just "the employer" - so the same person appears throughout. A spoken line shows the speaker and the
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
# where it has one) or one of Meta's MMS voices. "spaces": False for a script written without spaces
# between words, wrapped at any letter instead; "arabic": its own script is Arabic, kept and spoken.
LANGUAGES = {
    "en": {"name": "English", "fonts": ["DejaVuSans.ttf"], "voice": ["kokoro", "a", "am_michael"], "speed": 0.92},
    "bn": {"name": "Bangla", "fonts": ["NotoSansBengali.ttf"], "voice": ["mms", "facebook/mms-tts-ben"], "speed": 0.85},
    "es": {"name": "Spanish", "fonts": ["DejaVuSans.ttf"], "voice": ["kokoro", "e", "em_alex"], "speed": 0.92},
    "fr": {"name": "French", "fonts": ["DejaVuSans.ttf"], "voice": ["kokoro", "f", "ff_siwis"], "speed": 0.92},
    "zh": {"name": "Chinese", "fonts": ["NotoSansSC[wght].ttf"], "voice": ["kokoro", "z", "zm_yunjian"],
           "speed": 0.92, "spaces": False},
    "ja": {"name": "Japanese", "fonts": ["NotoSansJP[wght].ttf"], "voice": ["kokoro", "j", "jm_kumo"],
           "speed": 0.92, "spaces": False},
    "ko": {"name": "Korean", "fonts": ["NotoSansKR[wght].ttf"], "voice": ["mms", "facebook/mms-tts-kor"], "speed": 0.9},
    "th": {"name": "Thai", "fonts": ["NotoSansThai[wdth,wght].ttf"], "voice": ["mms", "facebook/mms-tts-tha"],
           "speed": 0.9, "spaces": False},
    "ar": {"name": "Arabic", "fonts": ["Amiri-Regular.ttf", "NotoNaskhArabic[wght].ttf"],
           "voice": ["mms", "facebook/mms-tts-ara"], "speed": 0.9, "arabic": True},
    "ur": {"name": "Urdu", "fonts": ["NotoNastaliqUrdu[wght].ttf", "Amiri-Regular.ttf"],
           "voice": ["mms", "facebook/mms-tts-urd-script_arabic"], "speed": 0.9, "arabic": True},
    "hi": {"name": "Hindi", "fonts": ["NotoSansDevanagari[wdth,wght].ttf"], "voice": ["kokoro", "h", "hm_omega"],
           "speed": 0.92},
}


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
TURN_AWAY = ("Turn every person so that their head faces completely away from the viewer: only the back "
             "of the head and hair or cap can be seen, no face, no cheek, no beard, no profile. Keep their "
             "clothes, pose, the objects and everything else in the picture exactly as it is.")
WOMAN = re.compile(r"\b(wom[ae]n|girls?|mother|wife|daughter|sister|lady|ladies|queen|grandmother)\b", re.I)

CHECK_PROMPT = """You check one picture for a children's video against strict rules. Look carefully.
The picture was drawn for this scene: {scene}
Answer with JSON only:
{"faces": true if ANY human face is visible - front, side or partly, even small or stylised - and that
   includes faces or heads in a painting, portrait, framed picture, statue, carving or silhouette
   that is part of the scene,
 "people": the number of human figures in the picture, counting people shown in paintings, framed
   pictures, statues or silhouettes,
 "immodest": true if any person shows uncovered arms, legs, hair of a woman, or tight clothing,
 "text": true if there is any writing or lettering in the picture,
 "frightening": true if anything violent, bloody or scary is shown,
 "matches": true if the picture shows what the scene describes - its main subject, what is happening
   and where - false if it shows something else (other people, another action, another place)}"""


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


async def narrate(lang, texts, folder):
    """[(wav path, seconds)] for each text, spoken in English ("en") or Bangla ("bn"). A line all in
    Arabic is silence, held for reading; a line with some Arabic in it is held that much longer."""
    said = [spoken(t, lang) for t in texts]
    items, results = [], [None] * len(texts)
    for i, (words, unspoken) in enumerate(said):
        out = os.path.join(folder, f"{lang}-{i:02d}.wav")
        if re.search(r"\w", words):
            items.append({"text": words, "out": out, "i": i})
        else:
            results[i] = held_silence(texts[i], lang, out)
    if items:
        language = LANGUAGES.get(lang) or LANGUAGES["en"]
        # The language's own pace: Bangla a little slower, or the voice runs words together
        job = {"lang": lang, "voice": language["voice"], "speed": language["speed"],
               "items": [{"text": it["text"], "out": it["out"]} for it in items]}
        out, err, code = await child.run(_python(), _worker(), stdin=json.dumps(job).encode(),
                                         stdout=asyncio.subprocess.PIPE)
        if code != 0:
            log.warning("narration in %s failed: %s", lang, err.decode(errors="replace")[-800:])
            raise VideoError(f"The {language_name(lang)} narration could not be made")
        durations = json.loads(out.decode().strip().splitlines()[-1])["durations"]
        for item, seconds in zip(items, durations):
            results[item["i"]] = (item["out"], seconds + READ_SECONDS * said[item["i"]][1])

    for i, text in enumerate(texts):
        path, seconds = results[i]
        results[i] = await with_recitation(text, lang, path if re.search(r"\w", said[i][0]) else None,
                                           seconds, folder, f"{lang}-{i:02d}") or results[i]
    return results


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
    return ImageFont.truetype(path, size, layout_engine=engine)


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


def caption(text, path, lang, title=False):
    """A transparent frame-sized PNG with the words on it: the title in the middle, a line of
    narration at the bottom - white, outlined in dark, wrapped to fit. Arabic in it is set in an
    Arabic font, right to left."""
    from PIL import Image, ImageDraw
    # A long line of a story read word for word is set a little smaller, to cover less of the picture
    size = 54 if title else 34 if len(text) <= 160 else 29
    language = LANGUAGES.get(lang) or LANGUAGES["en"]
    # Arabic in the line: in the language's own font when it is written in Arabic script (Urdu's
    # Nastaliq), otherwise in one with the Quran's letters
    fonts = {False: _font(lang, size),
             True: _font(lang, size) if language.get("arabic") else _font("quran", int(size * 1.15))}
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def measure(line):
        return sum(draw.textlength(piece, font=fonts[ar], direction="rtl" if ar else None)
                   for piece, ar in _pieces(line))

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
    step = int(size * (1.7 if ARABIC_RUN.search(text) else 1.45))
    height = step * len(lines)
    y = (HEIGHT - height) // 2 if title else HEIGHT - 40 - height
    for one in lines:
        x = (WIDTH - measure(one)) / 2
        for piece, ar in _pieces(one):
            # On a shared baseline, so the two scripts sit on one line
            draw.text((x, y + size), piece, font=fonts[ar], anchor="ls", direction="rtl" if ar else None,
                      fill=(255, 255, 255, 255), stroke_width=3 if title else 2, stroke_fill=(20, 20, 20, 230))
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


CLIP_VERSION = 1          # raised when the way a clip is made changes, so cached clips are made again


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


async def assemble(pictures, narration, texts, title, lang, folder, recorded=False, keys=None, cache=None):
    """The video for one language (`texts` its narration, `title` shown over the first scene): each picture held for its scene with a slow zoom or pan, scenes
    cross-faded, the narration placed at each scene's start, the words faded in over each line as it is spoken. MP4 bytes.

    `recorded` - one flag, or one per scene - marks narration the person read. With `keys` (each
    scene's picture, as kept) and `cache` (clip_cache), a scene whose picture, words and length are
    unchanged reuses the clip made last time: an edited sentence remakes its own scene's clip, not
    every scene's."""
    binary = ffmpeg()
    if not binary:
        raise VideoError("No ffmpeg on this server to put the video together")
    lengths = scene_lengths([seconds for _, seconds in narration])
    starts, at = [], 0.0
    for length in lengths:
        starts.append(at)
        at += length - FADE
    total = starts[-1] + lengths[-1]
    if cache:
        os.makedirs(cache, exist_ok=True)
    used = set()

    clips = []
    for i, (png_path, length, text) in enumerate(zip(pictures, lengths, texts)):
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
                "-map", last if chain else "[v0]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", joined],
               "joining the scenes")

    # The narration, each line where its scene begins
    audio_inputs, delays = [], []
    # The Bangla voice is bright and a little harsh: its highs softened
    read = recorded if isinstance(recorded, list) else [recorded] * len(narration)
    for i, ((wav, _), start) in enumerate(zip(narration, starts)):
        tone = "highshelf=f=3200:g=-5," if lang == "bn" and not read[i] else ""
        audio_inputs += ["-i", wav]
        ms = int((start + LEAD_IN) * 1000)
        delays.append(f"[{i + 1}:a]{tone}adelay={ms}|{ms},aformat=channel_layouts=stereo[a{i}]")
    mix = ";".join(delays) + ";" + "".join(f"[a{i}]" for i in range(len(narration))) + \
        f"amix=inputs={len(narration)}:normalize=0,apad=whole_dur={total:.3f}[aout]"

    # The words are already in the pictures: the video is copied as it is, with the narration added
    final = os.path.join(folder, f"story-{lang}.mp4")
    await _run([binary, "-y", "-i", joined, *audio_inputs, "-filter_complex", mix, "-map", "0:v", "-map", "[aout]",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-t", f"{total:.3f}", "-movflags", "+faststart", final],
               "adding the narration")
    with open(final, "rb") as f:
        return f.read(), round(total, 1)


def workspace():
    return tempfile.mkdtemp(prefix="video-story-")
