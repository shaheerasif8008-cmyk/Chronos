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

BROWSER_NAVIGATE = _fn(
    "browser__navigate",
    "Open a URL in a persistent isolated browser session. Requires session consent and per-task approval for sensitive sites.",
    {
        "url": {"type": "string", "description": "URL to open."},
        "session_id": {"type": "string", "description": "Existing browser session id. Omit to create one.", "default": ""},
        "consent": {"type": "object", "description": "Session purpose and allowed_domains when creating a session.", "default": {}},
    },
    ["url"],
)

BROWSER_CLICK = _fn("browser__click", "Click a selector in a persistent browser session.", {"session_id": {"type": "string"}, "selector": {"type": "string"}}, ["session_id", "selector"])
BROWSER_TYPE = _fn("browser__type", "Type text into a selector in a persistent browser session.", {"session_id": {"type": "string"}, "selector": {"type": "string"}, "text": {"type": "string"}}, ["session_id", "selector", "text"])
BROWSER_SELECT = _fn("browser__select", "Select an option in a persistent browser session.", {"session_id": {"type": "string"}, "selector": {"type": "string"}, "value": {"type": "string"}}, ["session_id", "selector", "value"])
BROWSER_SCROLL = _fn("browser__scroll", "Scroll a persistent browser session.", {"session_id": {"type": "string"}, "x": {"type": "integer", "default": 0}, "y": {"type": "integer", "default": 700}}, ["session_id"])
BROWSER_WAIT = _fn("browser__wait", "Wait for a selector or timeout in a persistent browser session.", {"session_id": {"type": "string"}, "selector": {"type": "string", "default": ""}, "milliseconds": {"type": "integer", "default": 1000}}, ["session_id"])
BROWSER_EXTRACT = _fn("browser__extract", "Extract visible text from a selector and mark it as untrusted browser content.", {"session_id": {"type": "string"}, "selector": {"type": "string", "default": "body"}}, ["session_id"])
BROWSER_SCREENSHOT = _fn("browser__screenshot", "Capture the current browser viewport and persist it on the session.", {"session_id": {"type": "string"}}, ["session_id"])
BROWSER_DOWNLOAD = _fn("browser__download", "Click a selector and record the resulting browser download.", {"session_id": {"type": "string"}, "selector": {"type": "string"}}, ["session_id", "selector"])
BROWSER_UPLOAD = _fn("browser__upload", "Upload a task-accessible file through a file input selector.", {"session_id": {"type": "string"}, "selector": {"type": "string"}, "path": {"type": "string"}}, ["session_id", "selector", "path"])
BROWSER_READ_DOM = _fn("browser__read_dom", "Read DOM HTML from a selector and mark it as untrusted browser content.", {"session_id": {"type": "string"}, "selector": {"type": "string", "default": "body"}}, ["session_id"])
BROWSER_GET_STATE = _fn("browser__get_state", "Return current URL, screenshot, takeover, download, and consent state for a browser session.", {"session_id": {"type": "string"}}, ["session_id"])
BROWSER_CLOSE = _fn("browser__close", "Close a persistent browser session.", {"session_id": {"type": "string"}}, ["session_id"])
BROWSER_REQUEST_TAKEOVER = _fn("browser__request_takeover", "Pause automation and request user takeover for MFA, CAPTCHA, or manual input.", {"session_id": {"type": "string"}, "reason": {"type": "string"}}, ["session_id", "reason"])

BROWSER_OPERATOR_TOOLS = [
    BROWSER_NAVIGATE,
    BROWSER_CLICK,
    BROWSER_TYPE,
    BROWSER_SELECT,
    BROWSER_SCROLL,
    BROWSER_WAIT,
    BROWSER_EXTRACT,
    BROWSER_SCREENSHOT,
    BROWSER_DOWNLOAD,
    BROWSER_UPLOAD,
    BROWSER_READ_DOM,
    BROWSER_GET_STATE,
    BROWSER_CLOSE,
    BROWSER_REQUEST_TAKEOVER,
]

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
    "Search emails in the connected Gmail account using Gmail query syntax. "
    "Use this before answering any question about inbox contents, recent emails, "
    "email summaries, senders, subjects, or whether an email exists. Ground the answer "
    "only in returned threads/messages; if result_count is 0, say no matching emails were found.",
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
    "Returns the sub-agent's final answer. For multiple independent workstreams, call this tool "
    "multiple times in the same assistant step so the sub-agents run in parallel; do not spawn "
    "them sequentially when the roles are already known. Max depth: 3 levels.",
    {
        "goal": {
            "type": "string",
            "description": "Clear, specific, self-contained goal for the sub-agent to accomplish.",
        },
        "model": {
            "type": "string",
            "description": "Explicit chat model selector id for the sub-agent.",
            "enum": ["gpt-5.4-mini", "gpt-5.4-nano", "deepseek-v4-pro", "deepseek-v4-flash"],
            "default": "gpt-5.4-mini",
        },
    },
    ["goal"],
)

START_TASK = _fn(
    "start_task",
    "Promote the current request into a durable background task. Call this ONLY "
    "when the work is large and long-running (multi-step research, batch outreach, "
    "anything that will take minutes or spawn sub-agents). The task runs in the "
    "background, survives disconnects, streams its activity, and routes risky "
    "actions through approvals. For quick questions or a single lookup, just "
    "answer or use a tool directly — do NOT call start_task.",
    {
        "goal": {"type": "string", "description": "Clear, self-contained goal for the durable task."},
    },
    ["goal"],
)

# ── Registry sets ─────────────────────────────────────────────────────────────

#: Full tool set available to top-level agent loops.
ALL_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    *BROWSER_OPERATOR_TOOLS,
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
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
    *BROWSER_OPERATOR_TOOLS,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
]

#: Names that always need explicit human approval before execution.
ALWAYS_APPROVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"gmail__send", "twitter__post", "linkedin__post", "website__publish"}
)

_SUBAGENT_TOOL_NAME = "spawn__subagent"

#: Tools available to an inline chat turn: quick tools + promotion. No recursive
#: sub-agent spawning inline — large work promotes via start_task instead.
INLINE_CHAT_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    *BROWSER_OPERATOR_TOOLS,
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    START_TASK,
]

_START_TASK_TOOL_NAME = "start_task"


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
