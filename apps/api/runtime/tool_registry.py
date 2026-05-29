"""
Tool Registry — Standard Anthropic/OpenAI tool schema definitions.

Every Chronos connector is exposed here as a typed tool schema.
The LLM reads these natively; the ToolBroker executes them.

Tool names use double-underscore (browser__search) so they convert
cleanly to dot notation for the broker (browser.search).  The helper
`to_broker_name` does this conversion.
"""
from __future__ import annotations

from typing import Any


def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Build a standard OpenAI-format function tool definition (litellm-compatible)."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


# ── Browser ───────────────────────────────────────────────────────────────────

BROWSER_SEARCH = _fn(
    "browser__search",
    "Search the web and return structured results with titles, URLs, and snippets. "
    "Use for research, market analysis, competitor lookups, and finding public information.",
    {
        "query": {"type": "string", "description": "The search query."},
        "max_results": {
            "type": "integer",
            "description": "Maximum number of results to return (default 8).",
            "default": 8,
        },
    },
    ["query"],
)

BROWSER_FETCH = _fn(
    "browser__fetch",
    "Fetch and parse a web page, returning its full text content. "
    "Use to read articles, documentation, company pages, or any public URL.",
    {
        "url": {"type": "string", "description": "The URL to fetch."},
    },
    ["url"],
)

BROWSER_EXTRACT_CONTACTS = _fn(
    "browser__extract_contacts",
    "Extract public contact information (name, email, title) from a company or team page.",
    {
        "url": {"type": "string", "description": "The company or team page URL."},
    },
    ["url"],
)

# ── Gmail ─────────────────────────────────────────────────────────────────────

GMAIL_DRAFT = _fn(
    "gmail__draft",
    "Create a Gmail draft. The draft is saved to the connected Gmail account and appears in Approvals "
    "for review before sending. Never sends immediately — always creates a draft first.",
    {
        "to": {"type": "string", "description": "Recipient email address."},
        "subject": {"type": "string", "description": "Email subject line."},
        "body": {"type": "string", "description": "Plain text email body."},
    },
    ["to", "subject", "body"],
)

GMAIL_SEARCH = _fn(
    "gmail__search",
    "Search emails in the connected Gmail account using Gmail query syntax.",
    {
        "query": {
            "type": "string",
            "description": "Gmail search query (e.g. 'from:user@example.com subject:proposal').",
        },
        "max_results": {
            "type": "integer",
            "description": "Maximum emails to return (default 10).",
            "default": 10,
        },
    },
    ["query"],
)

# ── Filesystem ────────────────────────────────────────────────────────────────

FS_LIST = _fn(
    "fs__list",
    "List files in the current task workspace.",
    {
        "path": {
            "type": "string",
            "description": "Directory path relative to workspace root (default: root).",
            "default": ".",
        },
    },
    [],
)

FS_READ = _fn(
    "fs__read",
    "Read a UTF-8 text file from the current task workspace.",
    {
        "path": {"type": "string", "description": "File path to read."},
    },
    ["path"],
)

FS_WRITE = _fn(
    "fs__write",
    "Write or overwrite a UTF-8 text file in the current task workspace.",
    {
        "path": {"type": "string", "description": "File path to write."},
        "content": {"type": "string", "description": "Full file content."},
    },
    ["path", "content"],
)

# ── Code execution ────────────────────────────────────────────────────────────

CODE_PYTHON = _fn(
    "code__python",
    "Execute Python code in a sandboxed environment. "
    "Use for data processing, analysis, and computation over local task files. "
    "Not for network access — use browser tools for that.",
    {
        "code": {"type": "string", "description": "Python code to execute."},
        "timeout_seconds": {
            "type": "integer",
            "description": "Maximum execution time in seconds (default 30).",
            "default": 30,
        },
    },
    ["code"],
)

# ── Artifacts ─────────────────────────────────────────────────────────────────

ARTIFACT_LIST = _fn(
    "artifact__list",
    "List current artifacts in this conversation, including key, title, kind, and version.",
    {},
    [],
)

