"""Anthropic's Messages API on one side, the OpenAI chat API on the other.

Claude Code speaks only the Messages API. Ollama and llama.cpp speak it too, and requests for their
models are passed through unchanged (app/routers/anthropic_v1.py). This module is for everything
else: a backend that only knows the OpenAI format gets the request translated, and its answer -
whole or streamed - translated back, so Claude Code cannot tell the difference.

Pure functions only; nothing here talks to the network.
"""
import json
import uuid

STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use",
                "function_call": "tool_use", "content_filter": "end_turn"}


def message_id():
    return "msg_" + uuid.uuid4().hex[:24]


def _text_of(content):
    """The words in a content field, which may be a string or a list of blocks."""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def _image_url(block):
    source = block.get("source") or {}
    if source.get("type") == "base64":
        return f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
    if source.get("type") == "url":
        return source.get("url", "")
    return ""


def _tool_result_text(block):
    """What a tool gave back, as the text an OpenAI "tool" message carries."""
    content = block.get("content")
    text = content if isinstance(content, str) else "\n".join(
        b.get("text", "") if b.get("type") == "text" else f"[{b.get('type')} omitted]"
        for b in content or [] if isinstance(b, dict))
    return ("Error: " + text) if block.get("is_error") else text


def to_openai_request(body, upstream_model):
    """A Messages API request as an OpenAI chat completions request."""
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _text_of(system)})

    for message in body.get("messages") or []:
        role, content = message.get("role"), message.get("content")
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        blocks = content or []
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            calls = [{"id": b.get("id"), "type": "function",
                      "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}}
                     for b in blocks if b.get("type") == "tool_use"]
            entry = {"role": "assistant", "content": text or None}
            if calls:
                entry["tool_calls"] = calls
            messages.append(entry)
            continue
        # A user turn: tool results become "tool" messages, and come first, as OpenAI requires
        # them straight after the assistant message that asked for them
        for block in blocks:
            if block.get("type") == "tool_result":
                messages.append({"role": "tool", "tool_call_id": block.get("tool_use_id"),
                                 "content": _tool_result_text(block)})
        parts = []
        for block in blocks:
            if block.get("type") == "text":
                parts.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "image":
                parts.append({"type": "image_url", "image_url": {"url": _image_url(block)}})
        if parts:
            only_text = all(p["type"] == "text" for p in parts)
            messages.append({"role": "user",
                             "content": "".join(p["text"] for p in parts) if only_text else parts})

    request = {"model": upstream_model, "messages": messages, "stream": bool(body.get("stream"))}
    if body.get("max_tokens"):
        request["max_tokens"] = body["max_tokens"]
    for name in ("temperature", "top_p"):
        if body.get(name) is not None:
            request[name] = body[name]
    if body.get("stop_sequences"):
        request["stop"] = body["stop_sequences"]
    tools = [t for t in body.get("tools") or [] if t.get("name") and "input_schema" in t]
    if tools:
        request["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t.get("description", ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}} for t in tools]
        choice = body.get("tool_choice") or {}
        kind = choice.get("type")
        if kind == "any":
            request["tool_choice"] = "required"
        elif kind == "tool" and choice.get("name"):
            request["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
        elif kind == "none":
            request["tool_choice"] = "none"
    if request["stream"]:
        request["stream_options"] = {"include_usage": True}
    return request


def _arguments(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except ValueError:
        return {"raw": raw}


def from_openai_response(payload, model):
    """An OpenAI chat completion as a Messages API message."""
    choice = (payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    content = []
    if message.get("content"):
        content.append({"type": "text", "text": message["content"]})
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        content.append({"type": "tool_use", "id": call.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                        "name": function.get("name"), "input": _arguments(function.get("arguments"))})
    usage = payload.get("usage") or {}
    return {
        "id": message_id(), "type": "message", "role": "assistant", "model": model, "content": content,
        "stop_reason": STOP_REASONS.get(choice.get("finish_reason"), "end_turn"), "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens") or 0,
                  "output_tokens": usage.get("completion_tokens") or 0},
    }


def sse(event, data):
    """One Messages API stream event."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class StreamTranslator:
    """Turns OpenAI stream chunks into the Messages API's event sequence, one chunk at a time.

    The Messages API numbers content blocks and opens and closes each one explicitly; OpenAI just
    sends deltas. So the translator keeps track of which block is open - text, or the tool call with
    a given index - and closes it the moment something else starts.
    """

    def __init__(self, model):
        self.model = model
        self.started = False
        self.index = -1
        self.open = None            # ("text",) or ("tool", openai_index)
        self.tool_blocks = {}       # openai tool index -> our block index
        self.stop_reason = "end_turn"
        self.usage = {"input_tokens": 0, "output_tokens": 0}

    def _start(self):
        if self.started:
            return b""
        self.started = True
        return sse("message_start", {"type": "message_start", "message": {
            "id": message_id(), "type": "message", "role": "assistant", "model": self.model, "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})

    def _close(self):
        if self.open is None:
            return b""
        self.open = None
        return sse("content_block_stop", {"type": "content_block_stop", "index": self.index})

    def feed(self, chunk):
        """Events for one parsed OpenAI chunk (the JSON of a data: line)."""
        out = self._start()
        if chunk.get("usage"):
            self.usage = {"input_tokens": chunk["usage"].get("prompt_tokens") or 0,
                          "output_tokens": chunk["usage"].get("completion_tokens") or 0}
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if self.open != ("text",):
                    out += self._close()
                    self.index += 1
                    self.open = ("text",)
                    out += sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {"type": "text", "text": ""}})
                out += sse("content_block_delta", {"type": "content_block_delta", "index": self.index,
                                                    "delta": {"type": "text_delta", "text": text}})
            for call in delta.get("tool_calls") or []:
                which = call.get("index", 0)
                function = call.get("function") or {}
                if which not in self.tool_blocks:
                    out += self._close()
                    self.index += 1
                    self.tool_blocks[which] = self.index
                    self.open = ("tool", which)
                    out += sse("content_block_start", {"type": "content_block_start", "index": self.index,
                                                        "content_block": {
                                                            "type": "tool_use",
                                                            "id": call.get("id") or "toolu_" + uuid.uuid4().hex[:24],
                                                            "name": function.get("name") or "",
                                                            "input": {}}})
                if function.get("arguments"):
                    out += sse("content_block_delta", {"type": "content_block_delta",
                                                        "index": self.tool_blocks[which],
                                                        "delta": {"type": "input_json_delta",
                                                                  "partial_json": function["arguments"]}})
            if choice.get("finish_reason"):
                self.stop_reason = STOP_REASONS.get(choice["finish_reason"], "end_turn")
        return out

    def finish(self):
        """The closing events: last block closed, the stop reason and usage, and message_stop."""
        out = self._start() + self._close()
        out += sse("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                                      "usage": {"output_tokens": self.usage["output_tokens"]}})
        out += sse("message_stop", {"type": "message_stop"})
        return out


def error_body(message, kind="api_error"):
    return {"type": "error", "error": {"type": kind, "message": message}}


def _blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    return [b for b in content or () if isinstance(b, dict)]


def fold_system_messages(messages):
    """Messages with role "system" inside the conversation, folded into the user turn beside them.

    Claude Code (2.1.2xx) sends some of its context - the list of skills - as a "system" message
    after the first user message. Models whose chat template allows a system message only at the
    very start refuse the whole request for it: qwen3.6 answered every Claude Code request with a
    500, "System message must be at the beginning". The note is kept where it was in the
    conversation, as a <system-reminder> block on the nearest user message - the way Claude Code
    sends its other reminders - so the model reads the same thing and every template accepts it.
    """
    if not isinstance(messages, list) or not any(isinstance(m, dict) and m.get("role") == "system"
                                                  for m in messages):
        return messages
    folded, pending = [], []
    for message in messages:
        if not isinstance(message, dict):
            folded.append(message)
            continue
        if message.get("role") == "system":
            text = "\n".join(b.get("text", "") for b in _blocks(message.get("content")) if b.get("type") == "text")
            if text.strip():
                note = {"type": "text", "text": f"<system-reminder>\n{text}\n</system-reminder>"}
                if folded and isinstance(folded[-1], dict) and folded[-1].get("role") == "user":
                    folded[-1] = {**folded[-1], "content": _blocks(folded[-1].get("content")) + [note]}
                else:
                    pending.append(note)
            continue
        if pending and message.get("role") == "user":
            message = {**message, "content": pending + _blocks(message.get("content"))}
            pending = []
        folded.append(message)
    if pending:      # nothing after it to carry it: a user turn of its own, still in its place
        folded.append({"role": "user", "content": pending})
    return folded


def estimate_tokens(body):
    """A rough count for /v1/messages/count_tokens: about four characters a token, as Claude Code
    itself assumes when a gateway has no counter at all."""
    text = json.dumps(body.get("system") or "") + json.dumps(body.get("messages") or []) \
        + json.dumps(body.get("tools") or [])
    return max(1, len(text) // 4)


def usage_from_event(line, usage):
    """Token counts out of a Messages API stream data line, folded into `usage` (OpenAI names)."""
    if not line.startswith("data:"):
        return usage
    try:
        event = json.loads(line[5:].strip())
    except ValueError:
        return usage
    if event.get("type") == "message_start":
        counts = (event.get("message") or {}).get("usage") or {}
        usage = dict(usage, prompt_tokens=counts.get("input_tokens") or usage.get("prompt_tokens") or 0)
    elif event.get("type") == "message_delta":
        counts = event.get("usage") or {}
        if counts.get("input_tokens"):
            usage = dict(usage, prompt_tokens=counts["input_tokens"])
        usage = dict(usage, completion_tokens=counts.get("output_tokens") or usage.get("completion_tokens") or 0)
    usage["total_tokens"] = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
    return usage
