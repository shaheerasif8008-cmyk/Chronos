---
name: docx
description: Create and edit Microsoft Word (.docx) documents — reports, letters, formatted text with headings, tables, and styles. Use when the user asks for a Word document or .docx file.
requires_connectors: []
spawns_sub_agent: false
---

# DOCX

Author Microsoft Word documents in the task workspace.

## When to use
- "Write / create a Word document / .docx"
- "Turn this into a formatted report / letter"
- "Edit this Word file"

## Creating a document
`.docx` is binary — use `code__python` with python-docx (preinstalled), not `fs__write`:

```python
from docx import Document
d = Document()
d.add_heading("Quarterly Report", level=1)
d.add_paragraph("Executive summary…")
d.add_heading("Details", level=2)
table = d.add_table(rows=1, cols=2)
table.rows[0].cells[0].text = "Metric"
table.rows[0].cells[1].text = "Value"
d.save("report.docx")
```

## Editing an existing document
Open it with `Document("input.docx")`, modify paragraphs/tables, and `save()` to a new workspace path.

## Output rules
Save with a relative path. After saving, summarize in 3-4 sentences and surface the artifact; do not paste the document body into chat.
