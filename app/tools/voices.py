"""The voices a story's characters get: one per character, from what the speech models really have.

A video story already knows who speaks - a cast, each member a kind (man, older woman, boy) and a voice
slot, so that two men in one story never share a voice. `KINDS` is how many slots each kind has; this
module turns a slot into an actual voice, which is the half that was missing.

Gender-matched voices come first, from Kokoro's own inventory. Its voice names are checked against the
model repo by scripts/check_voice_table.py, because an invented one fails only when a story is narrated.
Where a language has fewer voices of a kind than the story has characters - French has one voice in total,
and Bangla, Korean, Thai, Arabic and Urdu have one each from their MMS-TTS checkpoint - the character is
that voice shifted a little in pitch and in pace. A shift is never more than 12 %: enough for a child to
hear that the woodcutter is not the shepherd, short of a chipmunk, and the Videos page says which of a
story's characters are the language's one voice rather than voices the model has.
"""

# The kinds of speaker, and how many distinct voices each has, so that two men in a story never sound
# alike. The narrator is one voice; the words of Allah, of a prophet or of an angel are always given to
# the narrator, never to a character (app/tools/video_story.py asks the model for exactly that).
KINDS = {"narrator": 1, "man": 3, "older man": 2, "woman": 3, "older woman": 2, "boy": 2, "girl": 2}

# The slots of each gender, in the order the characters of a story take them. An older voice comes after
# a plain one of the same kind, and a child last, so a story of two men gets "man-1" and "man-2" and the
# shifting starts only past what the language can give out.
MALE_SLOTS = ["man-1", "man-2", "man-3", "older-man-1", "older-man-2", "boy-1", "boy-2"]
FEMALE_SLOTS = ["woman-1", "woman-2", "woman-3", "older-woman-1", "older-woman-2", "girl-1", "girl-2"]

# Kokoro's voices, by language and gender, in the order described above. A language's list stops where the
# model does: it has one French voice (ff_siwis) and one male Japanese voice (jm_kumo), and no child voices
# at all, which is why a boy or a girl is a grown voice of their gender, pitched up a little.
# Each entry is (Kokoro's language code, voice name); the code is what KPipeline is built with, and
# English reaches into its British set when the American one runs out.
POOLS = {
    "en": {
        "male": [("a", "am_michael"), ("a", "am_eric"), ("a", "am_adam"), ("a", "am_fenrir"),
                 ("a", "am_onyx"), ("a", "am_santa"), ("a", "am_liam"), ("a", "am_echo")],
        "female": [("a", "af_nicole"), ("a", "af_sarah"), ("a", "af_sky"), ("b", "bf_emma"),
                   ("b", "bf_lily"), ("a", "af_jessica"), ("a", "af_aoede")],
    },
    "es": {"male": [("e", "em_alex"), ("e", "em_santa")], "female": [("e", "ef_dora")]},
    "fr": {"male": [], "female": [("f", "ff_siwis")]},
    "hi": {"male": [("h", "hm_omega"), ("h", "hm_psi")], "female": [("h", "hf_alpha"), ("h", "hf_beta")]},
    "zh": {"male": [("z", "zm_yunjian"), ("z", "zm_yunxi"), ("z", "zm_yunxia"), ("z", "zm_yunyang")],
           "female": [("z", "zf_xiaobei"), ("z", "zf_xiaoni"), ("z", "zf_xiaoxiao"), ("z", "zf_xiaoyi")]},
    "ja": {"male": [("j", "jm_kumo")],
           "female": [("j", "jf_alpha"), ("j", "jf_gongitsune"), ("j", "jf_nezumi"), ("j", "jf_tebukuro")]},
}

# The pitch and pace of a character past the voices a language has. Deep ones first for the men, high
# ones first for the women, so the first two of a kind differ from each other by ear and the last is
# still recognisably the language's own voice. No pair is repeated, so no two characters of a story share
# a voice even when they are all shifts of one.
SHIFTS_MALE = [(0.92, 1.0), (0.88, 0.96), (0.96, 0.92), (1.0, 0.9), (1.0, 1.08), (1.04, 1.0)]
SHIFTS_FEMALE = [(1.08, 1.0), (1.12, 1.04), (1.04, 0.92), (1.0, 1.1), (1.06, 0.9), (1.08, 1.12)]
SHIFTS_YOUNG = [(1.12, 1.08), (1.07, 1.12)]
# A girl's own, higher and quicker than any adult of her gender in the same language, so that in a language
# with one voice a girl and a boy of the same shift still do not come out as the same speaker.
SHIFTS_YOUNG_F = [(1.1, 1.12), (1.12, 1.1)]

# A boy and a girl, pitched up a little and read a shade quicker, whether or not the language had a voice
# spare for them: Kokoro has no child voices, and nothing else comes close to one.
YOUNG = (1.07, 1.06)
YOUNG_F = (1.1, 1.12)


