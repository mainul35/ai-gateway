"""Image URLs in API requests, fetched by the gateway and sent on inline.

An OpenAI request can carry {"type": "image_url", "image_url": {"url": "https://..."}} and a
Messages API one {"type": "image", "source": {"type": "url", "url": "https://..."}}. Passed on as
they are, the model server fetches them itself - from inside this network, where http://127.0.0.1
or http://192.168.50.x is anything on the LAN, with no check at all. So every such URL is fetched
here instead, through app.net (public addresses only, every redirect checked), and replaced with the
picture itself. A URL that cannot be fetched that way fails the request with a 400 saying why.
"""
import base64

import httpx

from app import net

MAX_IMAGES = 8
MAX_BYTES = 10_000_000
TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; ai-gateway/0.1; +https://github.com/mainul35/ai-gateway)"


class ImageRefused(Exception):
    pass


async def _download(session, url):
    try:
        picture = await net.get(session, url, max_bytes=MAX_BYTES)
    except net.Refused as e:
        raise ImageRefused(f"image URL refused ({e}): {url[:200]}")
    except httpx.HTTPError as e:
        raise ImageRefused(f"image URL could not be fetched ({e.__class__.__name__}): {url[:200]}")
    if picture.status_code != 200 or not picture.content_type.startswith("image/"):
        raise ImageRefused(f"image URL did not return an image (HTTP {picture.status_code}, "
                           f"{picture.content_type or 'no type'}): {url[:200]}")
    return picture.content_type, base64.b64encode(picture.content).decode()


def _blocks(content):
    """Every dict block in a message's content, tool results' content included."""
    for block in content if isinstance(content, list) else ():
        if isinstance(block, dict):
            yield block
            yield from _blocks(block.get("content"))


async def inline(messages):
    """Replaces every remote image URL in these messages (both APIs' shapes) with the picture.

    Edits the blocks in place. Data URLs and base64 sources are left as they are.
    """
    remote = []
    for message in messages or ():
        if not isinstance(message, dict):
            continue
        for block in _blocks(message.get("content")):
            if block.get("type") == "image_url":
                target = block.get("image_url")
                url = target.get("url") if isinstance(target, dict) else target
                if isinstance(url, str) and not url.startswith("data:"):
                    remote.append((block, url, "openai"))
            elif block.get("type") == "image" and isinstance(block.get("source"), dict) \
                    and block["source"].get("type") == "url":
                remote.append((block, str(block["source"].get("url") or ""), "anthropic"))
    if not remote:
        return
    if len(remote) > MAX_IMAGES:
        raise ImageRefused(f"at most {MAX_IMAGES} image URLs per request")
    async with net.client(timeout=TIMEOUT, headers={"Accept": "image/*", "User-Agent": USER_AGENT}) as session:
        for block, url, shape in remote:
            mime, data = await _download(session, url)
            if shape == "openai":
                detail = block["image_url"].get("detail") if isinstance(block.get("image_url"), dict) else None
                block["image_url"] = {"url": f"data:{mime};base64,{data}", **({"detail": detail} if detail else {})}
            else:
                block["source"] = {"type": "base64", "media_type": mime, "data": data}
