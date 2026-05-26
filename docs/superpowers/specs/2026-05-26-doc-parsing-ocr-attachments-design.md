# Design: Document Parsing, OCR & File Attachments

**Date:** 2026-05-26
**Status:** Approved (pending spec review)

## Goal

Let Chronos read files. Two surfaces, sharing one parsing engine:

1. **User-attach (built first):** a user attaches files in chat; Chronos parses and reads them.
2. **Agent-tool (built second):** the agent parses files it encounters mid-task (Gmail attachments, downloaded PDFs, files in its workspace).

Scope decisions (locked during brainstorming):

- **File types:** PDF, DOCX, XLSX, PPTX, CSV, TXT/Markdown, and images (PNG/JPG/WEBP) via OCR. Unknown types are stored but flagged "not parseable yet" — never crash.
- **OCR:** litellm vision model via `core/llm.py` (no system binary; honors the "all LLM calls through core/llm.py" rule).
- **Large files:** truncate a preview into context automatically; store full text as an artifact; give the agent an on-demand `doc__read` tool to pull more.
- **Parse timing:** sync on first use (parse when the referencing message is sent, not eagerly on upload).

Out of scope: audio/video transcription, archives (zip), async background parsing with status polling.

## Architecture overview

```text
                  ┌─────────────────────────┐
  user 📎 upload  │  parsing/engine.py      │  agent doc__parse tool
  ───────────────▶│  parse(bytes,mime,name) │◀──────────────────────
                  │   → ParsedDocument      │
                  └───────────┬─────────────┘
                              │ OCR for images / scanned PDFs
                              ▼
                  core/llm.py  vision_ocr()   (litellm)
```

The parsing engine is pure: `bytes + mime + filename` in, `ParsedDocument` out. It
never touches the DB or object storage. Both surfaces wrap it. This keeps the engine
independently testable and reusable.

## Component 1 — Parsing engine (`apps/api/parsing/engine.py`)

```python
@dataclass
class ParsedDocument:
    full_text: str
    preview: str          # first ~6K tokens of full_text (truncation policy below)
    page_count: int
    char_count: int
    parser_used: str      # "pdf" | "pdf+ocr" | "docx" | "xlsx" | "pptx" | "text" | "image-ocr" | "none"
    truncated: bool       # True when preview < full_text
    note: str | None      # user-facing reason when parsing failed or type unsupported

async def parse_document(raw: bytes, mime: str, filename: str) -> ParsedDocument: ...
```

Dispatch by mime (fall back to extension when mime is generic `application/octet-stream`):

| Type | Library | Notes |
|------|---------|-------|
| PDF | `pypdf` | Per-page text extraction. A page yielding ~no text → render to image → `vision_ocr()`. `parser_used="pdf"` or `"pdf+ocr"`. |
| DOCX | `python-docx` | Paragraphs + table cell text. |
| XLSX | `openpyxl` | Sheet name + rows as delimited text. |
| PPTX | `python-pptx` | Slide text frames + notes. |
| CSV / TXT / MD | stdlib | UTF-8 decode, `errors="replace"`. |
| Images (PNG/JPG/WEBP) | `vision_ocr()` | `parser_used="image-ocr"`. |
| Unknown | — | `parser_used="none"`, `note="not parseable yet"`. Never raises. |

**New Python deps:** `pypdf`, `python-docx`, `openpyxl`, `python-pptx`, `Pillow`
(Pillow for rendering PDF pages to images for OCR). No system binary.

**Truncation policy:** `preview` = first ~6,000 tokens of `full_text`, approximated as
~24,000 characters (4 chars/token). `truncated=True` when the cut applies.

### OCR helper (`core/llm.py`)

Add `async def vision_ocr(image_bytes: bytes, mime: str) -> str`:

- Routes through litellm with a vision-capable model.
- Model resolution: a new `VISION_MODEL` setting; if unset, fall back to the
  `BACKUP_API_KEY` provider's default vision model. If no vision model is configured,
  return `""` and the engine records `note="OCR unavailable: no vision model configured"`.
- Sends the image as a base64 data URL in a multimodal message; prompt asks for verbatim
  text extraction with layout preserved.
- Failures return `""` (engine notes "no text extracted") rather than erroring the turn.

## Component 2 — Upload surface (user-attach, built first)

### `POST /attachments` — `apps/api/routers/attachments.py` (new)

- Multipart upload. Stores raw bytes via existing `core.artifacts.save_artifact(kind="attachment", conversation_id=...)`.
- Returns `{ attachment_id, filename, mime_type, size_bytes }`.
- **No parsing here** (sync-on-first-use).
- Enforces a 25 MB size cap and `permissions.check(member, "upload_attachment", conversation_id)`.

