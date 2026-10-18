"""Long conversations made to fit the model's window, by the gateway, so the work can carry on.

Claude Code and Claude Desktop count tokens with Claude's tokenizer and compact a conversation when
their count says it is nearly full. The models here count differently, and agents running at once
used to share one window, so a conversation could arrive bigger than the model can hold: the engine
answered "Context size has been exceeded", the app said "Service is busy", and the session was stuck.

So before a Messages API request goes on, it is measured against the model's window, leaving room for
the answer, and when it does not fit it is made to:

1. Old tool output shortened. In an agent's conversation most of the bulk is what its tools returned
   many steps ago - files read, test runs, listings. The start and end of each is kept, the middle
   said to be left out, and the newest turns are not touched. Old pictures become a line saying one
   was there.
2. If that is not enough, the older part of the conversation is summarised by the model itself -
   what the task is, what was decided and done, which files changed, what is open - and the summary
   stands in for it, ahead of the recent turns, which go on unchanged. The cut is always where the
   person spoke, never between a tool call and its result.

A summary is remembered by what it summarised, so the following requests of the same session (which
resend the whole conversation) reuse it instead of summarising again, and the part sent stays the same
from one request to the next: the engine's prompt cache keeps working.

Works on the Messages API's own shape (app/routers/anthropic_v1.py). Pure, apart from the summarise
function it is handed and its small memory of summaries.
"""
import copy
import hashlib
import json
import logging
from collections import OrderedDict

log = logging.getLogger("context_fit")

CHARS_PER_TOKEN = 3.0      # code and logs run 3-4 characters a token for Qwen's tokenizer; err on full
IMAGE_TOKENS = 1600        # a picture, whatever its encoded size
SAFETY = 0.94              # of the window: the estimate is an estimate
ANSWER_CAP = 32768         # room kept for the answer, at most
KEEP_RECENT = 6            # messages at the end never shortened
TOOL_KEEP_HEAD, TOOL_KEEP_TAIL = 700, 500
TAIL_SHARE = 0.55          # of the budget, for the recent part kept word for word after a summary
SUMMARY_INPUT_SHARE = 0.6  # of the window, the most of the old part handed to the summariser

SUMMARY_PROMPT = """You compress the earlier part of a working session between a person and a coding
agent, so the agent can carry on without it. Write a dense, factual summary for the agent, not for a
reader: the task and the person's goals and preferences; decisions made and why; files and functions
created, changed or examined, with their paths; commands run and what they showed; errors met and how
they were dealt with; the current state; and exactly what was in progress and what is left to do.
Keep names, paths, numbers and versions exact. No preamble. At most about 1500 words."""

_memory = OrderedDict()     # sha256 of the summarised messages -> summary
MEMORY_SIZE = 200


# --- measuring ----------------------------------------------------------------------------------

def _block_tokens(block):
    kind = block.get("type")
    if kind == "image" or kind == "document":
        return IMAGE_TOKENS
    if kind == "text":
        return len(block.get("text") or "") / CHARS_PER_TOKEN
    if kind == "tool_use":
        return (len(json.dumps(block.get("input") or {})) + 40) / CHARS_PER_TOKEN
    if kind == "tool_result":
        return 20 + _content_tokens(block.get("content"))
    if kind in ("thinking", "redacted_thinking"):
        return len(block.get("thinking") or block.get("data") or "") / CHARS_PER_TOKEN
    return len(json.dumps(block)) / CHARS_PER_TOKEN


def _content_tokens(content):
    if isinstance(content, str):
        return len(content) / CHARS_PER_TOKEN
    return sum(_block_tokens(b) for b in content or () if isinstance(b, dict))


def estimate(body, messages=None):
    """Tokens the request will take, roughly and on the high side."""
    system = body.get("system")
    tokens = _content_tokens(system) if system else 0
    tokens += len(json.dumps(body.get("tools") or [])) / CHARS_PER_TOKEN
    for message in body.get("messages") if messages is None else messages:
        tokens += 4 + _content_tokens(message.get("content"))
    return int(tokens)


def budget(body, context):
    answer = min(int(body.get("max_tokens") or 8192), ANSWER_CAP)
    return int(context * SAFETY) - answer


# --- step 1: old tool output, shortened -------------------------------------------------------------

def _shorten(text):
    if len(text) <= TOOL_KEEP_HEAD + TOOL_KEEP_TAIL + 200:
        return text
    left = len(text) - TOOL_KEEP_HEAD - TOOL_KEEP_TAIL
    return (text[:TOOL_KEEP_HEAD] + f"\n[... {left} characters of this tool output left out to fit the context "
            f"window; run the tool again if you need them ...]\n" + text[-TOOL_KEEP_TAIL:])


def _shorten_result(content):
    if isinstance(content, str):
        return _shorten(content)
    out = []
    for block in content or ():
        if isinstance(block, dict) and block.get("type") == "text":
            out.append({**block, "text": _shorten(block.get("text") or "")})
        elif isinstance(block, dict) and block.get("type") == "image":
            out.append({"type": "text", "text": "[a picture the tool returned was left out to fit the context window]"})
        else:
            out.append(block)
    return out


