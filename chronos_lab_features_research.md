# Chronos — External Lab Feature Scan

Survey of current (mid-2026) developer features from Anthropic, OpenAI, Microsoft,
and Google, mapped to concrete Chronos integration points. Each item notes what
already exists in the codebase, the gap, and the seam/file to touch.

This is a research/planning doc, not a commitment. Sequencing recommendation is at
the bottom. References at the end.

---

## Tier 1 — High value, low friction (touches code we already own)

### 1. Prompt caching on `assemble_context()`
**Source:** Anthropic, OpenAI, Bedrock prompt caching.
Cache reads cost ~10% of input tokens (writes 1.25×); 30–50% input-cost cut on
agent loops and RAG with no quality change.

- **Current state:** `apps/api/core/context.py` already assembles layers and orders
  high-value grounding first, with token budgeting (`token_budget.py`) and history
  compaction (`_compact_history`). What's missing is explicit `cache_control`
  breakpoints and a stable→variable ordering guarantee for cache hits.
- **Gap:** The base system prompt + tool routing + persona + org context are stable
  per request and should form a cache prefix; memory, citations, task state, and the
  user turn are variable and must come *after* the breakpoint.
- **Action:** Add `cache_control` markers at the end of the stable block. Audit for
  any per-request token (timestamp, request id) injected high in the prompt — it
  invalidates the whole cache. Verify litellm forwards `cache_control` for the active
  provider.
- **Files:** `core/context.py`, `core/llm.py`.

### 2. Context editing / mid-task compaction for long-running tasks
**Source:** Anthropic context management (server-side context editing + memory tool).
Complements the Memory seam — context editing trims *transient* tool output; the
Memory seam (`core/memory.py`) stays the durable store.

- **Current state:** Conversation history compaction exists. Per-task working-context
  compaction during `TaskExecutor.run()` does not.
- **Gap:** Multi-hour tasks accumulate large `step_done` tool payloads in working
  context. Once a step is committed to DB, its raw payload can be trimmed.
- **Action:** Add a compaction/eviction step in the executor loop that drops
  committed tool-result payloads from the live context (DB retains them for audit).
- **Files:** `runtime/executor.py`, `runtime/agent_loop.py`, `runtime/checkpoints.py`.

### 3. OpenTelemetry GenAI semantic conventions
**Source:** OpenTelemetry GenAI SIG `gen_ai.*` conventions — natively ingested by
Datadog/New Relic/Dynatrace; the de-facto LLM observability standard.

- **Current state:** Every seam emits `audit.log` (`core/audit.py`); Langfuse is the
  configured LLM tracer.
- **Gap:** No standardized, replayable hierarchical trace per task/sub-agent.
- **Action:** Wrap `tool_broker.execute`, each LLM step, and `sub_agent.spawn_and_wait`
  in OTel spans using `gen_ai.*` attributes (operation, model, token counts, tool name,
  latency) alongside existing audit/Langfuse calls. Satisfies the "auditable" parity
  bar with an industry schema.
- **Files:** `core/tool_broker.py`, `core/llm.py`, `runtime/sub_agent.py`.

### 4. Strict structured outputs everywhere JSON is parsed
**Source:** OpenAI and Anthropic strict-schema structured outputs (guaranteed-valid
JSON against a schema).

- **Current state:** `core/structured_response.py` exists; memory extraction and
  planner paths rely on `response_format={"type":"json_object"}` + hand-parsing.
- **Gap:** Confirm strict schema enforcement (not just JSON mode) is used on the
  memory-extraction and planning paths to remove a class of parse failures.
- **Files:** `memory/extraction.py`, `runtime/planner.py`/`cognition.py`,
  `core/structured_response.py`.

---

## Tier 2 — Strategic / parity features

### 5. A2A (Agent2Agent) protocol — Linux Foundation standard
**Source:** Google ADK 1.0 + A2A; natively supported by ADK, LangGraph, CrewAI,
Semantic Kernel, AutoGen. MCP connects agents to tools; A2A connects agents to agents.

