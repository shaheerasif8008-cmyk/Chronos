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

DOC_SUMMARIZE = _fn(
    "doc__summarize",
    "Summarize a document with verifiable citations. Each section of the summary "
    "is anchored to a verbatim quote from the source (char offsets included). "
    "Supports the same formats as doc__parse. Returns an honest warning when the "
    "document cannot be parsed.",
    {
        "artifact_id": {"type": "string", "description": "Artifact id of the document to summarize."},
    },
    ["artifact_id"],
)

DOC_COMPARE = _fn(
    "doc__compare",
    "Compare two documents and return a structured list of similarities and differences "
    "with verifiable citations into both sources.",
    {
        "artifact_id_a": {"type": "string", "description": "Artifact id of the first document."},
        "artifact_id_b": {"type": "string", "description": "Artifact id of the second document."},
    },
    ["artifact_id_a", "artifact_id_b"],
)

# ── Image generation ──────────────────────────────────────────────────────────

IMAGE_GENERATE = _fn(
    "image__generate",
    "Generate one or more images from a text description. "
    "Returns image artifacts that render inline in the chat. "
    "Use for illustrations, diagrams, mockups, or any visual output the user requests.",
    {
        "prompt": {
            "type": "string",
            "description": "Detailed text description of the image(s) to generate.",
        },
        "size": {
            "type": "string",
            "description": "Image dimensions (e.g. '1024x1024', '512x512'). Default: '1024x1024'.",
            "default": "1024x1024",
        },
        "count": {
            "type": "integer",
            "description": "Number of images to generate (1–4). Default: 1.",
            "default": 1,
        },
        "style": {
            "type": "string",
            "description": "Optional style hint (e.g. 'photorealistic', 'illustration', 'sketch').",
        },
    },
    ["prompt"],
)

IMAGE_EDIT = _fn(
    "image__edit",
    "Edit an existing image artifact using a natural-language instruction. "
    "Creates a new version of the source artifact non-destructively — original bytes are preserved. "
    "Use for retouching, style changes, background swaps, or any modification to an image the user already has.",
    {
        "artifact_id": {
            "type": "string",
            "description": "Artifact id of the source image to edit.",
        },
        "prompt": {
            "type": "string",
            "description": "Natural-language edit instruction (e.g. 'make the sky purple', 'remove the background').",
        },
        "mask": {
            "type": "string",
            "description": "Optional mask: an artifact id of a mask image, or a base64-encoded mask. "
                           "Transparent areas indicate regions to edit.",
        },
        "operation": {
            "type": "string",
            "description": "Edit operation type: 'edit' (default), 'variation', or 'background'.",
            "enum": ["edit", "variation", "background"],
            "default": "edit",
        },
    },
    ["artifact_id", "prompt"],
)

# ── Voice (STT / TTS) ─────────────────────────────────────────────────────────

VOICE_TRANSCRIBE = _fn(
    "voice__transcribe",
    "Transcribe an uploaded audio file to text (speech-to-text). "
    "Pass the artifact_id of an uploaded audio attachment. "
    "Returns the transcript text and a persistent transcript artifact. "
    "Use when the user uploads a voice memo, meeting recording, or any audio they want transcribed.",
    {
        "artifact_id": {
            "type": "string",
            "description": "Artifact id of the uploaded audio file to transcribe.",
        },
        "audio_b64": {
            "type": "string",
            "description": "Alternative to artifact_id: raw audio encoded as base64.",
        },
        "mime_type": {
            "type": "string",
            "description": "MIME type of the audio (e.g. 'audio/mpeg', 'audio/webm'). "
                           "Required only when using audio_b64.",
        },
        "conversation_id": {
            "type": "string",
            "description": "Conversation id to link the transcript artifact to (optional).",
        },
    },
    [],
)

VOICE_SPEAK = _fn(
    "voice__speak",
    "Convert text to speech and return an audio artifact the user can play. "
    "Use when the user requests an audio version of a response, a reading of text, "
    "or any synthesised narration.",
    {
        "text": {
            "type": "string",
            "description": "Text to convert to speech.",
        },
        "voice": {
            "type": "string",
            "description": "Voice identifier (e.g. 'alloy', 'echo', 'fable', 'onyx', 'nova', 'shimmer'). "
                           "Default: 'alloy'.",
        },
        "conversation_id": {
            "type": "string",
            "description": "Conversation id to link the audio artifact to (optional).",
        },
    },
    ["text"],
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
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
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
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
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
    GMAIL_DRAFT,
    GMAIL_SEARCH,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
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
