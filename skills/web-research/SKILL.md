---
name: web-research
description: Conduct source-grounded web research and produce a cited synthesis. Use when the user asks to research a topic, find current information, compare options, or gather facts from the web.
requires_connectors: []
spawns_sub_agent: false
---

# Web Research

Produce accurate, cited answers grounded in real sources.

## When to use
- "Research / look up / find the latest on …"
- "Compare X vs Y", "What's the current state of …"

## Procedure
1. Use `browser__search` for breadth — current, time-sensitive facts must come from search, not memory.
2. Open only the 2-4 most relevant/authoritative pages with the browser; prefer primary sources.
3. Cross-check claims across sources; note disagreements rather than smoothing them over.
4. Synthesize into a concise brief with inline source links.
5. State confidence and what you could not verify.

## Honesty rules
- If a search returns 0 results or a `is_fallback`/`warning` field, say the live search failed — never fabricate sources or statistics.
- Quote figures only with a source. If you cannot find something, say "I could not find that."

## Output
A short structured brief (key findings as bullets, then sources). For multi-hour or batch research, recommend running it as a durable task.