A2A's core abstraction — Tasks as first-class (submit → `task_id` → stream progress
via SSE, survives disconnects) — is *already* how Chronos's `tasks` table and
`/tasks/{id}/stream` work.

- **Action:** Add an A2A adapter (client + server) over the existing tasks router so
  Chronos interoperates in multi-vendor agent ecosystems. Minimal schema change.
- **Files:** `routers/tasks.py`, `runtime/task_runner.py`, new `connectors/a2a.py`.

### 6. Microsoft Agent Framework — middleware + graph workflows
**Source:** Microsoft Agent Framework 1.0 (GA Apr 2026), converging AutoGen +
Semantic Kernel. Notable ideas: middleware pipelines (telemetry, guardrails, state)
and explicit graph-based multi-agent orchestration.

- **Current state:** Safety limits, approval gating (Rule 8 gmail draft-first), and
  rate limits live in `tool_broker.py`/`autonomy.py` — these *are* middleware,
  currently hardcoded.
- **Action:** Formalize the broker's checks as a composable middleware chain so
  `SAFETY_LIMITS`, approvals, and rate limits become ordered, testable stages.
- **Files:** `core/tool_broker.py`, `core/autonomy.py`, `core/risk.py`.

### 7. Computer Use / CUA action-schema alignment
**Source:** OpenAI Responses computer-use tool; Anthropic computer use. Both use a
screenshot → action → screenshot loop with a shared action vocabulary.

- **Current state:** `connectors/browser.py` (Playwright sandbox).
- **Action:** Align the browser sub-agent's action vocabulary to the CUA action schema
  so model backends are swappable without rewriting the loop.
- **Files:** `connectors/browser.py`, `runtime/sub_agent.py`.

### 8. Agent Skills format alignment
**Source:** Anthropic Agent Skills — standardized `SKILL.md` + frontmatter, autonomous
*and* slash invocation.

- **Current state:** Chronos `skills/` already uses SKILL.md + metadata.json, lazy
  relevance loading (`skills/loader.py`, `skills/registry.py`) — nearly identical.
- **Action:** Align frontmatter to the public spec so community skills are drop-in.
- **Files:** `skills/loader.py`, `skills/registry.py`, skill metadata schema.

---

## Recommended sequencing

1. **#1, #2, #3** first — cost/reliability wins on code we own, no schema changes,
   directly serving the reliability-first mandate.
2. **#4** — cheap robustness; mostly verification + tightening.
3. **#5 (A2A)** — strategic enterprise bet; the task model already fits it.
4. **#6, #7, #8** — refactors that pay off as parity surface grows.

---

## References

- Claude Agent SDK: https://www.aiagentshub.net/blog/claude-agent-sdk-guide
- Anthropic prompt caching: https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Prompt caching cost/TTL 2026: https://aicheckerhub.com/anthropic-prompt-caching-2026-cost-latency-guide
- OpenAI tools for building agents: https://openai.com/index/new-tools-for-building-agents/
- OpenAI Responses + computer use: https://openai.com/index/equip-responses-api-computer-environment/
- OpenAI Agents SDK: https://openai.github.io/openai-agents-python/
- Microsoft Agent Framework overview: https://learn.microsoft.com/en-us/agent-framework/overview/
- Microsoft Agent Framework 1.0 GA: https://devblogs.microsoft.com/agent-framework/microsoft-agent-framework-version-1-0/
- Google ADK 1.0 + A2A: https://explore.n1n.ai/blog/google-adk-1-0-a2a-protocol-multi-agent-standard-2026-05-04
- A2A protocol upgrade: https://cloud.google.com/blog/products/ai-machine-learning/agent2agent-protocol-is-getting-an-upgrade
- OpenTelemetry GenAI observability: https://opentelemetry.io/blog/2026/genai-observability/
- OpenTelemetry for AI systems: https://uptrace.dev/blog/opentelemetry-ai-systems
