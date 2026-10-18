"""Memory for the playground, at two levels.

Per conversation: a running summary of the turns that no longer fit in what is sent to the model, so a
long chat keeps its thread instead of losing the beginning.

Per user: short notes that outlive a single conversation, so the next chat already knows what was
decided. They are kept up to date after every turn by explicit edits - add, update or delete a note
by its id - so that something that stops being true is taken out the moment the user says so
(CHECK_PROMPT). Both are written by the conversation's own model, which is already loaded and has
just read the material, and both are shown in the playground, where they can be edited or deleted.
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

# After every turn: the latest exchange, checked against the notes as they stand. The notes used to be
# rewritten whole from the conversation summary, every eight turns of a long chat. A model asked to
# rewrite a list keeps nearly everything, so nothing was ever removed: "targeting 96 GB of RAM" stayed
# after the RAM was in, "no GPU upgrades planned" after a second card was being priced, and facts the
# assistant had made up ("Qwen3.8-27B (2.4T params)") were kept as the user's. Edits by id, made the
# moment something changes and taken only from what the user said, are what keep the list true.
CHECK_PROMPT = """You maintain a short list of durable facts about a user, used to start their future
chats well. You are shown the list, each note with an id, and the latest exchange of a conversation.
Decide what the latest exchange changes in the list. Most of the time it changes nothing.

Only what the USER said counts. The assistant's words are there for context and are never a source
of facts about the user: it can be wrong. Every edit quotes, in "user_said", the user's own words
that make it necessary, copied exactly from a USER line. No such words, no edit.

- update: a note the user's words make out of date or wrong - a plan now done, a choice changed, a
  number that is different now. Write the note as it is true now, and keep the parts of the old
  note the user did not change. A plan is written as a plan ("Plans to add a Tesla P100"), never as
  something already done.
- delete: a note the user's words contradict or show no longer holds. The quoted words must be about
  the same thing as the note: words about which model they use never delete a note about RAM. A
  note the exchange does not touch is left alone, however old it is.
- add: a new durable fact from the user's words - their setup, hardware, projects, decisions,
  preferences, how they like answers. Not one-off questions and not the topic of today's chat.

