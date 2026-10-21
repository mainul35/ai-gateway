"""Files from Hugging Face to this machine's disk, for llama.cpp: resumable, and patient with the line.

Ollama's own pull gave up on a 32 GB model twice (2026-10-19, -20) when one of its parts failed five
times, and deleted what it had. Here every file goes to <name>.part and continues from its last byte
however often the connection drops, until it is complete; only then is it renamed. A file already
complete on disk is not fetched again, so a download resumed after a restart picks up where it was.

download() is a blocking generator of progress events, as utils.ollama_client's streams are:
{"status", "completed", "total"}, then {"status": "success"}, or {"error"} / {"cancelled": True}.
"""
import os
import shutil
import time

import requests

from utils import hf_client

CHUNK = 1 << 20
GIVE_UP_AFTER = 30 * 60      # seconds without a single byte arriving
PROGRESS_EVERY = 1.0         # seconds between progress events


def download(model_id, files, folder, cancellation, sizes=None):
    """Fetches `files` (paths in the repository) into `folder`, each under its own base name."""
    sizes = dict(sizes or {})
    os.makedirs(folder, exist_ok=True)
    total = sum(sizes.get(f, 0) for f in files)
    needed = sum(max(0, sizes.get(f, 0) - _have(folder, f)) for f in files)
    free = shutil.disk_usage(folder).free
    if needed > free * 0.98:
        yield {"error": f"Not enough disk space: {needed / 1e9:.1f} GB to download, {free / 1e9:.1f} GB free"}
        return
    done_before = 0
    for path in files:
        for event in _one(model_id, path, folder, sizes.get(path, 0), cancellation):
            if "error" in event or event.get("cancelled"):
                yield event
                return
            yield {"status": f"downloading {os.path.basename(path)}",
                   "completed": done_before + event["completed"], "total": total or None}
        done_before += sizes.get(path, 0) or _have(folder, path)
    yield {"status": "success"}


def _have(folder, path):
    target = os.path.join(folder, os.path.basename(path))
    if os.path.exists(target):
        return os.path.getsize(target)
    return os.path.getsize(target + ".part") if os.path.exists(target + ".part") else 0


def _one(model_id, path, folder, size, cancellation):
    target = os.path.join(folder, os.path.basename(path))
    part = target + ".part"
    if os.path.exists(target) and (not size or os.path.getsize(target) == size):
        yield {"completed": os.path.getsize(target)}
        return
    url = f"{hf_client.HF_BASE}/{model_id}/resolve/main/{path}"
    last_byte_at, last_event, wait = time.monotonic(), 0.0, 2
    while True:
        if cancellation.cancelled:
            yield {"cancelled": True}
            return
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size and have >= size:
            break
        try:
            headers = {**hf_client._headers(), **({"Range": f"bytes={have}-"} if have else {})}
            with requests.get(url, headers=headers, stream=True, timeout=(15, 60), allow_redirects=True) as r:
                if r.status_code == 416:            # asked past the end: the part is whole
                    break
                if r.status_code in (401, 403, 404):
                    yield {"error": f"Hugging Face refused {path}: HTTP {r.status_code}"}
                    return
                r.raise_for_status()
                if have and r.status_code == 200:   # the server ignored the range: start over
                    have = 0
                if not size:
                    size = have + int(r.headers.get("Content-Length") or 0)
                with open(part, "ab" if have else "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if cancellation.cancelled:
                            yield {"cancelled": True}
                            return
                        if not chunk:
                            continue
                        f.write(chunk)
                        have += len(chunk)
                        last_byte_at, wait = time.monotonic(), 2
                        if time.monotonic() - last_event >= PROGRESS_EVERY:
                            last_event = time.monotonic()
                            yield {"completed": have}
            if not size or have >= size:
                break
        except (requests.RequestException, OSError):
            pass                                    # dropped: go round again from the last byte
        if time.monotonic() - last_byte_at > GIVE_UP_AFTER:
            yield {"error": f"No data from Hugging Face for {GIVE_UP_AFTER // 60} minutes while downloading {path}"}
            return
        for _ in range(wait):                       # waits that a cancel can cut short
            if cancellation.cancelled:
                yield {"cancelled": True}
                return
            time.sleep(1)
        wait = min(wait * 2, 60)
    os.replace(part, target)
    yield {"completed": os.path.getsize(target)}