ARTIFACT_READ = _fn(
    "artifact__read",
    "Read the current content of a conversation artifact by stable key. Use before revising an existing artifact.",
    {"key": {"type": "string", "description": "Stable artifact key, e.g. 'report.md' or 'demo.html'."}},
    ["key"],
)

ARTIFACT_WRITE = _fn(
    "artifact__write",
    "Create or update a conversation artifact under a stable key. Reusing a key creates a new version.",
    {
        "key": {"type": "string", "description": "Stable artifact key, ideally with an extension."},
        "content": {"type": "string", "description": "Full artifact content."},
        "title": {"type": "string", "description": "Human-readable title."},
        "kind": {
            "type": "string",
            "description": "Artifact kind; inferred from the key extension when omitted.",
            "enum": ["html", "markdown", "code", "data", "text", "image"],
        },
    },
    ["key", "content"],
)

# ── Documents ─────────────────────────────────────────────────────────────────

DOC_PARSE = _fn(
    "doc__parse",
    "Parse a document or image into text. Use on a file the user attached, a file in "
    "the task workspace, or an artifact produced earlier. Supports PDF, DOCX, XLSX, "
    "PPTX, CSV, TXT, and images (OCR). Returns a text preview plus metadata; the full "
    "text is stored and can be paged with doc__read.",
    {
        "artifact_id": {"type": "string", "description": "Artifact id of the file to parse."},
        "path": {"type": "string", "description": "Path in the task workspace to parse (alternative to artifact_id)."},
    },
    [],
)

DOC_READ = _fn(
    "doc__read",
    "Read more of an already-parsed document beyond the preview. Use when the preview "
    "was truncated and you need a specific section.",
    {
        "artifact_id": {"type": "string", "description": "Artifact id of the parsed document (the parsed_text artifact, or the source attachment)."},
        "char_offset": {"type": "integer", "description": "Start offset into the full text (default 0).", "default": 0},
        "max_chars": {"type": "integer", "description": "Maximum characters to return (default 8000).", "default": 8000},
    },
    ["artifact_id"],
)

# ── Sub-agent ─────────────────────────────────────────────────────────────────

SPAWN_SUBAGENT = _fn(
    "spawn__subagent",
    "Spawn an autonomous sub-agent with its own context window to accomplish a specific goal. "
    "Use for parallel workstreams, deep research on a single topic, or tasks that benefit from "
    "a fresh context. The sub-agent has access to browser and filesystem tools. "
    "Returns the sub-agent's final answer. Max depth: 3 levels.",
    {
        "goal": {
            "type": "string",
            "description": "Clear, specific, self-contained goal for the sub-agent to accomplish.",
        },
        "model": {
            "type": "string",
            "description": "Model tier for the sub-agent: 'agent' for complex tasks, 'fast' for simple ones.",
            "enum": ["agent", "fast"],
            "default": "agent",
        },
    },
    ["goal"],
)

# ── Registry sets ─────────────────────────────────────────────────────────────

#: Full tool set available to top-level agent loops.
ALL_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    ARTIFACT_LIST,
    ARTIFACT_READ,
    ARTIFACT_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    SPAWN_SUBAGENT,
]

#: Subset for sub-agents — no recursive spawning beyond depth 3.
SUBAGENT_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    ARTIFACT_LIST,
    ARTIFACT_READ,
    ARTIFACT_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
]

#: Names that always need explicit human approval before execution.
ALWAYS_APPROVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"gmail__send", "twitter__post", "linkedin__post", "website__publish"}
)

ARTIFACT_TOOL_NAMES: frozenset[str] = frozenset(
    {"artifact__list", "artifact__read", "artifact__write"}
)

_SUBAGENT_TOOL_NAME = "spawn__subagent"


def to_broker_name(registry_name: str) -> str:
    """Convert double-underscore registry name to dot notation for the ToolBroker.

    browser__search  →  browser.search
    gmail__draft     →  gmail.draft
    spawn__subagent  →  spawn__subagent  (handled before reaching broker)
    """
    if "__" in registry_name:
        return registry_name.replace("__", ".", 1)
    return registry_name


def tool_name(schema: dict[str, Any]) -> str:
    """Extract the name string from a tool schema dict."""
    return schema["function"]["name"]
