# IntelliJ ReAct agent plugin — design

Status: draft for review. No plugin code exists yet. Side project of the AI gateway.

## 1. Goal and an honest limit

An IntelliJ IDEA plugin that runs a ReAct (Reason + Act) agent against the gateway's OpenAI-compatible
`/v1`, with managed memory and managed KV cache, so a local model behaves as reliably as possible on
coding tasks.

A harness cannot make a 30B local model reason like Claude. What it can do is remove the failures that
make small models look weak: lost context, repeated work, malformed tool calls, drifting off the task,
and slow turns. "Claude-level" here means **Claude-level discipline**: plan, act, observe, verify, stop.
Success is measured (section 9), not claimed.

## 2. Principles

1. **Code decides transitions, the model decides content.** Same rule as `local_coding_agent`: the
   loop, retries, budgets and stop conditions are code. The model only produces a thought and an action.
2. **The prompt prefix is append-only.** Everything that makes the KV cache hit depends on this (section 5).
3. **Every write is confirmed.** File edits and shell commands go through an approval step; reads do not.
4. **Memory never changes silently** (section 4.4). This is a hard requirement from the owner.
5. **No user content in code.** Prompts hold instructions only, never example stories or requests.

## 3. ReAct loop

```
user goal -> [plan] -> loop { Thought -> Action -> Observation } -> [verify] -> answer
```

- **Action format:** native OpenAI `tool_calls` when the model supports them, with a JSON fallback
  (`{"thought": "...", "action": "name", "args": {...}}`) parsed and schema-validated by code. A malformed
  call is retried once with the validation error as the observation, then counted as a failed step.
- **Tools (v1):** `read_file`, `list_dir`, `search_text`, `find_symbol` (PSI), `edit_file`
  (search/replace block, reusing `local_coding_agent/edit_engine.py` semantics), `create_file`,
  `run_command` (allow-listed, timeout), `run_tests`, `get_diagnostics` (IDE inspections, the cheap
  verifier), `ask_user`, `finish`.
- **Edits go through IntelliJ APIs** (`WriteCommandAction`, `Document`), so they are undoable (Ctrl+Z) and
  shown as a diff for approval via `DiffManager`. Not raw file writes.
- **Budgets enforced by code:** max steps, max wall time, max repeated identical action (3 -> force a
  re-plan), max consecutive failures (2 -> stop and ask the user). Each has a visible counter.
- **Verification:** after edits, `get_diagnostics` on touched files, then `run_tests` if configured.
  Failure feeds back as an observation; the verify loop is capped.
- **Streaming and cancel:** thoughts and observations stream into the tool window; Stop cancels the HTTP
  call and releases the gateway slot.

## 4. Memory

### 4.1 Tiers

| Tier | Holds | Lifetime | Where |
|---|---|---|---|
| Working | the current prompt: system, task, recent steps | one run | in the prefix (section 5) |
| Episodic | a rolling summary of finished steps and decisions | one conversation | `.idea/ai-agent/sessions/` |
| Project | durable facts: build/test commands, conventions, module map | the project | `.idea/ai-agent/memory.md` (VCS-able, human-editable) |
| User | preferences that follow the person across projects | the user | IDE app-level storage |

Project memory is a plain Markdown file of one-fact entries, so it can be read, diffed and edited by hand.
Optional: embeddings for recall over a large codebase via the gateway's `qwen3-embedding:0.6b`
(same stack as `app/knowledge.py`). Not in v1; a keyword/PSI search comes first.

### 4.2 What gets written

The agent proposes memory writes as a tool call `remember(scope, fact)`. Code, not the model, decides
whether the write is allowed through (4.4). Candidates come from: user corrections, discovered
build/test commands, confirmed decisions. Not stored: anything derivable from the code or git history.

### 4.3 Recall

At run start, project and user memory are injected once into the stable prefix. During a run, retrieval
results arrive as observations (appended), never by rewriting earlier text.

### 4.4 Conflict gate (required behaviour)

Before any memory entry is updated with information that may conflict with what is stored:

1. **Detect.** A cheap candidate pass (keyword/subject overlap, then the small router model for a
   contradicts / refines / unrelated verdict) compares the new fact with stored entries.
2. **Cherry-pick.** Extract only the conflicting part of the old entry and of the new fact, not the whole
   documents.
3. **Ask the user** in the tool window: *old: "..." / new: "..." / Overwrite, Keep old, or Keep both?*
4. **Act on the answer and continue.** Overwrite replaces and records the old text in a change log;
   Keep old drops the new fact; either way the run continues. No answer within the run = Keep old
   (the safe default), and the proposal is parked in a visible "pending memory changes" list.
5. **Non-conflicting writes** (new subject, or a pure refinement the user already approved class-wide)
   go through without a prompt.