# The voice a language is narrated in by default - what a story's narrator gets, and what scripts/
# tts_worker.py speaks a part with when the character's own voice asks for the language's. It lives
# here and not in app/tools/video_story.py, because the voice table is what owns the voices, and this
# module must stay importable by scripts/check_voice_table.py in a Python with none of the gateway's
# other packages.
NARRATORS = {
    "en": ["kokoro", "a", "am_michael"],
    "bn": ["mms", "facebook/mms-tts-ben"],
    "es": ["kokoro", "e", "em_alex"],
    "fr": ["kokoro", "f", "ff_siwis"],
    "zh": ["kokoro", "z", "zm_yunjian"],
    "ja": ["kokoro", "j", "jm_kumo"],
    "ko": ["mms", "facebook/mms-tts-kor"],
    "th": ["mms", "facebook/mms-tts-tha"],
    "ar": ["mms", "facebook/mms-tts-ara"],
    "ur": ["mms", "facebook/mms-tts-urd-script_arabic"],
    "hi": ["kokoro", "h", "hm_omega"],
}


def narrator(lang):
    """The language's own machine voice, in the shape scripts/tts_worker.py reads it."""
    return NARRATORS[lang]


def kinds():
    """Every voice slot a story can use: "narrator", and "<kind with a dash>-<n>" for the rest."""
    return ["narrator"] + [f"{kind.replace(' ', '-')}-{n}"
                           for kind, count in KINDS.items() if kind != "narrator" for n in range(1, count + 1)]


def plan(lang, narrator):
    """Every slot of a language: the voice that fills it, and how far it is shifted.

    `narrator` is the language's own machine voice as app/tools/video_story.py keeps it - ["kokoro", "a",
    "am_michael"] or ["mms", "facebook/mms-tts-ben"] - which is also what the narrator of a story gets
    unshifted. Returns {slot: (code, name, pitch, tempo)} for Kokoro and {slot: (model, pitch, tempo)} for
    MMS; the caller passes the voice to scripts/tts_worker.py and applies the shift to its audio with
    ffmpeg. A slot is "shifted" when pitch or tempo is not 1: the Videos page says so of a character whose
    voice is the language's own one, turned rather than a voice the model has.
    """
    pools = POOLS.get(lang) or {}
    # The narrator is the language's own voice, unturned: it is what a story was narrated by before any
    # character had a voice of their own, and what a person hears when they reopen an old story.
    out = {"narrator": (*narrator[1:], 1.0, 1.0) if narrator[0] == "kokoro" else (narrator[1], 1.0, 1.0)}
    if narrator[0] == "mms":
        # One voice in the whole language, so every character is it, turned: Bangla, Thai, Arabic, Urdu
        # and Korean have no second voice to reach for, and each MMS-TTS checkpoint carries one speaker.
        for slots, ladder, young in ((MALE_SLOTS, SHIFTS_MALE, SHIFTS_YOUNG),
                                     (FEMALE_SLOTS, SHIFTS_FEMALE, SHIFTS_YOUNG_F)):
            for i, slot in enumerate(slots):
                chosen = young if slot.startswith(("boy", "girl")) else ladder
                pitch, tempo = chosen[i % len(chosen)]
                out[slot] = (narrator[1], pitch, tempo)
        return out
    used = {(narrator[1], narrator[2])}                     # the narrator's voice, taken by the narrator
    for slots, gender in ((MALE_SLOTS, "male"), (FEMALE_SLOTS, "female")):
        pool = pools.get(gender) or []
        free = [voice for voice in pool if voice not in used]
        for i, slot in enumerate(slots):
            child = slot.startswith(("boy", "girl"))
            if i < len(free):
                code, name = free[i]
                pitch, tempo = (YOUNG_F if gender == "female" else YOUNG) if child else (1.0, 1.0)
            else:
                code, name = (pool or [(narrator[1], narrator[2])])[0]
                chosen = (SHIFTS_YOUNG_F if gender == "female" else SHIFTS_YOUNG) if child else \
                         (SHIFTS_FEMALE if gender == "female" else SHIFTS_MALE)
                pitch, tempo = chosen[i % len(chosen)]
            used.add((code, name, pitch, tempo))
            out[slot] = (code, name, pitch, tempo)
    return out


def spec(lang, slot, narrator):
    """One character's voice: (code, name, pitch, tempo), or (model, pitch, tempo) for MMS."""
    return plan(lang, narrator)[slot]


def job_voice(lang, slot, narrator):
    """The voice of one character as scripts/tts_worker.py reads it: the shape a whole job carries today
    (["kokoro", code, name] or ["mms", model]), so an older worker still speaks - in one voice, and says
    which it did."""
    filled = plan(lang, narrator)[slot]
    return [narrator[0], filled[0], filled[1]] if narrator[0] == "kokoro" else [narrator[0], filled[0]]


def retune(pitch, tempo):
    """The ffmpeg filter that turns one voice into a character: asetrate lifts or drops the pitch, and
    atempo gives back the pace the pitch change took away, then the character's own of it. Both speak
    at 24 kHz, so the sample rate is written out rather than guessed at."""
    if (pitch, tempo) == (1.0, 1.0):
        return ""
    return f"asetrate=24000*{pitch:.3f},resample=24000,atempo={tempo / pitch:.4f}"


def shifted(lang, slot, narrator):
    """Whether a character's voice is the language's own, turned, rather than one the model has."""
    filled = plan(lang, narrator)[slot]
    return filled[-2:] != (1.0, 1.0)
