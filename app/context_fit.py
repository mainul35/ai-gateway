"""Long conversations made to fit the model's window, by the gateway, so the work can carry on.

Claude Code and Claude Desktop count tokens with Claude's tokenizer and compact a conversation when
their count says it is nearly full; OpenCode and Codex count with their own guess at the model. The
models here count differently, and agents running at once used to share one window, so a
conversation could arrive bigger than the model can hold: the engine answered "Context size has been
exceeded", the app said "Service is busy", and the session was stuck.

So before a request goes on, it is measured against the model's window, leaving room for the answer,
and when it does not fit it is made to:

1. Old tool output shortened. In an agent's conversation most of the bulk is what its tools returned
   many steps ago - files read, test runs, listings. The start and end of each is kept, the middle
   said to be left out, and the newest turns are not touched. Old pictures become a line saying one
   was there.
2. If that is not enough, the older part of the conversation is summarised by the model itself -
   what the person said, what the task is, what was decided and done, which files changed, what is
   open - and the summary stands in for it, ahead of the recent turns, which go on unchanged. The cut
   is always where the person spoke, never between a tool call and its result.

   A summary that drops something the person said would lose it for the rest of the session, since
   it is reused. So it is checked against the distinctive words of the person's own messages (names,
   paths, identifiers, versions, things in quotes): with some missing, it is written again with them
   named, and whatever is still missing goes in after it as the person's own sentences.

A summary is remembered by what it summarised, so the following requests of the same session (which
resend the whole conversation) reuse it instead of summarising again, and the part sent stays the same
from one request to the next: the engine's prompt cache keeps working.

Three request shapes, each a Shape below: the Messages API (app/routers/anthropic_v1.py), OpenAI's
chat completions (OpenCode, app/routers/openai_v1.py) and OpenAI's Responses API (Codex,
app/routers/responses_v1.py). Pure, apart from the summarise function fit() is handed and its small
memory of summaries; fit_for() supplies one that asks the model being fitted for.
"""
import copy
import hashlib
import json
import logging
import re
from collections import OrderedDict

import httpx

from app import settings

log = logging.getLogger("context_fit")

CHARS_PER_TOKEN = 3.0      # code and logs run 3-4 characters a token for Qwen's tokenizer; err on full
IMAGE_TOKENS = 1600        # a picture, whatever its encoded size
SAFETY = 0.94              # of the window: the estimate is an estimate
ANSWER_CAP = 32768         # room kept for the answer, at most
KEEP_RECENT = 6            # messages at the end never shortened
SHORTEN_STEP = 16          # ...and the edge of the shortened part moves this many messages at a time
TOOL_KEEP_HEAD, TOOL_KEEP_TAIL = 700, 500
TAIL_SHARE = 0.55          # of the budget, for the recent part kept word for word after a summary
SUMMARY_INPUT_SHARE = 0.6  # of the window, the most of the old part handed to the summariser

SUMMARY_PROMPT = """You compress the earlier part of a working session between a person and a coding
agent, so the agent can carry on without it. Write a dense, factual summary for the agent, not for a
reader.

Start with what the person said: every fact, name, requirement, preference and decision they stated,
copied word for word (names, codenames, dates, numbers, paths, versions exactly as they wrote them).
These matter most - the agent can read files again, but cannot ask what the person told it.

Then: the task and the person's goals; decisions made and why; files and functions created, changed
or examined, with their paths; commands run and what they showed; errors met and how they were dealt
with; the current state; and exactly what was in progress and what is left to do. Keep names, paths,
numbers and versions exact. No preamble. At most about 1500 words."""

# Checking a summary kept what the person said: the distinctive words of their own messages - names
# in capitals, paths and file names, identifiers, versions, things in quotes. Messages longer than
# SAID_MAX_CHARS are pasted or injected text (files, AGENTS.md, a client's reminders), not said.
SAID_MAX_CHARS = 2000
MAX_TERMS = 60
QUOTED_MAX_CHARS = 3000    # of the person's own sentences added when a summary still leaves terms out
_TERM = re.compile(r"`[^`\n]{2,80}`|\"[^\"\n]{3,80}\"|\b[A-Z][A-Z0-9_]{3,}\b|[\w.~-]*[/\\][\w./\\-]+"
                   r"|\b[\w-]+\.[a-z][a-z0-9]{0,4}\b|\b[a-z]+_[a-z0-9_]+\b|\b[A-Za-z][a-z0-9]+[A-Z]\w*\b|\bv?\d+\.\d+(?:\.\d+)*\b")
