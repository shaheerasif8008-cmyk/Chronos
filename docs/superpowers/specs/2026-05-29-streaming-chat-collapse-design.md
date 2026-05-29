# Collapse Chronos to a Single Streaming Agent Loop

**Date:** 2026-05-29
**Status:** Design — awaiting review
**Author:** Chronos engineering (pairing session)

---

## Problem

Chat is slow (30s–2min per reply), feels dumb despite a capable underlying model
(`agent_model = openrouter/deepseek/deepseek-v4-pro`), and is wrapped in governance
machinery that adds friction without value on ordinary messages.

The root cause is a single architectural decision: **there is no real chat path.**
Every substantive message is forced through layers of pre-generation
classification and planning before the model produces a single token, and the
model never streams.

For one message ("what's the latest news on diddy") the latency stack is:

1. `classify_intent` — blocking LLM call on the free, rate-limited fast model
   (`apps/api/core/intent.py:117`).
2. `TaskExecutor._preflight_and_route` — a second classifier LLM call
   (`preflight`), then optionally `create_plan` → the full **DAG planner/executor**
   (`apps/api/runtime/executor.py:121`).
3. `run_loop` iteration 0 — `route_tool` pre-guesses the first tool
   (`apps/api/runtime/agent_loop.py:993`).
4. Every iteration — `publish_reasoning_summary` fires *another* free-model LLM
   call (`apps/api/runtime/agent_loop.py:323`).
5. `_llm_step` runs with `stream=False` (`agent_loop.py:474`). The chat router
   waits for the entire loop to finish, then fake-streams the final answer 40
   characters at a time (`apps/api/routers/chat.py:344`).

Result: 5–8 sequential LLM round-trips — several on rate-limited free models with
exponential-backoff retries — with **zero** visible output until all of it
completes. That is the latency and the "dumb wrapper" feeling: a strong model
strangled by scaffolding, never seen to think.

