"""Memory for the playground, at two levels.

Per conversation: a running summary of the turns that no longer fit in what is sent to the model, so a
long chat keeps its thread instead of losing the beginning.

Per user: short notes that outlive a single conversation, so the next chat already knows what was
decided. Both are written by the conversation's own model, which is already loaded and has just read
the material, and both are shown in the playground, where they can be edited or deleted: a decision the
user changes later must not be remembered forever.
"""
import json
import logging
import re

from app import settings

log = logging.getLogger("tools.memory")

NOTE_LENGTH = 160
SUMMARY_LENGTH = 1500

SUMMARY_PROMPT = """You keep running notes of a conversation, for an assistant that can no longer see
its older messages.

Write short bullet points, and be specific: names, numbers, versions, hardware, file paths, what the
user is working on, what they decided, what they prefer, and anything still open. Write the facts
themselves ("uses an RTX 3090 with 24 GB"), never a description of the conversation ("discussed
hardware"). If something was decided and then changed, keep only what is true now and drop the old
choice. At most 150 words. Reply with the notes only."""

NOTES_PROMPT = """You keep a short list of durable facts about a user, used to start future chats well.

Keep only what stays true beyond today's conversation: what they work on, their setup and hardware,
how they like answers, and decisions they have made. Each note is a specific fact, not a topic: write
"Uses MongoDB for the gateway, decided against PostgreSQL", never "Discussed databases". Leave out
one-off questions and anything temporary. If new information contradicts an old note, replace the old
note. At most {limit} notes, each under 120 characters.

Reply with a JSON array of strings and nothing else, for example: ["Runs a homelab server", "Prefers
short answers"]."""


def is_enabled():
    return settings.get_bool("memory.enabled", "MEMORY_ENABLED", True)


def model_for(conversation_model):
    """The chat's own model writes its notes: it is already loaded and understands the material."""
    return settings.memory_model() or conversation_model or settings.router_model()


def summary_request(previous, transcript, model):
    body = f"Conversation so far:\n{transcript}"
    if previous:
        body = f"Notes so far:\n{previous}\n\n{body}\n\nUpdate the notes."
    return {
        "model": model, "stream": False, "temperature": 0.2, "max_tokens": 900,
        "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": body}],
    }


def notes_request(existing, summary, model):
    existing_text = "\n".join(f"- {n}" for n in existing) or "(none yet)"
    return {
        "model": model, "stream": False, "temperature": 0.2, "max_tokens": 900,
        "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        "messages": [
            {"role": "system", "content": NOTES_PROMPT.format(limit=settings.max_user_notes())},
            {"role": "user", "content": f"Notes about the user so far:\n{existing_text}\n\n"
                                        f"What happened in the latest conversation:\n{summary}\n\n"
                                        "Updated notes as a JSON array:"},
        ],
    }


def clean_text(text, limit=SUMMARY_LENGTH):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    return text[:limit].strip() or None


def parse_notes(text, limit=None):
    """Reads the model's JSON array; falls back to reading bullet points if it wrote those instead."""
    limit = limit or settings.max_user_notes()
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\[.*]", cleaned, flags=re.S)
    notes = []
    if match:
        try:
            notes = [str(n) for n in json.loads(match.group(0)) if str(n).strip()]
        except ValueError:
            notes = []
    if not notes:
        notes = [re.sub(r"^\s*[-*\d.]+\s*", "", line).strip(' "')
                 for line in cleaned.splitlines() if re.match(r"^\s*[-*\d]", line)]
    seen, kept = set(), []
    for note in notes:
        note = note.strip()[:NOTE_LENGTH]
        if note and note.lower() not in seen:
            seen.add(note.lower())
            kept.append(note)
    return kept[:limit]


def transcript(messages, limit=12000):
    """The part of the conversation the notes must cover, oldest first."""
    lines = []
    for message in messages:
        text = (message.content or "").strip()
        if not text:
            continue
        lines.append(f"{'User' if message.role == 'user' else 'Assistant'}: {text[:2000]}")
    return "\n".join(lines)[-limit:]


def context_block(notes, summary):
    """What is put in front of the model on the next turn."""
    parts = []
    if notes:
        parts.append("What you know about this user from earlier conversations:\n"
                     + "\n".join(f"- {n}" for n in notes))
    if summary:
        parts.append("Earlier in this conversation:\n" + summary)
    if not parts:
        return None
    return ("\n\n".join(parts) + "\n\nUse this if it helps. If the user says something that contradicts "
                                 "it, believe the user.")
