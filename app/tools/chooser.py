"""Picking the model to answer with, once the kind of work is known.

The playground has always sent whatever model the drop-down was showing, which means the 31B vision
model answers "what is 17 times 23" and the 1.7B router model gets asked to write a class. Knowing
what the message is asking for - that much is the router's job - only helps if something then acts
on it, and that is this.

Nothing here is a judgement about which model is cleverer. It works from what each model reports
about itself (what it can do, how big it is, how much it can read at once) and from what this gateway
has watched it do (how often it failed, how long it takes), because those are the two things that can
be known rather than guessed. A model that has been failing is not the best model for anything.
"""
import logging
import re

from app import settings

log = logging.getLogger("tools.chooser")

# What a message is asking for. The router names one of these; the scoring below turns it into a model.
# "light" is small talk - thanks, a joke, a quick rewrite - and is the one kind given to the smallest
# model; "general" is a real question that needs knowledge, which a 1.7B answers badly.
CATEGORIES = ("code", "vision", "reasoning", "long", "general", "light")

CODE_NAME = re.compile(r"coder|code|starcoder|deepseek-?c", re.I)
# A model whose name says it is for one narrow thing is a poor default for everything else
# The image models Ollama lists (Qwen-Image's GGUF says "completion") are not for conversation either
SPECIALIST = re.compile(r"embed|rerank|guard|shield|whisper|tts|image|diffusion|flux", re.I)

BROKEN_SHARE = 0.5      # failing this often, with enough attempts to mean it, is broken
ENOUGH_TRIES = 2
SLOW_MS = 60_000
UNKNOWN_SIZE = 30.0   # billions; what a model is taken to be when nothing says        # a minute per answer is worth a point against, not a disqualification


def _parameters(model):
    """Billions of parameters, as a number. "31.3B" -> 31.3, "596.05M" -> 0.6.

    From what the model reports, else from its name (qwen3-next-80b, gemma4:31b, qwen3:1.7b): our
    llama.cpp profiles report nothing. UNKNOWN_SIZE when neither says, so that a model of unknown
    size never passes for the smallest.
    """
    text = (model.get("parameter_size") or "").strip().upper()
    found = re.match(r"([\d.]+)\s*([BMT])?", text)
    if not found:
        # Millions only with three digits or more ("596m"): "qwen3.6-1m" is a 1M window, not a size
        named = re.search(r"(?<![\d.])(?:(\d+(?:\.\d+)?)b|(\d{3,})m)(?![a-z])", (model.get("name") or "").lower())
        if not named:
            return UNKNOWN_SIZE
        return float(named.group(1)) if named.group(1) else float(named.group(2)) / 1000
    try:
        size = float(found.group(1))
    except ValueError:
        return UNKNOWN_SIZE
    return {"M": size / 1000, "T": size * 1000}.get(found.group(2), size)


def _reliability(health):
    """How much this model's own record argues for or against using it now."""
    requests = (health or {}).get("requests") or 0
    failures = (health or {}).get("failures") or 0
    if requests >= ENOUGH_TRIES and failures / requests >= BROKEN_SHARE:
        return None            # not a score: a refusal
    score = 0.0
    if requests and not failures:
        score += 0.5           # it has been asked and it has answered
    elif failures:
        score -= 1.0
    if (health or {}).get("avg_latency_ms", 0) > SLOW_MS:
        score -= 1.0
    return score


def is_helper(name):
    """One of the gateway's own helper models: the router and the embedder, run on the CPU.

    They are there to read every message and index documents, not to answer: Auto gave the CPU
    copy of the 1.7B router a question about mixing a 3090 and a 2080 Ti in one machine.
    """
    return (name or "").removesuffix(":latest") in {h.removesuffix(":latest") for h in helper_names()}


def helper_names():
    """The helper models' names as configured, for the playground's own Auto fallback to skip."""
    return sorted(n for n in (settings.router_model(), settings.get("knowledge.embedding.model")) if n)


ENOUGH_VOTES = 3       # verdicts before they count; fewer is one person's bad day


def judged(feedback, category):
    """What this person said about the model's answers, as points: from -1.4 to +0.6.

    The playground asks "Helpful?"; the "Accurate?" question it asked before 2026-10-19 is gone, but
    the verdicts given to it still count, as the same kind of vote. For this kind of question when
    there are enough verdicts on it, otherwise over all questions. Centred on 70% good, so a model
    has to be marked good most of the time to gain, and one marked bad half the time loses more than
    any small-model bonus is worth.
    """
    for counts in ((feedback or {}).get(category), (feedback or {}).get("all")):
        counts = counts or {}
        good = counts.get("helpful", 0) + counts.get("accurate", 0)
        votes = good + counts.get("unhelpful", 0) + counts.get("inaccurate", 0)
        if votes >= ENOUGH_VOTES:
            return (good / votes - 0.7) * 2
    return 0.0


