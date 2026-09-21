"""What a model is for, said in a sentence, so that deleting one is a decision rather than a guess.

Two sources, in this order:

  * a `description:` in config/models.yaml, which is the operator's own word and always wins
  * what the model itself reports - family, parameter size, quantisation, context window and the
    capabilities Ollama lists - turned into plain English

There is deliberately no third source. A table of hand-written blurbs keyed on model name goes stale
the week after it is written: names like "ornith:35b" or "muse-glimmer:30b" mean nothing to anyone
who did not pull them, and inventing a confident sentence about a model this code has never heard of
is worse than saying what is actually known. The few families named below are only the ones whose
purpose is legible from the name itself - "coder" and "embed" say what they are for.
"""
import re

# Only what a name genuinely tells you. Checked in order; the first match wins.
BY_NAME = [
    (re.compile(r"coder|code", re.I), "Tuned for writing and completing code."),
    (re.compile(r"embed", re.I), "An embedding model: turns text into vectors for search, not into replies."),
    (re.compile(r"\bllava\b|vision|\bvl\b|minicpm-v", re.I), "A vision model: answers questions about pictures."),
    (re.compile(r"guard|shield|safety", re.I), "A safety classifier, meant to screen other models' input or output."),
    (re.compile(r"rerank", re.I), "A reranker: scores search results, rather than holding a conversation."),
]

# Ollama's capability words, in the order they are worth reading
CAPABILITY_NOTES = [
    ("vision", "sees images"),
    ("tools", "can call tools"),
    ("thinking", "reasons step by step before answering"),
    ("embedding", "produces embeddings"),
    ("insert", "can fill in the middle of existing text"),
    ("completion", "generates text"),
]


def _thousands(context_length):
    """A context window as people say it out loud: 40960 -> "40K"."""
    if not context_length:
        return None
    if context_length >= 1000:
        rounded = context_length / 1024
        return f"{rounded:.0f}K" if rounded >= 10 else f"{rounded:.1f}K"
    return str(context_length)


def capability_words(capabilities):
    """The capability list as a readable phrase, e.g. "sees images, can call tools"."""
    known = [note for word, note in CAPABILITY_NOTES if word in (capabilities or ())]
    # "generates text" is only worth saying when it is the only thing there is to say
    if len(known) > 1 and "generates text" in known:
        known.remove("generates text")
    return ", ".join(known)


def describe(name, *, override="", family="", parameter_size="", quantization="",
             context_length=None, capabilities=(), backend=""):
    """One or two sentences: what this model is, and what it can do."""
    if override:
        return override.strip()

    facts = []
    if parameter_size:
        facts.append(f"{parameter_size} parameters")
    if quantization and quantization.lower() != "unknown":
        facts.append(f"quantised to {quantization}")
    window = _thousands(context_length)
    if window:
        facts.append(f"a {window} context window")

    said = []
    for pattern, sentence in BY_NAME:
        if pattern.search(name):
            said.append(sentence)
            break
    if family and not said:
        said.append(f"From the {family} family.")
    if facts:
        said.append(("It has " if said else "Has ") + ", ".join(facts) + ".")
    can = capability_words(capabilities)
    if can:
        said.append(f"It {can}.")
    if backend == "llamacpp":
        said.append("Served by this gateway's own tuned llama.cpp profile rather than by Ollama.")
    elif backend not in ("", "ollama"):
        said.append("Served by an upstream API, so nothing is stored on this machine.")
    return " ".join(said) or "Nothing is known about this model beyond its name."
