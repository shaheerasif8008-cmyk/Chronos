---
name: pptx
description: Create and edit PowerPoint (.pptx) presentations — title slides, bullet content, images, and speaker notes. Use when the user asks for slides, a deck, or a PowerPoint presentation.
requires_connectors: []
spawns_sub_agent: false
---

# PPTX

Build PowerPoint decks in the task workspace.

## When to use
- "Make a slide deck / presentation / PowerPoint / .pptx"
- "Turn this into slides"

## Creating a deck
`.pptx` is binary — use `code__python` with python-pptx (preinstalled):

```python
from pptx import Presentation
from pptx.util import Inches, Pt

prs = Presentation()
title = prs.slides.add_slide(prs.slide_layouts[0])
title.shapes.title.text = "Chronos Overview"
title.placeholders[1].text = "Enterprise AI platform"

bullets = prs.slides.add_slide(prs.slide_layouts[1])
bullets.shapes.title.text = "Highlights"
body = bullets.shapes.placeholders[1].text_frame
body.text = "Governed autonomy"
for point in ["Persistent memory", "Full auditability", "Tenant isolation"]:
    body.add_paragraph().text = point

prs.save("deck.pptx")
```

Aim for one idea per slide and 3-6 bullets per slide.

## Output rules
Save with a relative path, summarize in 3-4 sentences, and surface the artifact.
