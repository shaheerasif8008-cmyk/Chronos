from __future__ import annotations

import json
from typing import Any


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def compress_connector_output(
    output: Any,
    *,
    metadata: dict[str, Any],
    max_tokens: int = 800,
    query: str | None = None,
) -> dict[str, Any]:
    raw = json.dumps(output, sort_keys=True, default=str)
    token_estimate = estimate_tokens(raw)
    references: list[dict[str, Any]] = []
    chunks: list[str] = []
    chunk_size = max(200, max_tokens * 4)
    for index in range(0, len(raw), chunk_size):
        chunk = raw[index : index + chunk_size]
        if query and query.lower() not in chunk.lower():
            continue
        references.append({"chunk_index": len(chunks), "offset": index})
        chunks.append(chunk)
    if not chunks:
        chunks = [raw[:chunk_size]]
        references = [{"chunk_index": 0, "offset": 0}]

    summary = "\n".join(chunks)
    truncated = estimate_tokens(summary) > max_tokens or token_estimate > max_tokens
    while estimate_tokens(summary) > max_tokens and len(summary) > 32:
        summary = summary[: int(len(summary) * 0.75)]
    return {
        "summary": summary,
        "metadata": metadata,
        "references": references,
        "token_estimate": min(estimate_tokens(summary), max_tokens),
        "raw_token_estimate": token_estimate,
        "truncated": truncated,
    }