The same gate applies to every tier, and to the summary compactor: a summary may not silently drop or
reverse a recorded decision.

## 5. KV-cache management

llama.cpp reuses the KV cache for the longest identical token prefix of a slot. Any edit earlier in the
prompt invalidates everything after it. So:

### 5.1 Plugin side — stable prefix

Prompt layout, top to bottom, from most stable to least:

```
[system + tool schemas]        never changes within a version
[project + user memory]        fixed at run start
[episodic summary]             changes only at compaction
[turn history, append-only]    thoughts, actions, observations
```

Rules: no timestamps, counters or random ids in the prefix; tool schemas serialized in a fixed order;
observations are truncated **when produced** (head+tail with a marker), never retroactively.

### 5.2 Compaction only at boundaries

Context is compacted when it passes ~75% of the slot's `ctx_size`, and only at a step boundary:
the oldest completed steps are replaced by an updated episodic summary, written by the model itself
(conflict gate applies). That costs one full re-prefill, so it is rare and deliberate. The plugin logs
each compaction with the tokens saved and the re-prefill it caused.

Large observations (file dumps) are the main driver. Mitigation: read by range, prefer `search_text` +
ranged reads, and pin neither whole files nor repeated dumps.

### 5.3 Gateway side — small slot API

The gateway already launches llama.cpp with `--parallel`, KV quantization (`q8_0`) and flash attention
(`app/engine/profiles.py`). It does not yet expose slots. Additions, all admin/user-key gated and
additive to `/v1`:

- **Pin:** an optional `X-Session-Id` header (or `user`) maps a session to one llama.cpp slot
  (`id_slot`), so a conversation keeps hitting its own cache instead of whichever slot is free.
  `cache_prompt: true` set by default for the plugin.
- **Save / restore:** `POST /v1/cache/{session}/save` and `/restore` wrap llama.cpp's
  `slot save/restore` (needs `--slot-save-path`). Used when the engine is evicted by exclusive VRAM
  swapping, and for "resume yesterday's session without a 30 s re-prefill".
- **Report:** response headers or a `GET /v1/cache/{session}` return `cached_tokens`, `prompt_tokens`,
  hit rate and slot occupancy. The plugin shows hit rate in the status bar; a falling rate is the
  signal that something broke the prefix.
- **Idle rule:** never evict or restart an engine with requests in flight (existing rule in
  `engine-in-use-check`); a restore is queued behind running work.

Caveat to verify first: slot save/restore and `id_slot` behaviour differ across llama.cpp builds and
for hybrid/recurrent models (e.g. Qwen3-Next). Each model profile gets a `slots_supported` flag from a
probe at engine start; unsupported models still get 5.1 and 5.2.

### 5.4 Phase 0 measurements (2026-11-15, homelabai)

Throwaway llama-server b11836, Qwen3.8-27B Q8, 2 slots x 16K, q8 KV, `--ctx-checkpoints 64`,
`--cache-ram 8192`, `--slot-save-path`; engine idle, stopped and cleaned up afterwards
(`scripts/cache_probe.py`, 12.2K-token prefix):

| Case | Prompt tokens computed | Cached | Time |
|---|---|---|---|
| Cold | 12232 | 0 | 10.4 s |
| Same prefix, new tail (append-only) | 16 | 12216 | 0.30 s |
| One word changed at the very start | 12232 | 0 | 10.3 s |
| Save slot (12232 tokens, 1.05 GB) | | | 0.54 s |
| Erase, restore | | 12232 restored | 0.32 s |
| Request after restore | 16 | 12216 | 0.31 s |

Conclusions:
- **Prefix reuse works and is all-or-nothing.** 99.9% hit on an append-only prompt, 0% when the first
  word changes. Section 5.1 is confirmed as the single most valuable rule: 35x faster per turn.
- **Slot save/restore works** on this hybrid model, 12K tokens in 0.3 s. A 100K-token save is roughly
  8 GB, so keep few and expire them.
- **Pinning matters less than planned.** A request sent to the idle slot 1 still got a 12216-token hit
  (the slot holding the prefix, or `--cache-ram`, served it), and `id_slot` is not echoed on `/v1`.
  Drop "pin a slot" from 5.3 for llama.cpp unless a test with two *different* live prefixes shows
  eviction; keep the session id for stats and save/restore.
- **Not covered yet:** a 100K+ prefix, truly concurrent sessions, and SGLang.

**vLLM (`qwen3.6-vllm` config, 32K window, TP=2, `--enable-prefix-caching`), 12.2K-token prefix
(`scripts/cache_probe_vllm.py`):**

