"""What made each recent answer, put in front of the chat model so it does not have to guess.

Asked "which model did you use to generate the photo?", gemma answered "Imagen, by Google" - for a
picture Qwen-Image-Edit had made on this server's own GPU. The chat model never sees how a picture
was made: in the history a picture is a line like "(make the kid stand on a beach)". So the gateway
records what made each answer when it is saved, and before the next answer it says so plainly.
"""
import json
from pathlib import PurePath

from app import settings

RECENT = 3      # how many of the latest answers are described


def _file(name):
    """A model file as people would recognise it: the name, without folders or extension."""
    return PurePath(name).stem if name else ""


def image_maker(engine, edited, lettering_fixed=False):
    """What made a picture in ComfyUI: "qwen" or "flux" from images.edit_engine()."""
    if edited and engine == "qwen":
        lora = settings.edit_lora()
        made = f"Qwen-Image-Edit ({_file(settings.edit_model())}" \
               + (f", with the {_file(lora)} LoRA" if lora else "") + ")"
        how = "edited from the picture the user attached"
    elif edited:
        made = f"Flux image-to-image ({_file(settings.image_checkpoint())})"
        how = "redrawn from the picture the user attached"
    else:
        made = f"Flux ({_file(settings.image_checkpoint())})"
        how = "drawn from the request"
        if lettering_fixed:
            how += "; its lettering was then corrected by Qwen-Image-Edit"
    return {"tool": "image editing" if edited else "image generation", "model": made,
            "where": "in ComfyUI, on this gateway's own server and GPU", "how": how}


def photo_maker(action, did=""):
    """What worked on a photograph. None of these redraw it."""
    if action == "blur":
        return {"tool": "background blur", "model": f"depth estimation ({_file(settings.depth_model_path())})",
                "where": "on this gateway's own server",
                "how": "each pixel's distance estimated, then blurred more the further away it is; "
                       "nothing redrawn"}
    if action == "backdrop":
        return {"tool": "background colour", "model": f"person cut-out ({_file(settings.cutout_model_path())})",
                "where": "on this gateway's own server",
                "how": "the person cut out and put on a plain colour; the person is not redrawn"}
    models = [_file(settings.denoise_model())]
    if "enlarged" in (did or ""):
        models.append(_file(settings.upscale_model()))
    return {"tool": "photo clean-up", "model": " and ".join(m for m in models if m),
            "where": "in ComfyUI, on this gateway's own server and GPU",
            "how": (did or "cleaned up") + "; nothing in the picture invented"}


def _describe(message):
    """One line about one saved answer, from what was recorded with it."""
    filed = json.loads(message.attachments) if message.attachments else {}
    stats = json.loads(message.stats) if message.stats else {}
    seconds = stats.get("total")
    took = f", took {seconds} s" if seconds else ""
    generated = filed.get("generated")
    if generated:
        maker = generated.get("made_by")
        asked = generated.get("asked") or generated.get("prompt") or ""
        if maker:
            seed = f", seed {generated['seed']}" if generated.get("seed") else ""
            return (f"a picture, by the gateway's {maker['tool']} tool: {maker['model']}, "
                    f"{maker['where']}; {maker['how']}{seed}{took}. Asked for: \"{asked[:200]}\"")
        # Saved before this was recorded: what it was is known, what made it is not
        kind = "an edit of the user's picture" if generated.get("source_file_id") else "a new picture"
        return (f"{kind}, made by the gateway's own image tools on this server (which model was not "
                f"recorded for this one){took}. Asked for: \"{asked[:200]}\"")
    if filed.get("map"):
        return "a map, from the gateway's maps tool"
    subs = filed.get("subtitles")
    if subs:
        language = "translated to English" if subs.get("translated") else f"in language '{subs.get('language')}'"
        return (f"subtitles of the YouTube video \"{subs.get('title')}\" ({subs.get('url')}), {language}, "
                f"transcribed from the video's sound by {subs.get('model', 'Whisper')} on this gateway's own "
                f"GPU - not taken from YouTube's captions - {subs.get('segments')} lines{took}. The transcript "
                f"is that answer's text, above")
    sources = filed.get("sources") or []
    words = f"a written answer by the model {message.model}" if message.model else "a written answer"
    if sources:
        words += f", from a web search with {len(sources)} source(s)"
    if stats.get("tokens"):
        words += f", {stats['tokens']} tokens"
    return words + took


def context_block(messages):
    """The note for the next answer, from the conversation's saved messages, oldest first."""
    answers = [m for m in messages if m.role == "assistant"][-RECENT:]
    if not answers:
        return None
    ago = lambda n: "the latest answer" if n == 1 else f"{n} answers ago"
    lines = [f"- {ago(len(answers) - i)}: {_describe(m)}" for i, m in enumerate(answers)]
    return ("What produced the most recent answers in this conversation, as this gateway recorded it:\n"
            + "\n".join(lines)
            + "\n\nPictures in this conversation were made by the gateway's own tools listed above, not by "
              "you: you write text only. When asked how an answer or picture was made, which model or tool "
              "was used, or how long it took, answer from these facts. If something is not recorded here, "
              "say it was not recorded rather than guessing.")