def shorten_old(messages, keep_recent=KEEP_RECENT):
    """Tool results and pictures before the last few messages, made short. A new list."""
    out = copy.deepcopy(messages)
    for message in out[:max(0, len(out) - keep_recent)]:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for i, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                content[i] = {**block, "content": _shorten_result(block.get("content"))}
            elif block.get("type") == "image":
                content[i] = {"type": "text", "text": "[a picture was here; left out to fit the context window]"}
    return out


# --- step 2: the older part, summarised -------------------------------------------------------------

def _starts_a_turn(message):
    """A message the person wrote, not tool results going back: the only safe place to cut."""
    if message.get("role") != "user":
        return False
    content = message.get("content")
    return isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                               for b in content or ())


def _cut(body, messages, room):
    """The earliest place to cut after which the rest fits in `room`; None when there is none."""
    for k in range(1, len(messages)):
        if _starts_a_turn(messages[k]) and estimate(body, messages[k:]) <= room:
            return k
    return None


def _transcript(messages, limit_chars):
    lines = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                lines.append(f"{role.upper()}: {block.get('text')}")
            elif kind == "tool_use":
                lines.append(f"TOOL CALL {block.get('name')}: {json.dumps(block.get('input'))[:2000]}")
            elif kind == "tool_result":
                inner = block.get("content")
                text = inner if isinstance(inner, str) else " ".join(
                    b.get("text") or "" for b in inner or () if isinstance(b, dict) and b.get("type") == "text")
                lines.append(f"TOOL RESULT: {_shorten(text)}")
    text = "\n".join(lines)
    if len(text) <= limit_chars:
        return text
    # The start says what the task is; the end is where it stands. The middle gives way.
    head = int(limit_chars * 0.25)
    return text[:head] + "\n[... the middle of the session left out ...]\n" + text[-(limit_chars - head):]


def _key(messages):
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def fit(body, context, summarise, force=False):
    """The request, made to fit `context` tokens; (body, how) - how is None when it was left alone.

    summarise(prompt, text) -> summary text, by the same model. force shrinks to 70% of the room,
    for when the model said the window was full although the estimate said it was not.
    Returns (None, reason) when nothing makes it fit.
    """
    messages = body.get("messages") or []
    if not context or not messages:
        return body, None
    room = budget(body, context)
    if force:
        room = int(room * 0.7)
    if estimate(body) <= room:
        return body, None

    shortened = shorten_old(messages)
    if estimate(body, shortened) <= room:
        log.info("context: old tool output shortened, %s -> %s estimated tokens of %s",
                 estimate(body), estimate(body, shortened), room)
        return {**body, "messages": shortened}, "shortened"

    k = _cut(body, shortened, int(room * TAIL_SHARE))
    if k is None:
        # One long turn with no place to cut: shorten everything but the last two messages
        tighter = shorten_old(messages, keep_recent=2)
        if estimate(body, tighter) <= room:
            return {**body, "messages": tighter}, "shortened"
        return None, "the latest turn alone is bigger than the model's window"

    older = shortened[:k]
    key = _key(older)
    summary = _memory.get(key)
    if summary is None:
        # A session that keeps growing moves its cut forward now and then. The summary made at the
        # last cut is carried forward with what came after it, rather than everything summarised
        # again from the start, which in a very long session would lose its middle.
        earlier, since = None, 0
        for j in range(k - 1, 0, -1):
            if _starts_a_turn(shortened[j]):
                earlier = _memory.get(_key(shortened[:j]))
                if earlier:
                    since = j
                    break
        limit = int(context * SUMMARY_INPUT_SHARE * CHARS_PER_TOKEN)
        if earlier:
            text = ("Summary of the session before this point:\n" + earlier + "\n\nWhat happened after it:\n"
                    + _transcript(older[since:], limit - len(earlier)))
        else:
            text = _transcript(older, limit)
        summary = (await summarise(SUMMARY_PROMPT, text) or "").strip()
        if not summary:
            return None, "the model could not summarise the earlier conversation"
        _memory[key] = summary
        while len(_memory) > MEMORY_SIZE:
            _memory.popitem(last=False)
    else:
        _memory.move_to_end(key)

    rest = copy.deepcopy(shortened[k:])
    note = {"type": "text", "text": ("<earlier-conversation-summary>\nThe start of this session was summarised by "
                                     "the gateway to fit the model's context window:\n\n" + summary +
                                     "\n</earlier-conversation-summary>")}
    first = rest[0]
    content = first.get("content")
    first["content"] = [note] + ([{"type": "text", "text": content}] if isinstance(content, str) else list(content or []))
    fitted = {**body, "messages": rest}
    log.info("context: %d earlier messages summarised, %s -> %s estimated tokens of %s",
             k, estimate(body), estimate(fitted), room)
    if estimate(fitted) > room:
        return None, "even summarised, the conversation is bigger than the model's window"
    return fitted, "summarised"