Crucially, the native loop we want **already exists**. `agent_loop.py` is a
documented ReAct loop ("No upfront plan. The LLM emits tool_use blocks... decides
the next move"). It is simply buried under three vestigial pre-generation layers
and a per-step LLM tax, and it does not stream.

## Goal

One streaming path. Every message goes straight into the native loop. The model
receives the message, conversation context, and its tools, and **streams its
response immediately**. It decides what to do by acting, not by being labeled:

- Trivial question → streams the answer in one round-trip.
- Needs live info → emits a tool call (UI shows "Searching…"), then streams the answer.
- Genuinely large job → reaches for a sub-agent / durable task.

Planning is implicit. Governance is invisible until the model reaches for a
risky action. No classifier, no preflight, no DAG planner, no routing pre-call,
no per-step summary LLM call.

### Non-goals (explicitly out of scope for this change)

- Moving `fast_model` / `embedding_model` off the free OpenRouter tier (a config
  change; recommended separately — it still affects background extraction latency).
- Frontend rework beyond consuming the existing SSE event contract.
- Reworking the approval inbox, connectors, or memory subsystems.

## Decisions (resolved with product owner)

1. **Lazy task persistence.** A pure no-tool answer never creates a `tasks` row,
   Redis channel, or queue entry. Persistence begins only when the model reaches
   for a tool (or an approval gate fires). One code path; trivial chat is
   genuinely zero-overhead.
2. **Delete the DAG planner/executor** (`create_plan`, `preflight`, `_run_dag`,
   `_resume_dag`, and the planner module). The native loop handles multi-step work
   via implicit planning and sub-agents.
3. **No-LLM traces.** Drop the per-iteration `publish_reasoning_summary` model
   call. Surface concrete trace events (tool name, args preview, result summary)
   derived from data already in hand.

## Design

### Execution model

There are two consumers of the same native loop:

- **Chat turn (inline, streaming).** Runs *in the request* and streams tokens +
  trace events directly down the chat SSE response. No queue, no Redis relay for
  the common case. This is what the user types into.
- **Durable task (queued, background).** Runs via `task_runner` → `run_loop` for
  model-invoked sub-agents, explicitly started background work, and
  crash/approval resume. Emits activity through Redis as today.

A chat turn becomes a durable task lazily: the first tool call creates the `tasks`
row (for audit + approval resume), and from then on the turn is checkpointed like
any task. A turn that never calls a tool is never persisted as a task — only the
user and assistant `messages` rows are written, exactly like a normal chat.

### Streaming with tools — the one subtle part

`_llm_step` gains a streaming mode (`stream=True`). While consuming the stream:

- Relay `delta.content` chunks to the caller as `token` events.
- Accumulate `delta.tool_calls` fragments (id / name / arguments) **without**
  relaying them.

At stream end:

- If tool calls accumulated → this turn is an action turn. Execute them (governed
  by the broker), emit `trace` events, append results, loop again.
- Else → this turn is the final answer. It has already been streamed; persist the
  assistant message and finish.

Because a tool-call turn produces no user-visible `content`, the user only ever
sees real answer tokens or a "Searching…"/tool trace — never a half-streamed
answer that turns out to be a tool call.

### Components and boundaries

**`apps/api/core/intent.py` — DELETE.**
Remove the module. Drop `classify_intent` usage in the chat router and the
`test_chat_routing.py` tests that assert on it.

**`apps/api/routers/chat.py` — `send_message`.**
- Remove `classify_intent`, `_is_trivial_chat`, `_TRIVIAL_CHAT_PHRASES`,
  `_TOOL_HINT_WORDS`, and the `route_through_loop` branch.
- Keep the `explicit_memory` shortcut ("remember that…") for now — it is a
  deterministic command handler, not routing scaffolding. (Follow-up: make memory
  a tool the model calls; out of scope here.)
- New flow: permission check → save user message → `assemble_context` (with
  bounded memory retrieval) → stream the agent turn.
- The fake-streaming `_agent_loop_stream` (subscribe Redis → wait for
  `task_complete` → chunk) is replaced by direct inline streaming.

**`apps/api/runtime/agent_loop.py`.**
- Add a streaming turn entrypoint (async generator) used by chat. It drives the
  same loop body but yields `token` / `trace` / `artifact` / `awaiting_approval`
  / `done` events, and creates the task row lazily on first tool call.
- `_llm_step`: add `stream=True` path that yields tokens and accumulates tool
  calls (above).
- Remove `route_tool` call and `_append_routing_instruction` at iteration 0.
- Remove `publish_reasoning_summary` calls. Keep the existing concrete trace
  events (`tool_call`, `tool_result`, `tool_error`, `artifact`).
- Revise `_agent_system_message`: the current prompt ("You are Chronos running an
  autonomous enterprise task") is the wrong frame for chat and stiffens answers.
  Replace with one coherent assistant identity that serves both quick answers and
  multi-step work, keeps the search-honesty rules, and keeps the tool manifest.
- Keep: approval gating, sub-agent spawn, checkpointing, resume-after-approval
  (durable tier).

**`apps/api/runtime/executor.py`.**
- Delete the DAG path: `_preflight_and_route`, `_run_dag`, `_resume_dag`,
  `_execute_dag_step`, `_run_think_step`, `_deterministic_think`,
  `_handle_approval_gate` (DAG variant), `_maybe_replan`, `_merge_replan`,
  `_checkpoint_dag`, condition evaluation (`_safe_condition`, `_eval_condition_node`,
  `_compare_values`, `_to_namespace`), `resolve_args`, `_lookup_*`, and the DAG
  state helpers.
- `TaskExecutor.run(task_id)` → load task, short-circuit on terminal status,
  `run_loop(task)`.
- `TaskExecutor.resume(task_id)` → `awaiting_approval`/`paused` →
  `resume_after_approval`; has `agent_history` → `run_loop`; else → `run`.
- Preserve the re-exported names other modules import (`update_task`,
  `insert_task`, `activity_channel`, `AGENT_LOOP_APPROVAL_STEP_ID`,
  `approvals_ready_for_drafting`).

**`apps/api/runtime/planner.py` — DELETE.**
Remove the module and its tests. Audit and remove all imports (`executor.py`,
tests, any `tool_registry`/router references).

**`apps/api/core/llm.py`.**
- Add a streaming-with-tools helper backing the streaming `_llm_step`.
- Keep `complete_json` / `complete_text` (still used by background memory
  extraction). No hot-path free-model calls remain after `classify_intent` and
  `reasoning_summary` are gone.

**Memory hot path.**
`assemble_context` → `memory.retrieve` → `embed` (free model) runs before the
first token. Keep it, bounded by the existing `memory_retrieve_timeout_seconds`
(1.5s) so it can never stall the turn. (Moving embeddings off the free tier is a
recommended follow-up, not part of this change.)

### Data flow — chat turn

1. `POST /chat/message`.
2. Permission check; save user message; `assemble_context` (bounded memory retrieve).
3. Stream the agent turn:
   - Loop: stream a model step with tools.
     - Text-only turn → relay tokens → save assistant message → fire background
       memory extraction → `done`.
     - Tool-call turn → lazily create the task row (first time) → emit
       `trace{tool_call}` → broker execute → emit `trace{tool_result}` (or
       `awaiting_approval` if the broker gates) → append → continue.
4. SSE events emitted: `conversation`, `task_created` (only once a task is lazily
   created), `trace`, `token`, `artifact`, `awaiting_approval`, `done`. The
   existing frontend contract is preserved.

### Error handling

- Model error mid-turn → emit an error trace, persist a friendly assistant
  message, `done`.
- Tool error → returned to the model as a tool result; the model decides the next
  step (unchanged).
- `ApprovalRequired` → the task row now exists; persist loop state, emit
  `awaiting_approval`, end the turn. The approvals router resumes via the durable
  `run_loop`, which posts the result to the conversation as a new assistant
  message (unchanged resume path).
- Chat turns are best-effort: a dropped SSE connection does not resume a no-tool
  turn (matches competitor behavior). Durable tasks resume on restart as today.
- `MAX_ITERATIONS` guard retained.

## Testing & verification

**Unit**
- Streaming step: a content-only stream yields tokens and no tool calls; a
  tool-call stream accumulates calls and yields no tokens; a mixed/interleaved
  stream is handled.
- Lazy persistence: a no-tool turn writes no `tasks` row; the first tool call
  creates exactly one.
- Governance intact: an `always_approval` tool (e.g. `linkedin.post`) and a
  safety-limited tool still raise `ApprovalRequired` and open the gate.
- Deletion safety: sub-agent spawn and crash/approval resume still work with the
  DAG path removed.

**Update / remove**
- `test_chat_routing.py` (intent classifier gone), planner tests, DAG executor
  tests in `test_orchestration_category1.py` / related.

**Manual proof (the bar for "done")**
1. "what's the latest news on diddy" → first token in low single-digit seconds;
   one model round-trip + one search; honest if search fails.
2. "explain how OAuth works" → streams immediately; **no** `tasks` row created.
3. "research 50 AI law firms and draft outreach" → creates a durable task,
   streams activity, lands drafts in the approval inbox.

**Quality gates**
- `pytest` green; `ruff check` and `mypy src`/`apps` clean for touched files.

## Risks

- **Streaming + tool-call parsing across providers.** litellm's streamed
  `tool_calls` deltas must be accumulated correctly for the OpenRouter/deepseek
  path. Mitigation: a focused unit test against recorded stream chunks; fall back
  to a non-streaming step if a provider doesn't support streamed tool calls.
- **Large deletion (executor/planner).** Risk of breaking sub-agent/resume
  imports. Mitigation: preserve re-exported names; run the full suite; the three
  manual scenarios cover the live paths.
- **Lazy task row + approval.** The approval path depends on a task row existing;
  ensure the row is created before the gate opens (it is — gate only fires on a
  tool call, which is the lazy-create trigger).
