"""The voice table of a video story is checked, not trusted.

An invented Kokoro voice name does not fail where it is written: it fails when a person asks for a story,
in a worker process on the server, having loaded a model to get there. So this reads app/tools/voices.py
against what is actually known - Kokoro's own voice files, listed from the model repo by hand on 2026-10-25
- and against what a story needs: that every character of a kind has a voice, and that no two characters
of one story come out as the same speaker, in every one of the eleven languages.

Run by ci/checks.sh; exits non-zero, with what is wrong, when the table is.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.tools import video_story, voices                                  # noqa: E402

# Kokoro-82M's voice files, from https://huggingface.co/hexgrad/Kokoro-82M/tree/main/voices. The leading
# letter is the language code KPipeline is built with: a American English, b British, e Spanish, f French,
# h Hindi, i Italian, j Japanese, p Portuguese, z Mandarin.
KOKORO = {
    "a": "af_alloy af_aoede af_bella af_heart af_jessica af_kore af_nicole af_nova af_river af_sarah af_sky "
         "am_adam am_echo am_eric am_fenrir am_liam am_michael am_onyx am_puck am_santa".split(),
    "b": "bf_alice bf_emma bf_isabella bf_lily bm_daniel bm_fable bm_george bm_lewis".split(),
    "e": "ef_dora em_alex em_santa".split(),
    "f": "ff_siwis".split(),
    "h": "hf_alpha hf_beta hm_omega hm_psi".split(),
    "i": "if_sara im_nicola".split(),
    "j": "jf_alpha jf_gongitsune jf_nezumi jf_tebukuro jm_kumo".split(),
    "p": "pf_dora pm_alex pm_santa".split(),
    "z": "zf_xiaobei zf_xiaoni zf_xiaoxiao zf_xiaoyi zm_yunjian zm_yunxi zm_yunxia zm_yunyang".split(),
}

# A voice turned further than this is another creature, not another character of the same story
LIMITS = (0.88, 1.12)

MALE = ("man", "older man", "boy")
FEMALE = ("woman", "older woman", "girl")


def check():
    bad = []

    # The slots the cast can be given: one per kind, as many as KINDS says
    named = [f"{kind.replace(' ', '-')}-{n}" for kind, count in voices.KINDS.items()
             if kind != "narrator" for n in range(1, count + 1)]
    if set(voices.kinds()) != {"narrator", *named}:
        bad.append(f"the slots are {sorted(voices.kinds())}, not narrator and {' '.join(sorted(named))}")

    # Every Kokoro voice the table names exists, under the code it names
    for lang, pools in voices.POOLS.items():
        for gender, pool in pools.items():
            for code, name in pool:
                if name not in KOKORO.get(code, []):
                    bad.append(f"{lang}: {code}/{name} is not a Kokoro voice ({name} belongs to "
                               f"{', '.join(k for k, v in KOKORO.items() if name in v) or 'no one'})")
                # Kokoro names the gender: am_ and hm_ male, af_ and hf_ female. A woman's voice in the
                # men's pool is a mistake in the table, not a judgement call.
                if name[1] != ("m" if gender == "male" else "f"):
                    bad.append(f"{lang}: the {gender} pool holds {name}, whose name says otherwise")
                # English reaches into its British voices when its American ones run out; nothing else may
                # speak a language by another language's code.
                if code != video_story.LANGUAGES[lang]["voice"][1] and not (lang == "en" and code in ("a", "b")):
                    bad.append(f"{lang}: {code} is not the code this language's narrator speaks")

    # Every character of every language has a voice, no two share one, and none is turned too far
    for lang, language in video_story.LANGUAGES.items():
        narrator = language["voice"]
        filled = voices.plan(lang, narrator)
        missing = [slot for slot in voices.kinds() if slot not in filled]
        if missing:
            bad.append(f"{lang}: no voice for {' '.join(missing)}")
        used = {}
        for slot, voice in filled.items():
            if slot == "narrator":
                continue
            if voice in used:
                bad.append(f"{lang}: {slot} and {used[voice]} would sound identical ({voice})")
            used[voice] = slot
            if narrator[0] == "kokoro" and voice[1] not in KOKORO.get(voice[0], []):
                bad.append(f"{lang}: {slot} asks for {voice[0]}/{voice[1]}, which Kokoro does not have")
            for value in voice[-2:]:
                if not LIMITS[0] <= value <= LIMITS[1]:
                    bad.append(f"{lang}: {slot} is turned {value}, past {LIMITS[0]}..{LIMITS[1]}")

    if bad:
        print("check_voice_table: " + "\n".join(bad))
        return 1
    print(f"check_voice_table: {len(video_story.LANGUAGES)} languages, {len(voices.kinds())} slots, "
          f"no two characters of a story share a voice")
    return 0


if __name__ == "__main__":
    sys.exit(check())
