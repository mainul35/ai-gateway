"""Narration for the playground's video stories: text in, a WAV file per line out.

Run by app/tools/video_story.py in its own Python (settings video.tts.python, a 3.12 environment:
Kokoro's English text handling does not run on the gateway's 3.14). Reads one JSON object on stdin,
{"lang": "en" | "bn", "items": [{"text", "out"}], "speed": 0.92}, writes each WAV, and prints
{"durations": [seconds, ...]} on stdout. Everything runs on the CPU; the GPUs are left to the models.

  English  Kokoro-82M (hexgrad), voice am_michael: a calm male narrator
  Bangla   Meta's MMS-TTS for Bengali (facebook/mms-tts-ben)

The environment on homelabai (~/tts-venv, about 1.5 GB), made with uv:
  uv venv -p 3.12 ~/tts-venv
  uv pip install -p ~/tts-venv/bin/python --index-url https://download.pytorch.org/whl/cpu torch
  uv pip install -p ~/tts-venv/bin/python kokoro soundfile transformers imageio-ffmpeg
  uv pip install -p ~/tts-venv/bin/python <en_core_web_sm-3.8.0 wheel from spaCy's GitHub releases>
The fonts for the words on screen go in video.fonts.dir (~/fonts): NotoSansBengali.ttf, DejaVuSans.ttf.
"""
import json
import re
import sys

import numpy as np
import soundfile as sf

PAUSE_SECONDS = 0.35            # between the sentences of one line
CLAUSE_SECONDS = 0.15           # at a comma, in Bangla
# MMS's sampling noise: its defaults (0.667, 0.8) make a rough voice and uneven timing that runs words
# together. Lower is steadier and clearer.
NOISE, NOISE_DURATION = 0.45, 0.5


def english(items, speed):
    from kokoro import KPipeline
    pipeline = KPipeline(lang_code="a", repo_id="hexgrad/Kokoro-82M")
    durations = []
    for item in items:
        try:
            pieces = [np.asarray(audio, dtype=np.float32)
                      for _, _, audio in pipeline(item["text"], voice="am_michael", speed=speed)]
        except Exception as e:            # one line the voice cannot say is silence, not a lost video
            print(f"line not spoken: {e}: {item['text'][:80]!r}", file=sys.stderr)
            pieces = []
        audio = np.concatenate(pieces) if pieces else np.zeros(2400, dtype=np.float32)
        sf.write(item["out"], audio, 24000)
        durations.append(len(audio) / 24000)
    return durations


def bangla(items, speed, noise=NOISE, noise_duration=NOISE_DURATION):
    import torch
    from transformers import AutoTokenizer, VitsModel
    model = VitsModel.from_pretrained("facebook/mms-tts-ben")
    tokenizer = AutoTokenizer.from_pretrained("facebook/mms-tts-ben")
    model.speaking_rate = speed
    model.noise_scale = noise
    model.noise_scale_duration = noise_duration
    rate = model.config.sampling_rate
    pause = np.zeros(int(rate * PAUSE_SECONDS), dtype=np.float32)
    breath = np.zeros(int(rate * CLAUSE_SECONDS), dtype=np.float32)
    torch.manual_seed(0)                 # the same line read the same way every time
    durations = []
    for item in items:
        # One sentence at a time: the model reads a long line less clearly than several short ones
        sentences = [s.strip() for s in re.split(r"(?<=[।!?.])\s+", item["text"]) if s.strip()]
        parts = []
        for sentence in sentences:
            # A breath at each comma: read in one go, the clauses ran into each other
            clauses = [c.strip() for c in re.split(r"(?<=[,;])\s+", sentence) if c.strip()]
            for k, clause in enumerate(clauses):
                inputs = tokenizer(clause, return_tensors="pt")
                # Text with nothing in Bangla it can read - Latin letters ("Wa iz qaala rabbuka"), a lone
                # bracket, "..." - comes out as no tokens at all, and the model fails on it ("narrow():
                # length must be non-negative"): that ended a 28-scene video after its last picture
                if inputs["input_ids"].shape[-1] < 2:
                    continue
                try:
                    with torch.no_grad():
                        parts.append(model(**inputs).waveform[0].numpy().astype(np.float32))
                except Exception as e:
                    print(f"clause not spoken: {e}: {clause[:80]!r}", file=sys.stderr)
                    continue
                parts.append(breath if k < len(clauses) - 1 else pause)
        audio = np.concatenate(parts[:-1]) if parts else np.zeros(rate // 10, dtype=np.float32)
        sf.write(item["out"], audio, rate)
        durations.append(len(audio) / rate)
    return durations


def main():
    job = json.load(sys.stdin)
    speed = float(job.get("speed") or 0.92)
    items = job.get("items") or []
    if job.get("lang") == "bn":
        durations = bangla(items, speed, float(job.get("noise") or NOISE),
                           float(job.get("noise_duration") or NOISE_DURATION))
    else:
        durations = english(items, speed)
    print(json.dumps({"durations": durations}))


if __name__ == "__main__":
    main()