MISSING_NOTE = ("\n\n---\nYour summary must keep each of these exactly as the person wrote them, with what "
                "they refer to: {}")

SUMMARY_NOTE = ("<earlier-conversation-summary>\nThe start of this session was summarised by the gateway "
                "to fit the model's context window:\n\n{}\n</earlier-conversation-summary>")
PICTURE_LEFT_OUT = "[a picture was here; left out to fit the context window]"

_memory = OrderedDict()     # sha256 of the summarised messages -> summary
MEMORY_SIZE = 200


def _text_tokens(text):
    return len(text or "") / CHARS_PER_TOKEN


def _json_tokens(value):
    return len(json.dumps(value, ensure_ascii=False)) / CHARS_PER_TOKEN


def _shorten(text):
    if len(text) <= TOOL_KEEP_HEAD + TOOL_KEEP_TAIL + 200:
        return text
    left = len(text) - TOOL_KEEP_HEAD - TOOL_KEEP_TAIL
    return (text[:TOOL_KEEP_HEAD] + f"\n[... {left} characters of this tool output left out to fit the context "
            f"window; run the tool again if you need them ...]\n" + text[-TOOL_KEEP_TAIL:])


def _texts(content, kinds=("text",)):
    """The text of a content that is a string or a list of parts."""
    if isinstance(content, str):
        return content
    return " ".join(p.get("text") or "" for p in content or () if isinstance(p, dict) and p.get("type") in kinds)


# --- the shapes ---------------------------------------------------------------------------------

class Messages:
    """Anthropic's Messages API: system apart, messages of content blocks, tool results in user turns."""
    items_key = "messages"
    answer_fields = ("max_tokens",)
    picture_kinds = ("image", "document")

    def fixed(self, body):
        system = body.get("system")
        return (self.content_tokens(system) if system else 0) + _json_tokens(body.get("tools") or [])

    def content_tokens(self, content):
        if isinstance(content, str):
            return _text_tokens(content)
        return sum(self.block_tokens(b) for b in content or () if isinstance(b, dict))

    def block_tokens(self, block):
        kind = block.get("type")
        if kind in self.picture_kinds:
            return IMAGE_TOKENS
        if kind == "text":
            return _text_tokens(block.get("text"))
        if kind == "tool_use":
            return (len(json.dumps(block.get("input") or {})) + 40) / CHARS_PER_TOKEN
        if kind == "tool_result":
            return 20 + self.content_tokens(block.get("content"))
        if kind in ("thinking", "redacted_thinking"):
            return _text_tokens(block.get("thinking") or block.get("data"))
        return _json_tokens(block)

    def tokens(self, item):
        return 4 + self.content_tokens(item.get("content"))

    def kept_first(self, items):
        return 0                # the system prompt is not among the messages

    def starts_a_turn(self, item):
        """A message the person wrote, not tool results going back: the only safe place to cut."""
        if item.get("role") != "user":
            return False
        content = item.get("content")
        return isinstance(content, str) or not any(isinstance(b, dict) and b.get("type") == "tool_result"
                                                   for b in content or ())

    def _shorten_result(self, content):
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

    def shorten(self, item):
        """The item with its tool output shortened and pictures left out; None to leave it out."""
        content = item.get("content")
        if not isinstance(content, list):
            return item
        changed = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                block = {**block, "content": self._shorten_result(block.get("content"))}
            elif isinstance(block, dict) and block.get("type") == "image":
                block = {"type": "text", "text": PICTURE_LEFT_OUT}
            changed.append(block)
        return {**item, "content": changed}

    def transcript(self, item):
        role = (item.get("role") or "").upper()
        content = item.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                yield f"{role}: {block.get('text')}"
            elif kind == "tool_use":
                yield f"TOOL CALL {block.get('name')}: {json.dumps(block.get('input'))[:2000]}"
            elif kind == "tool_result":
                yield f"TOOL RESULT: {_shorten(_texts(block.get('content')))}"

    def with_note(self, item, note):
        content = item.get("content")
        rest = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        return {**item, "content": [{"type": "text", "text": note}] + rest}