### Chat wiring

- `ChatRequest` (in `routers/chat.py`) gains `attachment_ids: list[str] = []`.
- In `send_message`, before launching the agent loop: for each `attachment_id` whose
  `parse_status` is not yet `parsed`, load raw bytes → `parse_document(...)` →
  - save `full_text` as a `kind="parsed_text"` artifact (`parent_artifact_id` = the attachment),
  - set the attachment row's `parse_status` accordingly,
  - collect each document's `preview` + filename.
- **Routing:** when `attachment_ids` is non-empty, `send_message` forces the agent-loop
  route regardless of `_is_trivial_chat` — otherwise a short message ("summarize this")
  would hit the fast completion path, which has no `doc__read` tool.
- Parsed previews reach the agent loop via the existing **`inherited_context` seed
  pattern** in `agent_loop._load_history`: a new immutable seed block
  `# Attached files` is inserted before the goal message, listing each file's name and
  preview, and noting that `doc__read` can fetch more. This requires no `assemble_context`
  changes. The attachment context is passed into the task's stored state at creation time
  (mirroring how `inherited_context` is passed for sub-agents).

### Chat UI (`apps/web/app/chat/page.tsx`)

- 📎 button on the textarea row (~line 1039).
- On file select: `POST /attachments`, show a chip (filename + size + remove) above the input.
- Include collected `attachment_ids` in the `/chat/message` body; clear chips on send.

## Component 3 — Agent-facing tools (built second)

In `runtime/tool_registry.py`:

- **`doc__parse`** — args `{ artifact_id?: string, path?: string }`. Parses a file the agent
  found: an artifact id, or a path in its `fs` workspace. Returns preview + metadata; stores
  full text as a `parsed_text` artifact (same as the upload path).
- **`doc__read`** — args `{ artifact_id: string, page_range?: string, char_offset?: int, max_chars?: int }`.
  Pulls more of an already-parsed document beyond the injected preview. This is the
  on-demand mechanism for large files.

In `core/tool_broker.py`:

- New `doc` provider branch in `_route` → calls a thin `connectors`-style wrapper around the
  engine (`parsing/tool.py`) that resolves `artifact_id`/`path` to bytes and returns a `ToolResult`.
- `doc` added to the local-tier provider set alongside `browser`, `fs`, `code` (no vault/registry
  lookup needed). Read-only → no approval gate. Loop detection + audit fire automatically via
  the broker.
- Manifest auto-includes the new tools through `available_tool_schemas`.

## Component 4 — Storage & data model

Reuse the existing **artifacts** table; no new table.

- raw upload → `kind="attachment"`
- parsed text → `kind="parsed_text"`

**Migration 0017** (`0017_attachment_parsing.py`, `down_revision="0016_task_checkpoints"`)
adds to `artifacts`:

- `parent_artifact_id UUID NULL` — parsed-text row points at its source attachment.
- `parse_status TEXT NULL` — one of `pending | parsed | failed | unparseable`.
- index on `parent_artifact_id`.

All rows remain tagged `organization_id` and stored under `artifacts/{org_id}/`
(tenant isolation unchanged). `region` default unchanged.

## Error handling & limits

- Corrupt / password-protected / oversized file → `parse_status="failed"`, user-visible
  `note`; chat turn proceeds, attachment shown as "couldn't read this."
- Upload size cap (25 MB) returns 413 before storage.
- Engine truncates `preview` but always stores full text for `doc__read`.
- Vision OCR failure → empty text + note, never an exception that kills the turn.
- Unknown mime → stored, `parse_status="unparseable"`, surfaced gently.

## Testing

- **Engine unit tests** per type with small committed fixtures (1-page PDF, docx, xlsx, pptx,
  csv, png). `vision_ocr` mocked. Assert `parser_used`, `page_count`, truncation.
- **Upload + parse-on-send integration**: `POST /attachments` then `/chat/message` with the
  id; assert a `parsed_text` artifact exists and the preview reaches the seed context.
- **Agent tools**: `doc__parse` / `doc__read` routed through `tool_broker`; assert audit
  events and that loop detection applies.
- **Tenant isolation**: an attachment created under org A is not retrievable/parseable under org B.
- **Failure paths**: corrupt PDF → `failed`; unknown type → `unparseable`; no vision model →
  image returns note, no exception.

## Build order

1. Parsing engine + `vision_ocr` + deps + engine unit tests.
2. Migration 0017; `POST /attachments`; `ChatRequest.attachment_ids`; parse-on-send;
   seed-context injection; chat UI 📎.
3. `doc__parse` / `doc__read` tools; `doc` provider in tool_broker; tool tests.
4. Integration + isolation tests; manual verification of the chat-attach loop.