| Case | Cached tokens | Time |
|---|---|---|
| Cold | 0 | 2.1 s |
| Same prefix, new tail | 11616 of 12196 (95%) | 0.13 s |
| One word changed at the start | 0 | 2.1 s |
| A second, different prefix | 0, then 11616 on repeat | 2.1 s, 0.13 s |
| Back to the first prefix | 11616 (still cached) | 0.13 s |

- Same rule as llama.cpp: an append-only prefix is cached, a change at the start loses everything.
  Reuse is block-aligned (580 tokens left uncached here), so the hit is 95%, not 99.9%.
- Several prefixes coexist, so there is no slot to manage: automatic, nothing to pin.
- vLLM reports `cached_tokens` in `usage.prompt_tokens_details` (needs `--enable-prompt-tokens-details`,
  not in the current profile) and counters on `/metrics`. llama.cpp reports `timings.cache_n`.
- No save/restore exists on vLLM.

### 5.5 The plugin never sees the engine

The user picks a model from `GET /v1/models` and starts working; engine kinds are not a plugin concern.
So the plugin sends only the standard `/v1/chat/completions` request with a stable prefix, and every
engine-specific thing lives in the gateway:

- the gateway sets `cache_prompt`, and `--enable-prompt-tokens-details` on vLLM profiles;
- it **normalizes** the hit report into one field on every response, `usage.prompt_tokens_details.
  cached_tokens` (translated from `timings.cache_n` for llama.cpp), so the plugin has one way to read it;
- save/restore stays an internal gateway optimisation (survive eviction, resume a session), used where
  the engine supports it and silently skipped where not; the plugin has no cache-slot API to call;
- `GET /v1/models` may carry optional capability hints (context window, tools, vision) the plugin
  reads when present, and falls back to defaults when absent.

This replaces the plugin-facing slot API in 5.3: no `X-Session-Id` pinning, no `/v1/cache/...` calls.

## 6. Plugin architecture

- Kotlin, IntelliJ Platform Gradle Plugin 2.x, JDK 21 (installed: Temurin 21), target IDEA 2025.x
  Community+Ultimate. Gradle wrapper (no global Gradle installed).
- `agent-core` (plain Kotlin, no IDE dependency): loop, tool contract, memory, prompt builder, cache
  planner, gateway client. Unit-testable and reusable (a CLI could drive the same core).
- `plugin` module: tool window (chat, steps timeline, diff approval, memory view, cache status), settings
  page (gateway URL, model, budgets), PSI/diagnostics tools, `WriteCommandAction` editing.
- Credentials: gateway API key in IntelliJ `PasswordSafe`, never in settings XML or the repo.
- Ships to the repo as a separate project; this doc stays with the gateway because section 5.3 changes it.

## 7. Gateway touchpoints

| Change | Where |
|---|---|
| session -> slot pinning, `cache_prompt` default | `app/routers` /v1 proxy |
| slot save/restore/stats endpoints | `app/engine/supervisor.py`, new router |
| `--slot-save-path`, `slots_supported` probe | `app/engine/profiles.py` |
| per-user usage of cached vs fresh tokens | existing usage accounting |

`/v1` stays a passthrough for every other client; new behaviour is opt-in by header.

## 8. Phases

0. **Probe (before any code):** measure on homelabai that prefix reuse and slot save/restore work for
   the models we will use; record cached-vs-fresh prefill time. Decides how much of 5.3 is worth building.
1. **Core + CLI harness:** ReAct loop, tools, budgets, prompt builder, against the gateway; run on a
   scripted task set. No IDE yet.
2. **Plugin shell:** tool window, settings, streaming, read-only tools.
3. **Editing:** diff approval, undoable edits, diagnostics verify loop.
4. **Memory:** tiers, `remember`, conflict gate, pending-changes view.
5. **Cache:** gateway slot API, hit-rate display, compaction.
6. **Polish:** embeddings recall, run history, packaging, Marketplace-ready or private repository.

## 9. How we know it works

- **Task suite:** 20-30 small, real edit tasks across a couple of projects, each with a pass/fail check.
  Report success rate, steps, wall time per model. Run before and after each change (same discipline as
  `scripts/routing_eval`).
- **Cache:** prefix hit rate per turn (target > 90% outside compaction), time-to-first-token with and
  without reuse.
- **Memory:** a scripted conflict set (contradicting, refining, unrelated facts) must produce a prompt for
  the contradicting ones and none for the rest.

## 10. Open questions for the owner

1. Name for the plugin (the gateway's own product name is still undecided).
2. Which model is the default driver: `qwen3-coder:30b` (fast, dense) or `qwen3-next-80b` (stronger,
   hybrid, slot caching uncertain)?
3. Private repo only, or JetBrains Marketplace eventually?
4. Should `run_command` allow-list live in project memory (shared) or per user?