class ChatCompletions(Messages):
    """OpenAI's chat completions: system messages in the list, tool calls on the assistant's message,
    each result a message of its own with role "tool"."""
    answer_fields = ("max_completion_tokens", "max_tokens")
    picture_kinds = ("image_url", "input_audio", "file")

    def fixed(self, body):
        return _json_tokens(body.get("tools") or [])

    def tokens(self, item):
        calls = item.get("tool_calls")
        return 4 + self.content_tokens(item.get("content")) + (_json_tokens(calls) if calls else 0)

    def kept_first(self, items):
        """The system and developer messages at the start: the agent's instructions, always sent."""
        n = 0
        while n < len(items) and isinstance(items[n], dict) and items[n].get("role") in ("system", "developer"):
            n += 1
        return n

    def starts_a_turn(self, item):
        return item.get("role") == "user"

    def shorten(self, item):
        content = item.get("content")
        if item.get("role") == "tool":
            if isinstance(content, str):
                return {**item, "content": _shorten(content)}
            return {**item, "content": [{**p, "text": _shorten(p.get("text") or "")}
                                        if isinstance(p, dict) and p.get("type") == "text" else p
                                        for p in content or ()]}
        if isinstance(content, list):
            return {**item, "content": [{"type": "text", "text": PICTURE_LEFT_OUT}
                                        if isinstance(p, dict) and p.get("type") in self.picture_kinds else p
                                        for p in content]}
        return item

    def transcript(self, item):
        role = item.get("role") or ""
        text = _texts(item.get("content"))
        if role == "tool":
            yield f"TOOL RESULT: {_shorten(text)}"
            return
        if text:
            yield f"{role.upper()}: {text}"
        for call in item.get("tool_calls") or ():
            function = (call or {}).get("function") or {}
            yield f"TOOL CALL {function.get('name')}: {str(function.get('arguments'))[:2000]}"


class Responses(Messages):
    """OpenAI's Responses API: instructions apart, input a list of items - messages, function calls,
    their outputs (each its own item, tied by call_id), and reasoning."""
    items_key = "input"
    answer_fields = ("max_output_tokens",)
    picture_kinds = ("input_image", "input_file", "input_audio")
    text_kinds = ("input_text", "output_text", "text")

    def fixed(self, body):
        return _text_tokens(body.get("instructions")) + _json_tokens(body.get("tools") or [])

    def tokens(self, item):
        kind = item.get("type") or "message"
        if kind == "message":
            return 4 + self.content_tokens(item.get("content"))
        if kind.endswith("_output"):
            output = item.get("output")
            return 20 + (self.content_tokens(output) if isinstance(output, list) else _text_tokens(str(output or "")))
        return _json_tokens(item)

    def block_tokens(self, block):
        if block.get("type") in self.text_kinds:
            return _text_tokens(block.get("text"))
        return super().block_tokens(block)

    def kept_first(self, items):
        n = 0
        while n < len(items) and isinstance(items[n], dict) and (items[n].get("type") or "message") == "message" \
                and items[n].get("role") in ("system", "developer"):
            n += 1
        return n

    def starts_a_turn(self, item):
        return (item.get("type") or "message") == "message" and item.get("role") == "user"

    def _parts(self, parts):
        return [{**p, "text": _shorten(p.get("text") or "")} if isinstance(p, dict) and p.get("type") in self.text_kinds
                else {"type": "input_text", "text": PICTURE_LEFT_OUT}
                if isinstance(p, dict) and p.get("type") in self.picture_kinds else p
                for p in parts]

    def shorten(self, item):
        kind = item.get("type") or "message"
        if kind.endswith("_output"):
            output = item.get("output")
            if isinstance(output, str):
                return {**item, "output": _shorten(output)}
            return {**item, "output": self._parts(output)} if isinstance(output, list) else item
        if kind == "reasoning":
            # The model's old thoughts, encrypted or not: long, and of no use many steps later
            return None
        content = item.get("content")
        if kind == "message" and isinstance(content, list):
            return {**item, "content": [{"type": "input_text", "text": PICTURE_LEFT_OUT}
                                        if isinstance(p, dict) and p.get("type") in self.picture_kinds else p
                                        for p in content]}
        return item

    def transcript(self, item):
        kind = item.get("type") or "message"
        if kind == "message":
            text = _texts(item.get("content"), self.text_kinds)
            if text:
                yield f"{(item.get('role') or '').upper()}: {text}"
        elif kind.endswith("_output"):
            output = item.get("output")
            text = _texts(output, self.text_kinds) if isinstance(output, list) else str(output or "")
            yield f"TOOL RESULT: {_shorten(text)}"
        elif kind.endswith("_call"):
            arguments = item.get("arguments") or item.get("input") or item.get("action") or ""
            arguments = arguments if isinstance(arguments, str) else json.dumps(arguments)
            yield f"TOOL CALL {item.get('name') or kind}: {arguments[:2000]}"

    def with_note(self, item, note):
        content = item.get("content")
        rest = [{"type": "input_text", "text": content}] if isinstance(content, str) else list(content or [])
        return {**item, "content": [{"type": "input_text", "text": note}] + rest}


