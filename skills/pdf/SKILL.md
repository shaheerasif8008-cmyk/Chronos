---
name: pdf
description: Generate, fill, merge, and extract content from PDF files. Use when the user asks to create a PDF report, fill a PDF form, merge or split PDFs, or extract text and tables from a PDF.
requires_connectors: []
spawns_sub_agent: false
---

# PDF

Create and manipulate PDF documents in the task workspace.

## When to use
- "Make / generate / export a PDF …"
- "Fill out this PDF form"
- "Merge / split these PDFs"
- "Extract the text or tables from this PDF"

## Creating a PDF
PDFs are binary. Never use `fs__write` for `.pdf`. Use `code__python` with reportlab:

```python
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

doc = SimpleDocTemplate("report.pdf", pagesize=letter)
styles = getSampleStyleSheet()
story = [Paragraph("Title", styles["Title"]), Spacer(1, 12),
         Paragraph("Body text here.", styles["BodyText"])]
doc.build(story)
```

Save with a relative path so it lands in the workspace and becomes a downloadable artifact on the next scan.

## Extracting from a PDF
Use pdfplumber to read text and tables:

```python
import pdfplumber
with pdfplumber.open("input.pdf") as pdf:
    text = "\n".join(p.extract_text() or "" for p in pdf.pages)
    tables = pdf.pages[0].extract_tables()
```

## Merging / splitting
Use pypdf (`PdfReader`, `PdfWriter`) to combine pages or extract a page range, then save the result to the workspace.

## Output rules
After saving, keep your chat message to 3-4 sentences: name the file, list a few highlights, and tell the user to open the artifact.
