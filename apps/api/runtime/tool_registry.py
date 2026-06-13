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

BROWSER_LOGIN_TASK = _fn(
    "browser__login_task",
    "Start a governed website task that requires login. Opens the login URL in a persistent isolated browser session, "
    "requests user takeover for credentials/MFA/CAPTCHA, and then the agent should continue with normal browser tools "
    "after the user hands the session back. Never ask the user to paste passwords into chat.",
    {
        "login_url": {"type": "string", "description": "The website login URL to open."},
        "task": {"type": "string", "description": "The task to perform after login is complete."},
        "session_id": {"type": "string", "description": "Existing browser session id. Omit to create one.", "default": ""},
        "consent": {"type": "object", "description": "Session purpose and allowed_domains when creating a session.", "default": {}},
    },
    ["login_url", "task"],
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
    BROWSER_LOGIN_TASK,
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

# ── Previous chat history ─────────────────────────────────────────────────────

CHAT_HISTORY_SEARCH = _fn(
    "chat_history__search",
    "Search this user's previous Chronos conversations by exact keywords in titles and message content. "
    "Use when the user asks what was discussed before, wants to find an earlier chat by topic, "
    "or asks to continue a prior conversation. Results are scoped to the current member and organization.",
    {
        "query": {
            "type": "string",
            "description": "Specific keyword or phrase to search for in previous chat titles and messages.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum conversations to return, capped by the broker connector.",
            "default": 10,
        },
    },
    ["query"],
)

CHAT_HISTORY_RECENT = _fn(
    "chat_history__recent",
    "Retrieve this user's most recent previous Chronos conversations with compact transcript excerpts. "
    "Use for requests like 'summarize the last 10 chats' or 'what have we talked about recently'. "
    "Results are scoped to the current member and organization.",
    {
        "limit": {
            "type": "integer",
            "description": "Maximum recent conversations to return, capped by the broker connector.",
            "default": 10,
        },
    },
    [],
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

# ── Cloud and local computer ──────────────────────────────────────────────────

COMPUTER_CREATE_SESSION = _fn(
    "computer__create_session",
    "Create a durable cloud computer workspace for a task, with sandboxed filesystem and terminal state.",
    {"purpose": {"type": "string", "description": "Why this computer session is needed.", "default": "computer task"}},
    [],
)

COMPUTER_EXEC = _fn(
    "computer__exec",
    "Run a shell command inside the sandboxed cloud computer workspace. Commands are audited, timed out, and resource-limited.",
    {
        "session_id": {"type": "string", "description": "Cloud computer session id. Omit to create one.", "default": ""},
        "command": {"type": "string", "description": "Shell command to run inside the workspace."},
        "timeout_seconds": {"type": "integer", "description": "Timeout in seconds, capped at 30.", "default": 10},
    },
    ["command"],
)

COMPUTER_LIST_FILES = _fn("computer__list_files", "List files in a cloud computer workspace.", {"session_id": {"type": "string"}, "path": {"type": "string", "default": "."}}, ["session_id"])
COMPUTER_READ_FILE = _fn("computer__read_file", "Read a UTF-8 file from a cloud computer workspace.", {"session_id": {"type": "string"}, "path": {"type": "string"}}, ["session_id", "path"])
COMPUTER_WRITE_FILE = _fn("computer__write_file", "Write a UTF-8 file inside a cloud computer workspace.", {"session_id": {"type": "string"}, "path": {"type": "string"}, "content": {"type": "string"}}, ["session_id", "path", "content"])
COMPUTER_INSTALL_PACKAGE = _fn(
    "computer__install_package",
    "Install a package inside the cloud computer workspace using pip or npm. The command is audited and resource-limited.",
    {
        "session_id": {"type": "string"},
        "manager": {"type": "string", "enum": ["pip", "npm"], "default": "pip"},
        "package": {"type": "string"},
        "timeout_seconds": {"type": "integer", "default": 30},
    },
    ["session_id", "package"],
)
COMPUTER_SCREENSHOT = _fn("computer__screenshot", "Capture the cloud computer desktop screenshot, or return a truthful degraded state if no desktop runtime is attached.", {"session_id": {"type": "string"}}, ["session_id"])
COMPUTER_EXPORT_ARTIFACT = _fn("computer__export_artifact", "Export a file or directory from a cloud computer workspace as a durable artifact.", {"session_id": {"type": "string"}, "path": {"type": "string"}, "title": {"type": "string", "default": ""}, "kind": {"type": "string", "default": "file"}, "mime_type": {"type": "string", "default": "application/octet-stream"}}, ["session_id", "path"])

LOCAL_COMPUTER_GRANT = _fn("local_computer__grant", "Authorize a local folder for this task's desktop bridge actions.", {"folder_path": {"type": "string"}, "purpose": {"type": "string", "default": "local computer task"}}, ["folder_path"])
LOCAL_COMPUTER_LIST_FILES = _fn("local_computer__list_files", "List files inside an authorized local folder grant.", {"grant_id": {"type": "string"}, "path": {"type": "string", "default": "."}}, ["grant_id"])
LOCAL_COMPUTER_READ_FILE = _fn("local_computer__read_file", "Read a file inside an authorized local folder grant.", {"grant_id": {"type": "string"}, "path": {"type": "string"}}, ["grant_id", "path"])
LOCAL_COMPUTER_EXEC = _fn("local_computer__exec", "Run an approved shell command inside an authorized local folder. Requires a human approval record.", {"grant_id": {"type": "string"}, "command": {"type": "string"}, "timeout_seconds": {"type": "integer", "default": 10}}, ["grant_id", "command"])
LOCAL_COMPUTER_OPEN_APP = _fn("local_computer__open_app", "Request opening a local app through the desktop bridge. Requires a human approval record.", {"grant_id": {"type": "string"}, "app": {"type": "string"}}, ["grant_id", "app"])
LOCAL_COMPUTER_REVOKE = _fn("local_computer__revoke", "Revoke an authorized local folder grant.", {"grant_id": {"type": "string"}}, ["grant_id"])

COMPUTER_TOOLS = [
    COMPUTER_CREATE_SESSION,
    COMPUTER_EXEC,
    COMPUTER_LIST_FILES,
    COMPUTER_READ_FILE,
    COMPUTER_WRITE_FILE,
    COMPUTER_INSTALL_PACKAGE,
    COMPUTER_SCREENSHOT,
    COMPUTER_EXPORT_ARTIFACT,
    LOCAL_COMPUTER_GRANT,
    LOCAL_COMPUTER_LIST_FILES,
    LOCAL_COMPUTER_READ_FILE,
    LOCAL_COMPUTER_EXEC,
    LOCAL_COMPUTER_OPEN_APP,
    LOCAL_COMPUTER_REVOKE,
]

# ── Repo workspace ────────────────────────────────────────────────────────────

REPO_CLONE = _fn(
    "repo__clone",
    "Import a repository into the current task workspace. Supports local workspace paths and public GitHub HTTPS clone URLs.",
    {
        "source_path": {"type": "string", "description": "Local path under the configured Chronos workspace root.", "default": ""},
        "source_url": {"type": "string", "description": "Public https://github.com/<owner>/<repo> URL.", "default": ""},
        "repo_path": {"type": "string", "description": "Workspace-relative destination path. Default: repos/imported.", "default": "repos/imported"},
        "timeout_seconds": {"type": "integer", "description": "Clone timeout, capped at 60 seconds.", "default": 30},
    },
    [],
)

REPO_OPEN_FIXTURE = _fn(
    "repo__open_fixture",
    "Open a bundled fixture repository inside the current task workspace. "
    "This is the supported repo-workspace MVP: no arbitrary clone or host access.",
    {
        "name": {"type": "string", "description": "Fixture repo name. Default: python_bug.", "default": "python_bug"},
        "repo_path": {"type": "string", "description": "Workspace-relative destination path. Default: repos/<name>.", "default": ""},
    },
    [],
)

REPO_CREATE_BRANCH = _fn(
    "repo__create_branch",
    "Create or reset a branch in the current task repo workspace.",
    {
        "branch": {"type": "string", "description": "Branch name, for example fix/bug."},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    ["branch"],
)

REPO_LIST_FILES = _fn(
    "repo__list_files",
    "List source files in the current task repo workspace, excluding git and Chronos metadata.",
    {
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    [],
)

REPO_READ_FILE = _fn(
    "repo__read_file",
    "Read a UTF-8 source file from the current task repo workspace.",
    {
        "path": {"type": "string", "description": "Repo-relative file path."},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    ["path"],
)

REPO_WRITE_FILE = _fn(
    "repo__write_file",
    "Write or overwrite a UTF-8 source file in the current task repo workspace.",
    {
        "path": {"type": "string", "description": "Repo-relative file path."},
        "content": {"type": "string", "description": "Full replacement file content."},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    ["path", "content"],
)

REPO_RUN_TESTS = _fn(
    "repo__run_tests",
    "Run the constrained repo test loop. Only pytest commands are accepted; shell operators and absolute paths are rejected.",
    {
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
        "command": {"type": "string", "description": "Optional pytest command, for example 'pytest -q tests/test_app.py'.", "default": "pytest -q"},
        "timeout_seconds": {"type": "integer", "description": "Max runtime, capped at 30 seconds.", "default": 20},
    },
    [],
)

REPO_DIFF = _fn(
    "repo__diff",
    "Return git diff for the current task repo workspace.",
    {
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    [],
)

REPO_STATUS = _fn(
    "repo__status",
    "Return current branch and git status for the repo workspace.",
    {
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    [],
)

REPO_COMMIT = _fn(
    "repo__commit",
    "Commit all repo workspace changes with Chronos bot identity and return the commit SHA.",
    {
        "message": {"type": "string", "description": "Commit message."},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    ["message"],
)

REPO_CREATE_PR = _fn(
    "repo__create_pr",
    "Create an approved pull-request request artifact for the repo workspace. Without approval_id it returns approval_required.",
    {
        "title": {"type": "string", "description": "PR title."},
        "body": {"type": "string", "description": "PR body.", "default": ""},
        "base": {"type": "string", "description": "Base branch.", "default": "main"},
        "head": {"type": "string", "description": "Head branch. Defaults to current branch.", "default": ""},
        "approval_id": {"type": "string", "description": "Approval record id authorizing PR creation.", "default": ""},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    ["title"],
)

REPO_REVIEW = _fn(
    "repo__review",
    "Review the current repo diff and write an actionable code-review artifact with inline findings.",
    {
        "title": {"type": "string", "description": "Review title.", "default": "Code review"},
        "repo_path": {"type": "string", "description": "Workspace-relative repo path. Default: repos/python_bug.", "default": "repos/python_bug"},
    },
    [],
)

REPO_WORKSPACE_TOOLS = [
    REPO_CLONE,
    REPO_OPEN_FIXTURE,
    REPO_CREATE_BRANCH,
    REPO_LIST_FILES,
    REPO_READ_FILE,
    REPO_WRITE_FILE,
    REPO_RUN_TESTS,
    REPO_DIFF,
    REPO_STATUS,
    REPO_COMMIT,
    REPO_CREATE_PR,
    REPO_REVIEW,
]

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
                           "Optional; defaults to audio/mpeg.",
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

# ── Data analysis ─────────────────────────────────────────────────────────────

DATA_RUN = _fn(
    "data__run",
    "Run Python data analysis code (pandas/matplotlib/numpy) against an uploaded dataset. "
    "The dataset is identified by dataset_id (obtained from POST /datasets). "
    "The code runs in a sandbox where the dataset is available as 'data.csv' (or 'data.json'). "
    "Produce charts by saving matplotlib figures with plt.savefig('chart_N.png'). "
    "Printed output (tables, stats) is captured as a report artifact. "
    "Returns artifact ids for any generated charts and report.",
    {
        "dataset_id": {
            "type": "string",
            "description": "Dataset id (from POST /datasets) to analyze.",
        },
        "code": {
            "type": "string",
            "description": "Python code using pandas/matplotlib/numpy. "
                           "Read data with: import pandas as pd; df = pd.read_csv('data.csv'). "
                           "Save charts with: plt.savefig('chart_1.png'). "
                           "Print tables with: print(df.head()). "
                           "Network access, subprocess, and absolute paths are blocked.",
        },
    },
    ["dataset_id", "code"],
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

ASK_CLARIFICATION = _fn(
    "ask_clarification",
    "Ask the user for one short decision before continuing. Use this instead of writing a plain yes/no question "
    "when a missing choice materially affects the work. Provide 2-3 concrete options; the chat UI will also show "
    "an Other option for custom instructions.",
    {
        "question": {"type": "string", "description": "One concise question for the user."},
        "options": {
            "type": "array",
            "description": "Two or three concrete options the user can choose from.",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Short button label."},
                    "description": {"type": "string", "description": "One short sentence explaining the tradeoff."},
                },
                "required": ["label"],
            },
            "minItems": 2,
            "maxItems": 3,
        },
    },
    ["question", "options"],
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
    CHAT_HISTORY_SEARCH,
    CHAT_HISTORY_RECENT,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    *COMPUTER_TOOLS,
    *REPO_WORKSPACE_TOOLS,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
    DATA_RUN,
    SPAWN_SUBAGENT,
]

#: Subset for sub-agents — no recursive spawning beyond depth 3.
SUBAGENT_TOOLS: list[dict[str, Any]] = [
    BROWSER_SEARCH,
    BROWSER_FETCH,
    BROWSER_EXTRACT_CONTACTS,
    *BROWSER_OPERATOR_TOOLS,
    CHAT_HISTORY_SEARCH,
    CHAT_HISTORY_RECENT,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    *COMPUTER_TOOLS,
    *REPO_WORKSPACE_TOOLS,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
    DATA_RUN,
]

#: Names that always need explicit human approval before execution.
ALWAYS_APPROVAL_TOOL_NAMES: frozenset[str] = frozenset(
    {"gmail__send", "twitter__post", "linkedin__post", "website__publish", "local_computer__exec", "local_computer__open_app"}
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
    CHAT_HISTORY_SEARCH,
    CHAT_HISTORY_RECENT,
    FS_LIST,
    FS_READ,
    FS_WRITE,
    CODE_PYTHON,
    *COMPUTER_TOOLS,
    DOC_PARSE,
    DOC_READ,
    DOC_SUMMARIZE,
    DOC_COMPARE,
    IMAGE_GENERATE,
    IMAGE_EDIT,
    VOICE_TRANSCRIBE,
    VOICE_SPEAK,
    DATA_RUN,
    START_TASK,
    ASK_CLARIFICATION,
]

_START_TASK_TOOL_NAME = "start_task"
_ASK_CLARIFICATION_TOOL_NAME = "ask_clarification"


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