MESSAGES, CHAT, RESPONSES = Messages(), ChatCompletions(), Responses()


# --- measuring ----------------------------------------------------------------------------------

def _items(body, shape):
    items = body.get(shape.items_key)
    return items if isinstance(items, list) else []


def estimate(body, messages=None, shape=MESSAGES):
    """Tokens the request will take, roughly and on the high side."""
    items = _items(body, shape) if messages is None else messages
    return int(shape.fixed(body) + sum(shape.tokens(i) for i in items if isinstance(i, dict)))


def budget(body, context, shape=MESSAGES):
    asked = next((body.get(f) for f in shape.answer_fields if body.get(f)), None)
    answer = min(int(asked or 8192), ANSWER_CAP)
    return int(context * SAFETY) - answer


# --- step 1: old tool output, shortened -------------------------------------------------------------

def shorten_old(messages, keep_recent=KEEP_RECENT, shape=MESSAGES, step=SHORTEN_STEP):
    """Tool results and pictures before the last few messages, made short. A new list.

    The edge moves in steps of `step` messages, not one at a time: what was sent before must stay as
    it was, or the engine cannot reuse what it read last time. qwen3.6's attention keeps part of its
    state in a form that cannot be cut back to a changed message, so a conversation changed a few
    messages from its end is read again from far back - 140K tokens, three minutes, every request
    (2026-10-19). With steps, it is read again once per step."""
    out = copy.deepcopy(messages)
    edge = max(0, (len(out) - keep_recent) // step * step)
    old = [shape.shorten(m) if isinstance(m, dict) else m for m in out[:edge]]
    return [m for m in old if m is not None] + out[edge:]


# --- step 2: the older part, summarised -------------------------------------------------------------

def _cut(body, first, messages, room, shape):
    """The earliest place to cut after which the rest fits in `room`; None when there is none."""
    for k in range(1, len(messages)):
        if isinstance(messages[k], dict) and shape.starts_a_turn(messages[k]) \
                and estimate(body, first + messages[k:], shape) <= room:
            return k
    return None


def _transcript(messages, limit_chars, shape):
    text = "\n".join(line for m in messages if isinstance(m, dict) for line in shape.transcript(m))
    if len(text) <= limit_chars:
        return text
    # The start says what the task is; the end is where it stands. The middle gives way.
    head = int(limit_chars * 0.25)
    return text[:head] + "\n[... the middle of the session left out ...]\n" + text[-(limit_chars - head):]


# What a client puts into the person's turn by itself - Claude Code's reminders and command echoes,
# and the gateway's own summary note - is not what the person said
_INJECTED = re.compile(r"<(system-reminder|earlier-conversation-summary|command-[\w-]+|local-command-[\w-]+)>"
                       r".*?</\1>", re.S)


def _said(messages, shape):
    """The person's own messages among these, as text: not tool results, not long pasted text."""
    said = []
    for m in messages:
        if isinstance(m, dict) and shape.starts_a_turn(m):
            for line in shape.transcript(m):
                if line.startswith("USER: "):
                    text = _INJECTED.sub("", line[6:]).strip()
                    if text and len(text) <= SAID_MAX_CHARS:
                        said.append(text)
    return said


def _terms(texts):
    """The distinctive words of these texts, in the order they first appear."""
    terms = []
    for text in texts:
        for match in _TERM.finditer(text):
            term = match.group(0).strip("`\"").rstrip(".,;:")
            if len(term) >= 4 and term not in terms:
                terms.append(term)
    return terms[:MAX_TERMS]


def _missing(summary, terms):
    found = summary.lower()
    return [t for t in terms if t.lower() not in found]


def _quoted(said, missing):
    """The person's sentences holding the missing terms, word for word, up to QUOTED_MAX_CHARS."""
    lines, size = [], 0
    for text in said:
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            sentence = sentence.strip()
            if sentence and any(t.lower() in sentence.lower() for t in missing) and sentence not in lines:
                if size + len(sentence) > QUOTED_MAX_CHARS:
                    return lines
                lines.append(sentence)
                size += len(sentence)
    return lines


async def _summary(summarise, text, said):
    """A summary of `text`, made to keep the distinctive words of what the person said (`said`)."""
    summary = (await summarise(SUMMARY_PROMPT, text) or "").strip()
    if not summary:
        return ""
    terms = _terms(said)
    missing = _missing(summary, terms)
    if missing:
        # Written again with them named: added at the end, so the engine reads the rest from its cache
        again = (await summarise(SUMMARY_PROMPT, text + MISSING_NOTE.format(", ".join(missing))) or "").strip()
        still = _missing(again, terms) if again else missing
        log.info("context: the summary left out %d of %d terms the person used; written again, %d left out",
                 len(missing), len(terms), len(still))
        if again and len(still) < len(missing):
            summary, missing = again, still
    if missing:
        quoted = _quoted(said, missing)
        if quoted:
            summary += "\n\nThe person's own words, which the summary above may leave out:\n" + \
                "\n".join(f"- {line}" for line in quoted)
    return summary


def _key(messages):
    return hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _strings(value):
    """Every piece of text in a request, as the model reads it: no pictures' data, no signatures."""
    if isinstance(value, str):
        if not value.startswith("data:"):
            yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in ("data", "signature", "encrypted_content"):
                yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


async def _calibrated(body, room, shape, count):
    """`room` in the estimate's own units, from the model's real count of the request's text.

    The estimate's 3 characters a token errs on full by design, but how far depends on the text:
    an agent's conversation of HTML and code ran near 5 (232K estimated, 142K real, 2026-10-19), so
    it was compressed at half the window. Asked only once the estimate says it does not fit."""
    text = "\n".join(_strings({k: body.get(k) for k in ("system", "instructions", "tools", shape.items_key)}))
    real = await count(text) if text else None
    if not real:
        return room
    factor = min(1.5, max(0.3, real / (len(text) / CHARS_PER_TOKEN)))
    return int(room / factor)


async def fit(body, context, summarise, force=False, shape=MESSAGES, count=None):
    """The request, made to fit `context` tokens; (body, how) - how is None when it was left alone.

    summarise(prompt, text) -> summary text, by the same model. force shrinks to 70% of the room,
    for when the model said the window was full although the estimate said it was not.
    count(text) -> the model's own token count of text, or None; when given, the estimate is checked
    against it before anything is changed. Returns (None, reason) when nothing makes it fit.
    """
    items = _items(body, shape)
    if not context or not items:
        return body, None
    room = budget(body, context, shape)
    if force:
        room = int(room * 0.7)
    if estimate(body, shape=shape) <= room:
        return body, None
    if count:
        measured = await _calibrated(body, room, shape, count)
        if measured != room:
            log.info("context: estimate %s against %s, measured by the model: room is %s in the estimate's units",
                     estimate(body, shape=shape), room, measured)
            room = measured
        if estimate(body, shape=shape) <= room:
            return body, None

    # Instructions at the start (OpenAI's system messages) stay as they are, ahead of everything
    n = shape.kept_first(items)
    first, messages = copy.deepcopy(items[:n]), items[n:]

    def with_items(rest):
        return {**body, shape.items_key: first + rest}

    shortened = shorten_old(messages, shape=shape)
    if estimate(body, first + shortened, shape) <= room:
        log.info("context: old tool output shortened, %s -> %s estimated tokens of %s",
                 estimate(body, shape=shape), estimate(body, first + shortened, shape), room)
        return with_items(shortened), "shortened"

    k = _cut(body, first, shortened, int(room * TAIL_SHARE), shape)
    if k is None:
        # One long turn with no place to cut: shorten everything but the last two messages
        tighter = shorten_old(messages, keep_recent=2, shape=shape, step=1)
        if estimate(body, first + tighter, shape) <= room:
            return with_items(tighter), "shortened"
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
            if isinstance(shortened[j], dict) and shape.starts_a_turn(shortened[j]):
                earlier = _memory.get(_key(shortened[:j]))
                if earlier:
                    since = j
                    break
        limit = int(context * SUMMARY_INPUT_SHARE * CHARS_PER_TOKEN)
        if earlier:
            text = ("Summary of the session before this point:\n" + earlier + "\n\nWhat happened after it:\n"
                    + _transcript(older[since:], limit - len(earlier), shape))
        else:
            text = _transcript(older, limit, shape)
        summary = await _summary(summarise, text, _said(older, shape))
        if not summary:
            return None, "the model could not summarise the earlier conversation"
        _memory[key] = summary
        while len(_memory) > MEMORY_SIZE:
            _memory.popitem(last=False)
    else:
        _memory.move_to_end(key)

    rest = copy.deepcopy(shortened[k:])
    rest[0] = shape.with_note(rest[0], SUMMARY_NOTE.format(summary))
    fitted = with_items(rest)
    log.info("context: %d earlier messages summarised, %s -> %s estimated tokens of %s",
             k, estimate(body, shape=shape), estimate(fitted, shape=shape), room)
    if estimate(fitted, shape=shape) > room:
        return None, "even summarised, the conversation is bigger than the model's window"
    return fitted, "summarised"


async def fit_for(body, backend, chat_url, force=False, shape=MESSAGES):
    """fit(), with the summary written by the model being fitted for, at its OpenAI chat endpoint
    `chat_url`, without thinking: it is the model already loaded, and a different one would swap the
    GPU in the middle of the person's work. (body, how); body is None when nothing makes it fit."""
    context = backend.context_length
    if not context:
        return body, None

    async def summarise(prompt, text):
        request = {"model": backend.upstream_model, "stream": False, "max_tokens": 3000, "temperature": 0.2,
                   "chat_template_kwargs": {"enable_thinking": False},
                   "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}]}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout(), connect=10)) as client:
                response = await client.post(chat_url, json=request, headers=backend.headers())
            if not response.is_success:
                log.warning("summarising for %s failed: %s %s", backend.upstream_model, response.status_code,
                            response.text[:200])
                return ""
            return response.json()["choices"][0]["message"].get("content") or ""
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            log.warning("summarising for %s failed: %s", backend.upstream_model, e)
            return ""

    async def count(text):
        """The model's own count: llama-server's /tokenize. Ollama has no such call; None there."""
        if backend.kind != "llamacpp":
            return None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60, connect=5)) as client:
                # llama-server reads "content", vLLM "prompt" and the model's name; both answer "tokens"
                response = await client.post(chat_url.split("/v1/")[0] + "/tokenize",
                                             json={"content": text, "prompt": text, "model": backend.upstream_model},
                                             headers=backend.headers())
            return len(response.json()["tokens"]) if response.is_success else None
        except (httpx.HTTPError, ValueError, KeyError) as e:
            log.warning("counting tokens for %s failed: %s", backend.upstream_model, e)
            return None

    fitted, how = await fit(body, context, summarise, force, shape, count)
    if fitted is None:
        log.warning("%s: could not be made to fit %s tokens: %s", backend.upstream_model, context, how)
    return fitted, how