Each note is one specific fact under 120 characters ("Has 92 GB RAM, upgraded from 45 GB"), never a
topic ("Discussed RAM"). Notes marked (user's own) were written by the user: never change or delete
them. Keep the list at most {limit} notes: when it is full, add only by updating or deleting.

Reply with JSON only, for example:
{{"update": [{{"id": "n3", "text": "...", "user_said": "..."}}],
  "delete": [{{"id": "n5", "user_said": "..."}}],
  "add": [{{"text": "...", "user_said": "..."}}]}}
Empty lists when nothing changes, which is most of the time."""

_EDIT = {"type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"},
                                           "user_said": {"type": "string"}}}
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "update": {"type": "array", "items": {**_EDIT, "required": ["id", "text", "user_said"]}},
        "delete": {"type": "array", "items": {**_EDIT, "required": ["id", "user_said"]}},
        "add": {"type": "array", "items": {**_EDIT, "required": ["text", "user_said"]}},
    },
    "required": ["update", "delete", "add"],
}

QUOTE_COVERAGE = 0.8    # of a quote's words that must be in what the user wrote


def _words(text):
    return re.findall(r"[a-z0-9]+(?:[.'][a-z0-9]+)*", (text or "").lower())


def said_by_user(quote, user_text):
    """Whether the quote is the user's: most of its words are in what they wrote this turn.

    Not an exact match, because models tidy a quote while copying it; but a claim found only in the
    assistant's answer ("Qwen3.8-27B has 2.4 trillion parameters") does not pass, which is the point.
    """
    words = [w for w in _words(quote) if len(w) > 2]
    if len(words) < 2:
        return False
    theirs = set(_words(user_text))
    return sum(w in theirs for w in words) / len(words) >= QUOTE_COVERAGE


COMMON = set("the and for with that this from have has had was are not but you your our use uses used "
             "using its into about than then them they will would can could should want wants plan plans "
             "planned planning now also just very more most some any all one two get got make made".split())


def about(quote, note):
    """Whether the quoted words share a meaningful word with the note they are used to change.

    A real quote can still be about something else: "I'm planning to add a Tesla P100" was used to
    delete both notes about RAM. A wrongly deleted note is lost, while a stale one can be corrected
    the next time it comes up, so this errs on the side of keeping.
    """
    significant = lambda text: {w for w in _words(text) if len(w) > 2 and w not in COMMON}
    return bool(significant(quote) & significant(note))


def check_request(notes, exchange, model):
    """notes: (key, text, written_by_user) in list order; exchange: the latest turns, as text."""
    listed = "\n".join(f"{key}: {text}" + (" (user's own)" if manual else "")
                       for key, text, manual in notes) or "(no notes yet)"
    return {
        "model": model, "stream": False, "temperature": 0, "max_tokens": 900,
        "reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False},
        "response_format": {"type": "json_schema", "json_schema": {"name": "note_edits", "schema": CHECK_SCHEMA}},
        "messages": [
            {"role": "system", "content": CHECK_PROMPT.format(limit=settings.max_user_notes())},
            {"role": "user", "content": f"Notes:\n{listed}\n\nLatest exchange:\n{exchange}\n\nEdits as JSON:"},
        ],
    }


def parse_changes(text, notes_now, user_text, protected=()):
    """The model's edits, checked: each backed by the user's own words, only ids that exist, never a
    note the user wrote, no duplicates.

    notes_now is {key: the note's text}. Returns {"update": {key: text}, "delete": {key}, "add": [text]},
    or None when unreadable.
    """
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    match = re.search(r"\{.*\}", cleaned, flags=re.S)
    try:
        raw = json.loads(match.group(0)) if match else None
    except ValueError:
        raw = None
    if not isinstance(raw, dict):
        return None
    editable = set(notes_now) - set(protected)

    def backed(item, changes=None):
        """The user said it - and, for an edit to an existing note, said it about that note."""
        return (isinstance(item, dict) and said_by_user(item.get("user_said"), user_text)
                and (changes is None or about(item.get("user_said"), changes)))

    update = {}
    for item in raw.get("update") or ():
        new = str(item.get("text") or "").strip()[:NOTE_LENGTH] if isinstance(item, dict) else ""
        key = item.get("id") if isinstance(item, dict) else None
        # An "update" to the same words is no edit; seen when the model had nothing to change
        if key in editable and new and new != notes_now[key] and backed(item, notes_now[key]):
            update[key] = new
    delete = {item["id"] for item in raw.get("delete") or ()
              if isinstance(item, dict) and item.get("id") in editable
              and backed(item, notes_now[item["id"]])} - set(update)
    add, seen = [], {t.lower() for t in update.values()}
    for item in raw.get("add") or ():
        note = str(item.get("text") or "").strip()[:NOTE_LENGTH] if backed(item) else ""
        if note and note.lower() not in seen:
            seen.add(note.lower())
            add.append(note)
    return {"update": update, "delete": delete, "add": add}


def exchange_text(messages, chars=3000):
    """The latest turn - the user's message and the answer - with the turn before it.

    Returns (the exchange as text, what the user wrote in it), the second for checking quotes.
    """
    said = [m for m in messages if (m.content or "").strip()]
    last_user = max((i for i, m in enumerate(said) if m.role == "user"), default=None)
    if last_user is None:
        return "", ""
    picked = said[max(0, last_user - 2):]
    text = "\n".join(f"{'USER' if m.role == 'user' else 'ASSISTANT'}: {(m.content or '').strip()[:chars]}"
                     for m in picked)
    return text, "\n".join((m.content or "")[:chars] for m in picked if m.role == "user")


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


def clean_text(text, limit=SUMMARY_LENGTH):
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    return text[:limit].strip() or None


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
