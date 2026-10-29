"""A person's reading of a line, said again in a machine voice: their words, pronunciation and pace kept,
only the voice changed. For the video studio, where a machine voice cannot say a word as it should be
said (a name, an Arabic term in Bangla), and the person reads the line to show how.

kNN-VC (Baas, van Niekerk and Kamper, Interspeech 2023; github.com/bshall/knn-vc, MIT): each 20 ms of
the reading is described by WavLM-Large (layer 6, which carries the sounds said, not who says them),
replaced by the average of its four nearest frames of the machine voice, and the result turned back
into sound by the HiFi-GAN vocoder trained for it. The more of the machine voice there is to match
against, the better it sounds: a few minutes, kept between jobs in a features file.

Run by app/tools/video_story.py in the narration Python (video.tts.python), on the CPU. Reads one JSON
object on stdin:
  {"source": "reading.wav", "out": "converted.wav", "voice": "voice.pt", "add": ["machine-1.wav", ...],
   "knnvc": "~/knn-vc-src/knn-vc-master"}
every WAV 16 kHz mono. "add" (optional) is more of the machine voice, its features added to the
"voice" file first; with no "source" only that is done. Prints {"seconds": ..., "voice_seconds": ...}.

Needs, besides torch and soundfile (in the narration Python already): the kNN-VC source (its wavlm/
and hifigan/ folders; the rest is not used) and its two checkpoints in torch's hub cache
(~/.cache/torch/hub/checkpoints), from the repository's v0.1 release:
  WavLM-Large.pt (1.26 GB) and prematch_g_02500000.pt (66 MB)
"""
import json
import os
import sys

import numpy as np
import soundfile as sf
import torch

RATE = 16000
LAYER = 6           # WavLM's layer that keeps what is said and loses most of who says it
TOP_K = 4
RELEASE = "https://github.com/bshall/knn-vc/releases/download/v0.1/"


def _checkpoint(name):
    return torch.hub.load_state_dict_from_url(RELEASE + name, map_location="cpu", progress=False)


def _models(source_dir):
    sys.path.insert(0, os.path.expanduser(source_dir))
    from hifigan.models import Generator
    from hifigan.utils import AttrDict
    from wavlm.WavLM import WavLM, WavLMConfig
    checkpoint = _checkpoint("WavLM-Large.pt")
    wavlm = WavLM(WavLMConfig(checkpoint["cfg"]))
    wavlm.load_state_dict(checkpoint["model"])
    wavlm.eval()
    with open(os.path.join(os.path.expanduser(source_dir), "hifigan", "config_v1_wavlm.json")) as f:
        vocoder = Generator(AttrDict(json.load(f)))
    vocoder.load_state_dict(_checkpoint("prematch_g_02500000.pt")["generator"])
    vocoder.eval()
    vocoder.remove_weight_norm()
    return wavlm, vocoder


def _read(path):
    audio, rate = sf.read(path, dtype="float32", always_2d=True)
    if rate != RATE:
        raise SystemExit(f"{path} is {rate} Hz, not {RATE}")
    return torch.from_numpy(audio.mean(axis=1))


@torch.inference_mode()
def _features(wavlm, audio):
    # A minute at a time: WavLM's attention over a whole long reading takes more memory than it needs
    step = RATE * 60
    parts = [wavlm.extract_features(audio[k:k + step][None], output_layer=LAYER, ret_layer_results=False)[0][0]
             for k in range(0, len(audio), step) if len(audio[k:k + step]) >= RATE // 10]
    return torch.cat(parts) if parts else torch.zeros(0, 1024)


def _cosine_distances(query, pool):
    query = torch.nn.functional.normalize(query, dim=-1)
    pool = torch.nn.functional.normalize(pool, dim=-1)
    return 1 - query @ pool.T


@torch.inference_mode()
def convert(wavlm, vocoder, audio, pool):
    query = _features(wavlm, audio)
    out = []
    for k in range(0, len(query), 2000):          # the distances of 2000 frames at a time
        nearest = _cosine_distances(query[k:k + 2000], pool).topk(TOP_K, largest=False, dim=-1).indices
        out.append(pool[nearest].mean(dim=1))
    return vocoder(torch.cat(out)[None]).squeeze().numpy()


def main():
    job = json.load(sys.stdin)
    torch.set_num_threads(max(1, (os.cpu_count() or 2) // 2))   # half the cores: the gateway goes on serving
    wavlm, vocoder = _models(job.get("knnvc") or "~/knn-vc-src/knn-vc-master")
    pool = torch.load(job["voice"]) if os.path.isfile(job["voice"]) else torch.zeros(0, 1024)
    if job.get("add"):
        pool = torch.cat([pool] + [_features(wavlm, _read(path)) for path in job["add"]])
        torch.save(pool, job["voice"] + ".part")
        os.replace(job["voice"] + ".part", job["voice"])
    result = {"voice_seconds": round(len(pool) * 0.02, 1)}
    if job.get("source"):
        if not len(pool):
            raise SystemExit("no machine voice to match against")
        audio = convert(wavlm, vocoder, _read(job["source"]), pool)
        sf.write(job["out"], np.clip(audio, -1, 1), RATE)
        result["seconds"] = round(len(audio) / RATE, 2)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