def score(model, category, needs_vision):
    """How well this model suits the work, or None when it cannot or should not do it."""
    name = model.get("name") or ""
    capabilities = set(model.get("capabilities") or ())
    if is_helper(name):
        return None
    if model.get("blocked"):
        return None            # it cannot load now: a model answering someone else holds the GPU
    if needs_vision and "vision" not in capabilities:
        return None            # it cannot see the picture it is being asked about
    if SPECIALIST.search(name) or (capabilities and "completion" not in capabilities):
        return None            # an embedding model is not going to hold a conversation
    reliability = _reliability(model.get("health"))
    if reliability is None:
        return None

    size = _parameters(model)
    context = model.get("context_length") or 0
    points = reliability + judged(model.get("feedback"), category)
    if category == "light":
        # Small talk goes to the smallest model: it answers in a second and leaves the GPU to
        # whatever needs it. Everything that needs knowledge gets a bigger one.
        points -= min(size / 12.0, 2.0)
    else:
        # Size stands in for quality, but gently: a 30B is better than a 3B, not ten times better
        points += min(size / 12.0, 2.0)

    # A model tuned for code is the right answer to a coding question and a slightly wrong answer to
    # everything else: it won "summarise this transcript" on a tie-break, which is how this was found
    if category != "code" and CODE_NAME.search(name):
        points -= 1.5

    if category == "code":
        points += 3.0 if CODE_NAME.search(name) else 0.0
        points += 0.5 if "thinking" in capabilities else 0.0
        # Coding work reads whole files and projects: a window that holds more of them is worth a
        # little, up to 256K (beyond that the window is stretched and slower, as for "long")
        points += min(context, 262144) / 262144
    elif category == "reasoning":
        points += 2.5 if "thinking" in capabilities else 0.0
        points += 0.5 if size >= 20 else 0.0
    elif category == "long":
        # Reading something long is the one job where the context window decides it - up to 256K. A
        # bigger window is a stretched one (qwen3.6-1m is YaRN x4) and slower at every length, so it
        # is for the conversation that really needs it, chosen by name, not for every long read
        points += min(context, 262144) / 131072.0
    elif category == "vision":
        points += 2.0 if "vision" in capabilities else 0.0
    return points


def why(model, category):
    """A short sentence for the reason it was picked, in the words of what was actually known."""
    reasons = ["smallest that will do"] if category == "light" else []
    capabilities = set(model.get("capabilities") or ())
    name = model.get("name") or ""
    if category == "vision" or "vision" in capabilities:
        reasons.append("sees images")
    if category == "code" and CODE_NAME.search(name):
        reasons.append("tuned for code")
    if category == "reasoning" and "thinking" in capabilities:
        reasons.append("reasons before answering")
    if category == "long" and model.get("context_length"):
        reasons.append(f"reads {round(model['context_length'] / 1024)}K at once")
    if model.get("parameter_size"):
        reasons.append(f"{model['parameter_size']} parameters")
    health = model.get("health") or {}
    if health.get("requests") and not health.get("failures"):
        reasons.append("no failures here")
    return ", ".join(reasons[:3])


def smallest(models, needs_vision=False):
    """Auto's model before the router has said anything: the smallest one that can hold a conversation."""
    able = [m for m in models if score(m, "light", needs_vision) is not None]
    return min(able, key=_parameters)["name"] if able else None


def pick(models, category, needs_vision=False, prefer=None):
    """The best of these models for this work, or None when none of them will do.

    `prefer` is the model the conversation has been using: when it scores as well as the winner it
    keeps the job, because changing model mid-conversation costs a reload of its weights and throws
    away the warm cache, and is not worth it for a tie.
    """
    chosen, considered = _select(models, category, needs_vision, prefer)
    if chosen is None:
        return None
    log.debug("category %s -> %s from %d candidates", category, chosen.get("name"), considered)
    return {"model": chosen["name"], "why": why(chosen, category),
            "considered": considered, "category": category,
            "passed_over": _passed_over(models, category, needs_vision, prefer, chosen)}


def _select(models, category, needs_vision, prefer):
    """(the winner, how many could do the work): best score, a near-tie to the smaller model or to
    the one the conversation is already using."""
    scored = [(points, model) for model in models
              for points in [score(model, category, needs_vision)] if points is not None]
    if not scored:
        return None, 0
    best_points = max(points for points, _ in scored)
    shortlist = [model for points, model in scored if points >= best_points - 0.25]
    shortlist.sort(key=_parameters)        # a near-tie goes to the smaller model, which is quicker
    return next((m for m in shortlist if m.get("name") == prefer), shortlist[0]), len(scored)


def _passed_over(models, category, needs_vision, prefer, chosen):
    """The model that would have won but for its own recent failures, when it is not the one chosen -
    so a reply can say "gemma failed; answered by nemotron" instead of looking mislabelled. Chosen the
    same way with every model's record wiped; None when the winner won on merit."""
    best, _ = _select([{**m, "health": {}} for m in models], category, needs_vision, prefer)
    if best is None or best.get("name") == chosen.get("name"):
        return None
    health = next((m.get("health") or {} for m in models if m.get("name") == best.get("name")), {})
    if not health.get("failures"):
        return None
    return {"model": best["name"], "failures": health["failures"], "requests": health.get("requests") or 0}
